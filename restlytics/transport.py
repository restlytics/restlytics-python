"""Transport layer: fire-and-forget OTLP delivery.

Design constraints (all in service of "telemetry must never hurt the host app",
SPEC section 6):
  * Runs AFTER the response is flushed; the actual POST is handed to a daemon
    thread so even the gzip + network time is off the request's critical path.
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
import threading
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional


class Transport:
    """Transport interface. ``send`` accepts a fully-built OTLP payload dict."""

    def send(self, payload: Dict[str, Any]) -> None:  # pragma: no cover - interface
        raise NotImplementedError


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

    The send is dispatched to a short-lived daemon thread so the host request is
    never blocked on the network. All errors are swallowed.
    """

    def __init__(
        self,
        ingest_url: str,
        key: str,
        timeout_ms: int = 2000,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._url = self._build_url(ingest_url)
        self._key = key
        self._timeout = max(0.1, timeout_ms / 1000.0)
        self._on_error = on_error

    @staticmethod
    def _build_url(ingest_url: str) -> str:
        return ingest_url.rstrip("/") + "/v1/traces"

    def send(self, payload: Dict[str, Any]) -> None:
        # Defensive: without the basics there's nothing useful to do.
        if not self._url or not self._key:
            return

        try:
            body = self._encode(payload)
        except Exception as exc:  # noqa: BLE001 - never raise into host
            self._report("restlytics: failed to encode payload: {0}".format(exc))
            return

        if body is None:
            return

        # Fire-and-forget on a daemon thread: the host request never waits for I/O.
        thread = threading.Thread(
            target=self._post,
            args=(body,),
            name="restlytics-flush",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:  # noqa: BLE001 - thread spawn can fail under load
            # Fall back to a synchronous send rather than dropping (still swallows).
            self._report("restlytics: thread start failed: {0}".format(exc))
            self._post(body)

    def _encode(self, payload: Dict[str, Any]) -> Optional[bytes]:
        json_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return gzip.compress(json_bytes, compresslevel=6)

    def _post(self, body: bytes) -> None:
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
        except urllib.error.URLError as exc:
            # Degrade silently on timeout/503/connection error -- drop the batch.
            self._report("restlytics: send failed: {0}".format(exc))
        except Exception as exc:  # noqa: BLE001 - absolute backstop
            self._report("restlytics: transport exception: {0}".format(exc))

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
