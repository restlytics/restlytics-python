"""Django integration: request middleware + DB connection execute-wrapper.

Add the middleware by dotted path in ``settings.py``::

    MIDDLEWARE = [
        "restlytics.integrations.django.RestlyticsDjangoMiddleware",
        # ... the rest of your middleware ...
    ]

The middleware opens the root SERVER span in ``__call__`` and flushes it in
``process_response`` -- which Django runs after the view returns -- and also reads
the resolved route template from ``request.resolver_match.route``.

DB spans use Django's ``connection.execute_wrapper`` context manager, installed
once per connection. Call :func:`install_db_instrumentation` from your app's
``AppConfig.ready()`` (or rely on the middleware to install it lazily on the
first request).
"""

from __future__ import annotations

import time
from typing import Optional

from .. import get_config, get_tracer
from ..otlp import KIND_CLIENT, STATUS_ERROR
from ..sql import normalize, operation_of
from .common import apply_http_attributes, should_trace

# Guard so we only register the connection signal once per process.
_db_installed = False


class RestlyticsDjangoMiddleware:
    """Django new-style middleware (callable wrapping ``get_response``)."""

    def __init__(self, get_response):
        self.get_response = get_response
        # Best-effort DB instrumentation install at middleware construction time.
        try:
            install_db_instrumentation()
        except Exception:
            pass

    def __call__(self, request):
        path = request.path or "/"
        cfg = get_config()

        if not should_trace(path, cfg.ignore_paths):
            return self.get_response(request)

        tracer = get_tracer()
        method = request.method or "GET"
        traceparent = request.META.get("HTTP_TRACEPARENT")

        try:
            tracer.start_server_span("{0} {1}".format(method, path), traceparent)
        except Exception:
            return self.get_response(request)

        try:
            response = self.get_response(request)
        except Exception:
            # Flush an errored trace, then re-raise so Django's own error handling
            # (and the 500 response) still runs.
            self._finish(tracer, request, method, path, 500)
            raise

        self._finish(tracer, request, method, path, getattr(response, "status_code", None))
        return response

    @staticmethod
    def _finish(tracer, request, method, path, status_code):
        try:
            root = tracer.root_span()
            if root is None:
                return
            route = _route_template(request) or path
            apply_http_attributes(root, method, route, status_code)
            tracer.finish_server_span()
        except Exception:
            try:
                tracer.reset()
            except Exception:
                pass


def _route_template(request) -> Optional[str]:
    """Resolved Django route template, e.g. ``users/<int:id>``."""
    match = getattr(request, "resolver_match", None)
    if match is not None:
        route = getattr(match, "route", None)
        if isinstance(route, str) and route:
            # Django routes omit the leading slash; normalize for consistency.
            return "/" + route.lstrip("/")
    return None


def install_db_instrumentation() -> None:
    """Register a ``connection_created`` handler that adds an execute-wrapper."""
    global _db_installed
    if _db_installed:
        return

    try:
        from django.db.backends.signals import connection_created
    except Exception:
        return

    def _on_connection_created(sender, connection, **kwargs):
        try:
            connection.execute_wrappers.append(_RestlyticsExecuteWrapper(connection))
        except Exception:
            pass

    connection_created.connect(_on_connection_created, weak=False)

    # Also wrap any already-open connections.
    try:
        from django.db import connections

        for alias in connections:
            conn = connections[alias]
            if conn.connection is not None:
                conn.execute_wrappers.append(_RestlyticsExecuteWrapper(conn))
    except Exception:
        pass

    _db_installed = True


class _RestlyticsExecuteWrapper:
    """Django execute-wrapper that records a DB CLIENT span per query."""

    def __init__(self, connection):
        self._db_system = _vendor_to_system(getattr(connection, "vendor", None))
        self._db_name = None
        try:
            settings_dict = getattr(connection, "settings_dict", {}) or {}
            self._db_name = settings_dict.get("NAME")
        except Exception:
            self._db_name = None

    def __call__(self, execute, sql, params, many, context):
        tracer = get_tracer()
        if not tracer.is_sampled():
            return execute(sql, params, many, context)

        start_ns = tracer.now_ns()
        errored = False
        try:
            return execute(sql, params, many, context)
        except Exception:
            errored = True
            raise
        finally:
            try:
                end_ns = tracer.now_ns()
                self._record(tracer, sql, params, many, start_ns, end_ns, errored)
            except Exception:
                pass

    def _record(self, tracer, sql, params, many, start_ns, end_ns, errored):
        cfg = get_config()
        if not cfg.instrument_db:
            return

        summary = normalize(sql or "")
        span = tracer.add_child_span(summary or "db", start_ns, end_ns, kind=KIND_CLIENT)
        if span is None:
            return

        tracer.increment_db_query_count()
        span.set_string("restlytics.category", "db")
        span.set_string("db.query.summary", summary)
        if self._db_system:
            span.set_string("db.system.name", self._db_system)
        if self._db_name:
            span.set_string("db.namespace", str(self._db_name))

        op = operation_of(sql or "")
        if op:
            span.set_string("db.operation.name", op)

        # Bindings are COUNTED, never sent.
        span.set_int("restlytics.bindings_count", _count_bindings(params, many))

        if cfg.capture_sql and sql:
            span.set_string("db.query.text", sql[:2048])

        if errored:
            span.set_status(STATUS_ERROR, "db error")


def _count_bindings(params, many: bool) -> int:
    try:
        if params is None:
            return 0
        if many:
            # ``executemany`` -> a sequence of param-rows.
            total = 0
            for row in params:
                total += len(row) if hasattr(row, "__len__") else 1
            return total
        return len(params) if hasattr(params, "__len__") else 1
    except Exception:
        return 0


def _vendor_to_system(vendor: Optional[str]) -> Optional[str]:
    mapping = {
        "postgresql": "postgresql",
        "mysql": "mysql",
        "sqlite": "sqlite",
        "oracle": "oracle",
        "microsoft": "mssql",
    }
    if not vendor:
        return None
    return mapping.get(vendor, vendor)
