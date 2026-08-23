"""Transport layer: fire-and-forget OTLP delivery.

Design constraints (all in service of "telemetry must never hurt the host app",
SPEC section 6):
  * Runs AFTER the response is flushed; payloads enter a bounded queue serviced
    by one daemon worker, so gzip + network time is off the request's critical path.
  * Hard short timeout (~2s) so a slow/unreachable ingest endpoint can't pile up.
  * Every error path is swallowed. We never raise into the host application.

Wire format (must match the ingestion contract exactly):
    POST {ingest_url}/v1/traces
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
from typing import Any, Callable, Dict, List, Optional


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


class Transport:
    """Transport interface. ``send`` accepts a fully-built OTLP payload dict."""

    def send(self, payload: Dict[str, Any]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

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
        self._sink = sink

    def send(self, payload: Dict[str, Any]) -> None:
        self.payloads.append(payload)
        if self._sink is not None:
            try:
                self._sink(json.dumps(payload))
            except Exception:
                # Even logging must not throw.
                pass


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
        self._url = self._build_url(ingest_url)
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
    def _build_url(ingest_url: str) -> str:
        return ingest_url.rstrip("/") + "/v1/traces"

    def send(self, payload: Dict[str, Any]) -> None:
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
                    self._queue.put_nowait(payload)
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
                    body = self._encode(item)  # type: ignore[arg-type]
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
        try:
            request = urllib.request.Request(
                self._url,
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
        try:
            self._on_error(message)
        except Exception:
            # Even logging must not throw.
            pass


def build_transport(
    kind: str,
    ingest_url: str,
    key: str,
    timeout_ms: int = 2000,
    on_error: Optional[Callable[[str], None]] = None,
) -> Transport:
    """Resolve a transport from a config string (``http``/``curl``/``null``/``log``)."""
    normalized = (kind or "http").strip().lower()
    if normalized in ("null", "none", "off"):
        return NullTransport()
    if normalized == "log":
        return LogTransport(sink=on_error)
    # ``curl`` is accepted as an alias for the default HTTP transport (the spec's
    # ``curl`` option is PHP-specific; Python uses urllib).
    return HttpTransport(ingest_url, key, timeout_ms=timeout_ms, on_error=on_error)
