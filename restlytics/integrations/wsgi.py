"""WSGI middleware -- opens the root SERVER span and flushes AFTER the response.

Used by Flask (``app.wsgi_app``) and generic WSGI apps. Django ships its own
middleware (``integrations.django``) that hooks the same lifecycle.

Lifecycle: open the span when the WSGI request begins, then close + flush in the
iterable's ``close()`` -- which the WSGI server calls only once the full response
body has been written to the client. That keeps the flush off the request's
critical path.

Route templates: raw WSGI has no route concept, so we look for a framework hint
in ``environ`` (Flask sets the matched rule). If none is found we fall back to
the request path so high-cardinality URLs still don't leak as the span name --
Flask installs a more precise template via its own integration.
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional

from .. import get_config, get_tracer
from .common import apply_http_attributes, should_trace


class WsgiMiddleware:
    """Wrap a WSGI ``app`` with restlytics request tracing."""

    def __init__(self, app: Callable, ignore_paths: Optional[Iterable[str]] = None) -> None:
        self.app = app
        self._ignore_paths = list(ignore_paths) if ignore_paths is not None else None

    def _ignore(self) -> List[str]:
        if self._ignore_paths is not None:
            return self._ignore_paths
        return get_config().ignore_paths

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/") or "/"

        if not should_trace(path, self._ignore()):
            return self.app(environ, start_response)

        tracer = get_tracer()
        method = environ.get("REQUEST_METHOD", "GET")
        traceparent = environ.get("HTTP_TRACEPARENT")

        try:
            tracer.start_server_span("{0} {1}".format(method, path), traceparent)
        except Exception:
            return self.app(environ, start_response)

        status_holder = {"code": None}

        def _capturing_start_response(status, headers, exc_info=None):
            try:
                status_holder["code"] = int(str(status).split(" ", 1)[0])
            except Exception:
                status_holder["code"] = None
            return start_response(status, headers, exc_info)

        def _finish():
            try:
                root = tracer.root_span()
                if root is not None:
                    route = environ.get("restlytics.route") or path
                    apply_http_attributes(root, method, route, status_holder["code"])
                tracer.finish_server_span()
            except Exception:
                try:
                    tracer.reset()
                except Exception:
                    pass

        try:
            result = self.app(environ, _capturing_start_response)
        except Exception:
            # Mark the root as errored, flush, then re-raise into the host so the
            # framework's own error handling still runs.
            try:
                root = tracer.root_span()
                if root is not None:
                    route = environ.get("restlytics.route") or path
                    apply_http_attributes(root, method, route, 500)
                tracer.finish_server_span()
            except Exception:
                try:
                    tracer.reset()
                except Exception:
                    pass
            raise

        return _ClosingIterator(result, _finish)


class _ClosingIterator:
    """Wrap the WSGI response iterable so we flush in ``close()`` (after the body)."""

    def __init__(self, wrapped: Iterable, on_close: Callable[[], None]) -> None:
        self._wrapped = wrapped
        self._iterator = iter(wrapped)
        self._on_close = on_close
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iterator)

    def close(self):
        # Close the underlying iterable first (frees framework resources), then flush.
        try:
            wrapped_close = getattr(self._wrapped, "close", None)
            if callable(wrapped_close):
                wrapped_close()
        finally:
            if not self._closed:
                self._closed = True
                self._on_close()
