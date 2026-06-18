"""SQLAlchemy integration: DB CLIENT spans via cursor-execute events.

We hook ``before_cursor_execute`` to stamp a start time on the execution context
and ``after_cursor_execute`` to close the DB span. Works on an :class:`Engine`,
an async engine's ``.sync_engine``, or the global :class:`Engine` class (so every
engine created afterwards is instrumented).

Usage::

    import restlytics
    from sqlalchemy import create_engine

    restlytics.init(service_name="my-api")
    engine = create_engine("postgresql://...")
    restlytics.instrument_sqlalchemy(engine)

Only the NORMALIZED, literal-free statement is sent as ``db.query.summary``; raw
SQL (``db.query.text``, capped 2048 chars) is sent only when ``capture_sql`` is
on. Binding values are NEVER sent -- only the count.
"""

from __future__ import annotations

from typing import Optional

from .. import get_config, get_tracer
from ..otlp import KIND_CLIENT
from ..sql import normalize, operation_of

_START_ATTR = "_restlytics_start_ns"


def instrument(engine_or_class=None, *, capture_sql: Optional[bool] = None):
    """Attach DB-span instrumentation. Returns the target on success, else ``None``.

    ``engine_or_class`` may be an Engine instance, ``None`` (instruments the
    global ``Engine`` class so all engines are covered), or any object the
    SQLAlchemy event system accepts.
    """
    try:
        from sqlalchemy import event
    except Exception:
        return None

    target = engine_or_class
    if target is None:
        try:
            from sqlalchemy.engine import Engine

            target = Engine
        except Exception:
            return None

    # Async engines proxy a sync engine; instrument that.
    sync_engine = getattr(target, "sync_engine", None)
    if sync_engine is not None:
        target = sync_engine

    try:
        if not event.contains(target, "before_cursor_execute", _before_cursor_execute):
            event.listen(target, "before_cursor_execute", _before_cursor_execute)
        if not event.contains(target, "after_cursor_execute", _after_cursor_execute):
            event.listen(target, "after_cursor_execute", _after_cursor_execute)
    except Exception:
        return None

    return target


def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    try:
        tracer = get_tracer()
        if not tracer.is_sampled():
            return
        # Stash the monotonic-anchored start on the execution context.
        if context is not None:
            setattr(context, _START_ATTR, tracer.now_ns())
    except Exception:
        pass


def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    try:
        tracer = get_tracer()
        if not tracer.is_sampled():
            return

        cfg = get_config()
        if not cfg.instrument_db:
            return

        end_ns = tracer.now_ns()
        start_ns = getattr(context, _START_ATTR, None) if context is not None else None
        if start_ns is None:
            start_ns = end_ns

        summary = normalize(statement or "")
        span = tracer.add_child_span(summary or "db", start_ns, end_ns, kind=KIND_CLIENT)
        if span is None:
            return

        tracer.increment_db_query_count()
        span.set_string("restlytics.category", "db")
        span.set_string("db.query.summary", summary)

        system = _dialect_system(conn)
        if system:
            span.set_string("db.system.name", system)

        namespace = _database_name(conn)
        if namespace:
            span.set_string("db.namespace", namespace)

        op = operation_of(statement or "")
        if op:
            span.set_string("db.operation.name", op)

        span.set_int("restlytics.bindings_count", _count_bindings(parameters, executemany))

        if cfg.capture_sql and statement:
            span.set_string("db.query.text", statement[:2048])
    except Exception:
        # Telemetry must never throw into a query path.
        pass


def _dialect_system(conn) -> Optional[str]:
    try:
        name = conn.dialect.name  # e.g. 'postgresql', 'mysql', 'sqlite'
    except Exception:
        return None
    mapping = {
        "postgresql": "postgresql",
        "psycopg2": "postgresql",
        "mysql": "mysql",
        "mariadb": "mysql",
        "sqlite": "sqlite",
        "oracle": "oracle",
        "mssql": "mssql",
    }
    return mapping.get(name, name)


def _database_name(conn) -> Optional[str]:
    try:
        db = conn.engine.url.database
        return str(db) if db else None
    except Exception:
        return None


def _count_bindings(parameters, executemany: bool) -> int:
    try:
        if parameters is None:
            return 0
        if executemany:
            total = 0
            for row in parameters:
                total += len(row) if hasattr(row, "__len__") else 1
            return total
        return len(parameters) if hasattr(parameters, "__len__") else 1
    except Exception:
        return 0
