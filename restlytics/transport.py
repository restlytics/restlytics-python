"""Transport layer: fire-and-forget OTLP delivery.

Design constraints (all in service of "telemetry must never hurt the host app",
SPEC section 6):
  * Runs AFTER the response is flushed; payloads enter a bounded queue serviced
    by one daemon worker, so gzip + network time is off the request's critical path.
  * Hard short timeout (~2s) so a slow/unreachable ingest endpoint can't pile up.
  * Every error path is swallowed. We never raise into the host application.

Wire format (must match the ingestion contract exactly):
    POST {ingest_url}/v1/traces or {ingest_url}/v1/logs
    X-Restlytics-Key: {key}
    Content-Type: application/json
    Content-Encoding: gzip
    body = gzip(json)

Pure stdlib: ``urllib`` + ``gzip`` + ``threading``. No third-party HTTP client.
"""

from __future__ import annotations

import gzip
import json
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, cast


_diagnostic_context = threading.local()


def is_reporting_transport_diagnostic() -> bool:
    """Whether this thread is inside an ``on_error`` callback.

    Native logging handlers use this guard to prevent an ``on_error`` callback
    that itself logs from creating an infinite failed-export feedback loop.
    """
    return bool(getattr(_diagnostic_context, "active", False))


@dataclass(frozen=True)
class TransportDiagnostics:
    """A lock-safe snapshot of delivery health; never contains payload data."""

    accepted_batches: int
    delivered_batches: int
    dropped_batches: int
    failed_batches: int
    queued_batches: int
    in_flight_batches: int
    queue_capacity: int
    closed: bool


