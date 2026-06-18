"""Best-effort outbound HTTP instrumentation for ``requests`` and ``httpx``.

Optional: these monkeypatch the respective client's send path to record an HTTP
CLIENT span per outbound call. The ``url.full`` query string is redacted of
sensitive keys; request/response bodies are never captured.

Both functions are no-ops (returning ``False``) when the target library isn't
installed, so calling them is always safe.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from .. import get_config, get_tracer
from ..otlp import KIND_CLIENT, STATUS_ERROR
from ..redact import redact_url

_requests_installed = False
_httpx_installed = False


def instrument_requests() -> bool:
    """Wrap ``requests.adapters.HTTPAdapter.send`` to emit HTTP CLIENT spans."""
    global _requests_installed
    if _requests_installed:
        return True
    try:
        from requests.adapters import HTTPAdapter
    except Exception:
        return False

    original = HTTPAdapter.send

    def _send(self, request, **kwargs):
        tracer = get_tracer()
        cfg = get_config()
        if not (tracer.is_sampled() and cfg.instrument_http):
            return original(self, request, **kwargs)

        start_ns = tracer.now_ns()
        status = None
        errored = False
        try:
            response = original(self, request, **kwargs)
            status = getattr(response, "status_code", None)
            return response
        except Exception:
            errored = True
            raise
        finally:
            try:
                _record(
                    tracer,
                    cfg,
                    method=getattr(request, "method", "GET"),
                    url=getattr(request, "url", ""),
                    status=status,
                    start_ns=start_ns,
                    end_ns=tracer.now_ns(),
                    errored=errored,
                )
            except Exception:
                pass

    HTTPAdapter.send = _send  # type: ignore[assignment]
    _requests_installed = True
    return True


def instrument_httpx() -> bool:
    """Wrap ``httpx.Client.send`` to emit HTTP CLIENT spans."""
    global _httpx_installed
    if _httpx_installed:
        return True
    try:
        import httpx
    except Exception:
        return False

    original = httpx.Client.send

    def _send(self, request, **kwargs):
        tracer = get_tracer()
        cfg = get_config()
        if not (tracer.is_sampled() and cfg.instrument_http):
            return original(self, request, **kwargs)

        start_ns = tracer.now_ns()
        status = None
        errored = False
        try:
            response = original(self, request, **kwargs)
            status = getattr(response, "status_code", None)
            return response
        except Exception:
            errored = True
            raise
        finally:
            try:
                _record(
                    tracer,
                    cfg,
                    method=getattr(request, "method", "GET"),
                    url=str(getattr(request, "url", "")),
                    status=status,
                    start_ns=start_ns,
                    end_ns=tracer.now_ns(),
                    errored=errored,
                )
            except Exception:
                pass

    httpx.Client.send = _send  # type: ignore[assignment]
    _httpx_installed = True
    return True


def _record(tracer, cfg, method, url, status, start_ns, end_ns, errored) -> None:
    redacted = redact_url(url or "", cfg.redact_query_keys)
    host = _host_of(url)

    name = "{0} {1}".format(method or "GET", host or "http")
    span = tracer.add_child_span(name, start_ns, end_ns, kind=KIND_CLIENT)
    if span is None:
        return

    span.set_string("restlytics.category", "http")
    span.set_string("http.request.method", method or "GET")
    if redacted:
        span.set_string("url.full", redacted)
    if host:
        span.set_string("server.address", host)
    if status is not None:
        span.set_int("http.response.status_code", int(status))
    if errored or (status is not None and int(status) >= 500):
        span.set_status(STATUS_ERROR, "http error")


def _host_of(url: str) -> Optional[str]:
    try:
        return urlsplit(url).hostname
    except Exception:
        return None
