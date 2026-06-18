"""ASGI middleware -- opens the root SERVER span and flushes AFTER the response.

Used by FastAPI / Starlette (``app.add_middleware(AsgiMiddleware)``). Because the
SDK's per-request state lives in a :class:`contextvars.ContextVar`, concurrent
``asyncio`` tasks each get their own trace.

Route template: FastAPI/Starlette populate ``scope["route"].path`` once the
request has been matched to an endpoint. We read it AFTER the handler runs (it is
not set at request start), falling back to the raw path for unmatched routes so a
high-cardinality URL never becomes the span name.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import get_config, get_tracer
from .common import apply_http_attributes, should_trace


class AsgiMiddleware:
    """Pure-ASGI middleware (no Starlette base class dependency)."""

    def __init__(self, app, ignore_paths: Optional[List[str]] = None) -> None:
        self.app = app
        self._ignore_paths = list(ignore_paths) if ignore_paths is not None else None

    def _ignore(self) -> List[str]:
        if self._ignore_paths is not None:
            return self._ignore_paths
        return get_config().ignore_paths

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/") or "/"
        if not should_trace(path, self._ignore()):
            await self.app(scope, receive, send)
            return

        tracer = get_tracer()
        method = scope.get("method", "GET")
        traceparent = _header(scope, b"traceparent")

        try:
            tracer.start_server_span("{0} {1}".format(method, path), traceparent)
        except Exception:
            await self.app(scope, receive, send)
            return

        status_holder: Dict[str, Any] = {"code": None}

        async def _send(message):
            if message.get("type") == "http.response.start":
                status_holder["code"] = message.get("status")
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception:
            self._finish(tracer, scope, method, path, 500)
            raise

        self._finish(tracer, scope, method, path, status_holder["code"])

    @staticmethod
    def _finish(tracer, scope, method, path, status_code):
        try:
            root = tracer.root_span()
            if root is not None:
                route = _route_template(scope) or path
                apply_http_attributes(root, method, route, status_code)
            tracer.finish_server_span()
        except Exception:
            try:
                tracer.reset()
            except Exception:
                pass


def _header(scope, name: bytes) -> Optional[str]:
    for key, value in scope.get("headers", []) or []:
        if key == name:
            try:
                return value.decode("latin-1")
            except Exception:
                return None
    return None


def _route_template(scope) -> Optional[str]:
    """Extract the matched route template from the ASGI scope (Starlette/FastAPI)."""
    route = scope.get("route")
    if route is not None:
        path = getattr(route, "path", None)
        if isinstance(path, str) and path:
            return path
    # Starlette also exposes the matched path under ``path_format`` sometimes.
    path_format = scope.get("path_format")
    if isinstance(path_format, str) and path_format:
        return path_format
    return None