class Exporter:
    """Provider-neutral customer exporter contract.

    Implement :meth:`export_traces` and, when native logs are enabled,
    :meth:`export_logs`. Restlytics invokes these callbacks on a dedicated
    bounded worker through :class:`ExporterTransport`; callbacks never run on
    an instrumented request/job path. Payloads are already source-redacted,
    production-shaped OTLP dictionaries and never contain the Restlytics key.

    Lifecycle callbacks receive a maximum wait in milliseconds. Implementations
    should honor it and return ``False`` when their own drain cannot complete.
    ``None`` is accepted as a successful return for ergonomic compatibility.
    """

    def export_traces(self, payload: Dict[str, Any]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def export_logs(self, payload: Dict[str, Any]) -> None:
        """Export one OTLP logs batch; the default intentionally drops it."""
        del payload

    def flush(self, timeout_ms: int = 2000) -> bool:
        """Drain provider-owned work within ``timeout_ms`` milliseconds."""
        del timeout_ms
        return True

    def shutdown(self, timeout_ms: int = 2000) -> bool:
        """Perform the provider's bounded final drain and cleanup."""
        return self.flush(timeout_ms)


class Transport:
    """Legacy SDK transport interface.

    Existing ``transport_impl=`` integrations remain supported. New customer
    providers should implement :class:`Exporter` and pass ``exporter=`` to
    :func:`restlytics.init`; the SDK then supplies bounded async isolation.
    """

    def send(self, payload: Dict[str, Any]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def send_logs(self, payload: Dict[str, Any]) -> None:
        """Export an OTLP logs payload.

        The compatibility fallback delegates to :meth:`send`, which lets custom
        capture transports written for the trace-only SDK observe log payloads
        without an immediate interface migration. Network transports override
        this method so logs use the distinct OTLP ``/v1/logs`` signal path.
        """
        self.send(payload)

    def flush(self, timeout_ms: int = 2000) -> bool:
        """Wait for accepted work during process shutdown. Non-HTTP transports are immediate."""
        return True

    def close(self, timeout_ms: int = 2000) -> bool:
        return self.flush(timeout_ms)


class NullTransport(Transport):
    """No-op transport (tests / disabling delivery while keeping instrumentation)."""

    def send(self, payload: Dict[str, Any]) -> None:
        return None


class LogTransport(Transport):
    """Capture/log transport for local debugging and tests.

    Stores every payload in :attr:`payloads` and optionally invokes a sink
    callback (e.g. ``print`` or a logger). Synchronous and never touches the
    network, so tests can assert on what would have been sent.
    """

    def __init__(self, sink: Optional[Callable[[str], None]] = None) -> None:
        self.payloads: List[Dict[str, Any]] = []
        self.trace_payloads: List[Dict[str, Any]] = []
        self.log_payloads: List[Dict[str, Any]] = []
        self._sink = sink

    def send(self, payload: Dict[str, Any]) -> None:
        self.trace_payloads.append(payload)
        self._capture(payload)

    def send_logs(self, payload: Dict[str, Any]) -> None:
        self.log_payloads.append(payload)
        self._capture(payload)

    def _capture(self, payload: Dict[str, Any]) -> None:
        self.payloads.append(payload)
        if self._sink is not None:
            try:
                self._sink(json.dumps(payload))
            except Exception:
                # Even logging must not throw.
                pass


class PreviewTransport(Transport):
    """Structured, local-only production payload preview; never opens a socket."""

    def __init__(
        self,
        sample_rate: float,
        sink: Optional[Callable[[str], None]] = print,
    ) -> None:
        self.sample_rate = sample_rate
        self.reports: List[Dict[str, Any]] = []
        self._sink = sink

    def send(self, payload: Dict[str, Any]) -> None:
        self._capture(payload, "traces")

    def send_logs(self, payload: Dict[str, Any]) -> None:
        self._capture(payload, "logs")

    def _capture(self, payload: Dict[str, Any], signal: str) -> None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if signal == "logs":
                item_count = sum(
                    len(scope.get("logRecords", []))
                    for resource in payload.get("resourceLogs", [])
                    for scope in resource.get("scopeLogs", [])
                )
            else:
                item_count = sum(
                    len(scope.get("spans", []))
                    for resource in payload.get("resourceSpans", [])
                    for scope in resource.get("scopeSpans", [])
                )
            report = {
                "mode": "preview",
                "networkRequestMade": False,
                "signal": signal,
                "configuredSampleRate": self.sample_rate,
                "itemCount": item_count,
                "jsonBytes": len(encoded),
                "gzipBytes": len(gzip.compress(encoded, compresslevel=6)),
                "redactionPolicyApplied": [
                    "url query values and URL credentials",
                    "sensitive headers and credentials",
                    "request and response bodies",
                    "exception messages and stack traces",
                    "SQL binding values",
                ],
                "payload": payload,
            }
            if signal == "traces":
                # Preserve the v1 preview fields for existing consumers.
                report["sampled"] = True
                report["spanCount"] = item_count
            else:
                report["logRecordCount"] = item_count
            self.reports.append(report)
            if len(self.reports) > 16:
                del self.reports[0]
            if self._sink is not None:
                self._sink(json.dumps(report, ensure_ascii=False, indent=2))
        except Exception:
            # Preview retains the SDK's never-raise guarantee.
            pass


class ExporterTransport(Transport):
    """Bounded, asynchronous safety boundary around a customer :class:`Exporter`.

    Export callbacks run serially on one daemon worker. Saturation drops new
    batches instead of blocking or growing memory, and every customer exception
    (including lifecycle/diagnostic exceptions) is contained. ``flush`` and
    ``close`` wait no longer than the caller's deadline even if a provider
    ignores it or blocks forever.
    """

    def __init__(
        self,
        exporter: Exporter,
        *,
        on_error: Optional[Callable[[str], None]] = None,
        queue_capacity: int = 64,
    ) -> None:
        self.exporter = exporter
        self._on_error = on_error
        self._capacity = max(1, int(queue_capacity))
        self._queue: "queue.Queue[object]" = queue.Queue(maxsize=self._capacity)
        self._lock = threading.Lock()
        self._closed = False
        self._in_flight = 0
        self._pending = 0
        self._accepted = 0
        self._delivered = 0
        self._dropped = 0
        self._failed = 0
        self._stop = object()
        self._worker = threading.Thread(
            target=self._run,
            name="restlytics-custom-exporter",
            daemon=True,
        )
        self._worker.start()

    def send(self, payload: Dict[str, Any]) -> None:
        self._enqueue("traces", payload)

    def send_logs(self, payload: Dict[str, Any]) -> None:
        self._enqueue("logs", payload)

    def diagnostics(self) -> TransportDiagnostics:
        with self._lock:
            return TransportDiagnostics(
                accepted_batches=self._accepted,
                delivered_batches=self._delivered,
                dropped_batches=self._dropped,
                failed_batches=self._failed,
                queued_batches=self._queue.qsize(),
                in_flight_batches=self._in_flight,
                queue_capacity=self._capacity,
                closed=self._closed,
            )

    def flush(self, timeout_ms: int = 2000) -> bool:
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        if not self._await_pending(deadline):
            return False
        return self._invoke_lifecycle("flush", deadline) is True

    def close(self, timeout_ms: int = 2000) -> bool:
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        with self._lock:
            if self._closed and not self._worker.is_alive():
                return True
            self._closed = True

        drained = self._await_pending(deadline)
        shutdown = self._invoke_lifecycle("shutdown", deadline) if drained else None
        # Stop only after the lifecycle callback actually completed. If it timed
        # out, leave the daemon worker available for a later bounded retry.
        if drained and shutdown is not None and self._worker.is_alive():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                self._queue.put(self._stop, timeout=remaining)
            except queue.Full:
                return False
            self._worker.join(max(0.0, deadline - time.monotonic()))
        return bool(shutdown is True and not self._worker.is_alive())

    def _enqueue(self, signal: str, payload: Dict[str, Any]) -> None:
        try:
            with self._lock:
                if self._closed:
                    self._dropped += 1
                    unavailable = True
                else:
                    unavailable = False
                    try:
                        self._queue.put_nowait((signal, payload))
                    except queue.Full:
                        self._dropped += 1
                        full = True
                    else:
                        self._accepted += 1
                        self._pending += 1
                        full = False
            if unavailable:
                self._report("restlytics: batch dropped because custom exporter is closed")
            elif full:
                self._report("restlytics: batch dropped because custom exporter queue is full")
        except BaseException as exc:
            # Payload enqueue and even hostile mapping implementations are outside
            # the host application's failure domain.
            with self._lock:
                self._dropped += 1
            self._report("restlytics: custom exporter enqueue failed: {0}".format(exc))

    def _await_pending(self, deadline: float) -> bool:
        while True:
            with self._lock:
                pending = self._pending
            if pending == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def _invoke_lifecycle(self, operation: str, deadline: float) -> Optional[bool]:
        if not self._worker.is_alive():
            return None
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            return None
        event = threading.Event()
        result: Dict[str, bool] = {}
        task = (operation, int(remaining * 1000), event, result)
        try:
            self._queue.put(task, timeout=remaining)
        except BaseException:
            return None
        if not event.wait(max(0.0, deadline - time.monotonic())):
            return None
        return bool(result.get("ok", False))

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._stop:
                    return
                task = cast(Tuple[Any, ...], item)
                operation = str(task[0])
                if operation in ("traces", "logs"):
                    self._run_export(operation, cast(Dict[str, Any], task[1]))
                else:
                    self._run_lifecycle(
                        operation,
                        cast(int, task[1]),
                        cast(threading.Event, task[2]),
                        cast(Dict[str, bool], task[3]),
                    )
            except BaseException as exc:
                # Absolute worker backstop. Normal callback failures are counted
                # inside the operation-specific methods below.
                self._report("restlytics: custom exporter worker failed: {0}".format(exc))
            finally:
                self._queue.task_done()

    def _run_export(self, signal: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._in_flight = 1
        try:
            callback_name = "export_logs" if signal == "logs" else "export_traces"
            callback = getattr(self.exporter, callback_name)
            callback(payload)
        except BaseException as exc:
            with self._lock:
                self._failed += 1
            self._report("restlytics: custom {0} export failed: {1}".format(signal, exc))
        else:
            with self._lock:
                self._delivered += 1
        finally:
            with self._lock:
                self._in_flight = 0
                self._pending -= 1

    def _run_lifecycle(
        self,
        operation: str,
        timeout_ms: int,
        event: threading.Event,
        result: Dict[str, bool],
    ) -> None:
        try:
            callback = getattr(self.exporter, operation, None)
            if not callable(callback) and operation == "shutdown":
                callback = getattr(self.exporter, "close", None)
            if not callable(callback):
                result["ok"] = True
            else:
                value = callback(max(0, timeout_ms))
                result["ok"] = True if value is None else bool(value)
        except BaseException as exc:
            result["ok"] = False
            self._report("restlytics: custom exporter {0} failed: {1}".format(operation, exc))
        finally:
            event.set()

    def _report(self, message: str) -> None:
        if self._on_error is None:
            return
        previous = getattr(_diagnostic_context, "active", False)
        try:
            _diagnostic_context.active = True
            self._on_error(message)
        except BaseException:
            pass
        finally:
            _diagnostic_context.active = previous


class HttpTransport(Transport):
    """Default transport: gzip the JSON body and POST it via ``urllib``.

    A single daemon worker drains a bounded queue so the host request is never
    blocked on the network and thread growth is capped. All errors are swallowed.
    """

    def __init__(
        self,
        ingest_url: str,
        key: str,
        timeout_ms: int = 2000,
        on_error: Optional[Callable[[str], None]] = None,
        queue_capacity: int = 64,
    ) -> None:
        self._url = self._build_url(ingest_url, "traces")
        self._logs_url = self._build_url(ingest_url, "logs")
        self._key = key
        self._timeout = max(0.1, timeout_ms / 1000.0)
        self._on_error = on_error
        self._capacity = max(1, queue_capacity)
        self._queue: "queue.Queue[object]" = queue.Queue(maxsize=self._capacity)
        self._lock = threading.Lock()
        self._closed = False
        self._in_flight = 0
        self._pending = 0
        self._accepted = 0
        self._delivered = 0
        self._dropped = 0
        self._failed = 0
        self._stop = object()
        self._worker = threading.Thread(
            target=self._run,
            name="restlytics-transport",
            daemon=True,
        )
        self._worker.start()

    @staticmethod
    def _build_url(ingest_url: str, signal: str = "traces") -> str:
        return ingest_url.rstrip("/") + "/v1/" + signal

    def send(self, payload: Dict[str, Any]) -> None:
        self._enqueue("traces", payload)

    def send_logs(self, payload: Dict[str, Any]) -> None:
        self._enqueue("logs", payload)

    def _enqueue(self, signal: str, payload: Dict[str, Any]) -> None:
        # The request path performs only a bounded, non-blocking enqueue. Encoding,
        # compression and all I/O happen on the single daemon worker.
        with self._lock:
            unavailable = self._closed or not self._url or not self._key
        if unavailable:
            self._record_drop("restlytics: batch dropped because transport is closed or unconfigured")
            return
        try:
            with self._lock:
                self._pending += 1
                self._accepted += 1
                try:
                    self._queue.put_nowait((signal, payload))
                except queue.Full:
                    self._pending -= 1
                    self._accepted -= 1
                    raise
        except queue.Full:
            self._record_drop("restlytics: batch dropped because transport queue is full")
        except Exception as exc:  # noqa: BLE001 - never raise into host
            self._record_drop("restlytics: enqueue failed: {0}".format(exc))

    def diagnostics(self) -> TransportDiagnostics:
        with self._lock:
            return TransportDiagnostics(
                accepted_batches=self._accepted,
                delivered_batches=self._delivered,
                dropped_batches=self._dropped,
                failed_batches=self._failed,
                queued_batches=self._queue.qsize(),
                in_flight_batches=self._in_flight,
                queue_capacity=self._capacity,
                closed=self._closed,
            )

    def flush(self, timeout_ms: int = 2000) -> bool:
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        while True:
            with self._lock:
                pending = self._pending
            if pending == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def close(self, timeout_ms: int = 2000) -> bool:
        with self._lock:
            if self._closed and not self._worker.is_alive():
                return True
            self._closed = True
        flushed = self.flush(timeout_ms)
        if flushed:
            try:
                self._queue.put_nowait(self._stop)
            except queue.Full:  # pragma: no cover - queue was observed empty
                return False
            self._worker.join(max(0, timeout_ms) / 1000.0)
        return flushed and not self._worker.is_alive()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._stop:
                    return
                with self._lock:
                    self._in_flight = 1
                try:
                    signal, payload = cast(Tuple[str, Dict[str, Any]], item)
                    body = self._encode(payload)
                    if signal == "logs":
                        delivered = body is not None and self._post_logs(body)
                    else:
                        delivered = body is not None and self._post(body)
                    with self._lock:
                        if delivered:
                            self._delivered += 1
                        else:
                            self._failed += 1
                except Exception as exc:  # noqa: BLE001 - absolute worker backstop
                    with self._lock:
                        self._failed += 1
                    self._report("restlytics: failed to encode or send payload: {0}".format(exc))
            finally:
                with self._lock:
                    self._in_flight = 0
                    if item is not self._stop:
                        self._pending -= 1
                self._queue.task_done()

    def _encode(self, payload: Dict[str, Any]) -> Optional[bytes]:
        json_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return gzip.compress(json_bytes, compresslevel=6)

    def _post(self, body: bytes) -> bool:
        return self._post_url(body, self._url)

    def _post_logs(self, body: bytes) -> bool:
        return self._post_url(body, self._logs_url)

    def _post_url(self, body: bytes, url: str) -> bool:
        try:
            request = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                    "X-Restlytics-Key": self._key,
                },
            )
            # Response is always 200 with a partialSuccess envelope; we treat any
            # (or no) response as success and move on. Reading the body lets the
            # connection close cleanly.
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                try:
                    resp.read()
                except Exception:
                    pass
            return True
        except urllib.error.URLError as exc:
            # Degrade silently on timeout/503/connection error -- drop the batch.
            self._report("restlytics: send failed: {0}".format(exc))
            return False
        except Exception as exc:  # noqa: BLE001 - absolute backstop
            self._report("restlytics: transport exception: {0}".format(exc))
            return False

    def _record_drop(self, message: str) -> None:
        with self._lock:
            self._dropped += 1
        self._report(message)

    def _report(self, message: str) -> None:
        if self._on_error is None:
            return
        previous = getattr(_diagnostic_context, "active", False)
        try:
            _diagnostic_context.active = True
            self._on_error(message)
        except BaseException:
            # Even logging must not throw.
            pass
        finally:
            _diagnostic_context.active = previous


def build_transport(
    kind: str,
    ingest_url: str,
    key: str,
    timeout_ms: int = 2000,
    on_error: Optional[Callable[[str], None]] = None,
    sample_rate: float = 1.0,
) -> Transport:
    """Resolve a transport from a config string."""
    normalized = (kind or "http").strip().lower()
    if normalized in ("null", "none", "off"):
        return NullTransport()
    if normalized == "log":
        return LogTransport(sink=on_error)
    if normalized == "preview":
        return PreviewTransport(sample_rate=sample_rate, sink=on_error or print)
    # ``curl`` is accepted as an alias for the default HTTP transport (the spec's
    # ``curl`` option is PHP-specific; Python uses urllib).
    return HttpTransport(ingest_url, key, timeout_ms=timeout_ms, on_error=on_error)
