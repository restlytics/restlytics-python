"""restlytics -- framework-native tracing for Python (Django / FastAPI / Flask).

One contract, every language: this SDK emits the same OTLP/JSON wire format as
every other restlytics SDK and obeys the same safety rules (fire-and-forget,
gzip, ~2s timeout, swallow all errors, never block/raise into the host).

Quick start::

    import restlytics
    restlytics.init(service_name="my-api")  # reads RESTLYTICS_* env vars

    # Flask / Django (WSGI):
    app.wsgi_app = restlytics.WsgiMiddleware(app.wsgi_app)

    # FastAPI (ASGI):
    app.add_middleware(restlytics.AsgiMiddleware)

    # SQLAlchemy DB spans:
    restlytics.instrument_sqlalchemy(engine)

The core (``ids``/``sql``/``intervals``/``otlp``/``transport``/``tracer``/
``config``) is pure stdlib so importing restlytics never pulls in a framework.
"""

from __future__ import annotations

from typing import Optional

from .config import Config
from .otlp import SDK_NAME, SDK_VERSION
from .tracer import Tracer
from .transport import (
    HttpTransport,
    LogTransport,
    NullTransport,
    Transport,
    build_transport,
)

__all__ = [
    "__version__",
    "init",
    "get_tracer",
    "is_initialized",
    "Config",
    "Tracer",
    "Transport",
    "HttpTransport",
    "NullTransport",
    "LogTransport",
    "WsgiMiddleware",
    "AsgiMiddleware",
    "DjangoMiddleware",
    "FlaskMiddleware",
    "RestlyticsMiddleware",
    "instrument_sqlalchemy",
    "instrument_django",
    "instrument_requests",
    "instrument_httpx",
]

__version__ = SDK_VERSION

# Module-global tracer set by init(). Integrations resolve it lazily via
# get_tracer() so import order never matters.
_tracer: Optional[Tracer] = None
_config: Optional[Config] = None


def init(
    *,
    key: Optional[str] = None,
    ingest_url: Optional[str] = None,
    service_name: Optional[str] = None,
    environment: Optional[str] = None,
    sample_rate: Optional[float] = None,
    transport: Optional[str] = None,
    capture_sql: Optional[bool] = None,
    timeout_ms: Optional[int] = None,
    max_spans: Optional[int] = None,
    config: Optional[Config] = None,
    transport_impl: Optional[Transport] = None,
    on_error=None,
) -> Tracer:
    """Initialize the SDK and return the global :class:`Tracer`.

    Configuration resolves from ``RESTLYTICS_*`` environment variables, then any
    explicit keyword overrides win. Pass a ready-made :class:`Config` via
    ``config=`` or a custom :class:`Transport` via ``transport_impl=`` (used by
    tests). Safe to call more than once; the last call wins.

    Never raises: a misconfigured SDK installs a no-op tracer rather than break
    the host application's startup.
    """
    global _tracer, _config

    try:
        if config is None:
            config = Config.from_env(
                key=key,
                ingest_url=ingest_url,
                service_name=service_name,
                environment=environment,
                sample_rate=sample_rate,
                transport=transport,
                capture_sql=capture_sql,
                timeout_ms=timeout_ms,
                max_spans=max_spans,
            )

        if transport_impl is None:
            # No key -> NullTransport so instrumentation is inert but importable.
            kind = config.transport if config.key else "null"
            transport_impl = build_transport(
                kind,
                config.ingest_url,
                config.key,
                timeout_ms=config.timeout_ms,
                on_error=on_error,
            )

        _config = config
        _tracer = Tracer(
            transport=transport_impl,
            service_name=config.service_name,
            environment=config.environment,
            sample_rate=config.sample_rate,
            max_spans=config.max_spans,
        )
        return _tracer
    except Exception:
        # Initialization must never break host startup. Fall back to an inert tracer.
        _config = config or Config()
        _tracer = Tracer(
            transport=NullTransport(),
            service_name=getattr(_config, "service_name", "python"),
            environment=getattr(_config, "environment", "production"),
            sample_rate=0.0,
        )
        return _tracer


def get_tracer() -> Tracer:
    """Return the active tracer, initializing a default one from env if needed."""
    global _tracer
    if _tracer is None:
        init()
    assert _tracer is not None  # for type checkers
    return _tracer


def get_config() -> Config:
    """Return the active config (initializing from env if needed)."""
    global _config
    if _config is None:
        init()
    assert _config is not None
    return _config


def is_initialized() -> bool:
    return _tracer is not None


# --------------------------------------------------------------------------- #
# Lazy framework adapter accessors. We import inside the wrappers so that the
# top-level ``import restlytics`` never imports Flask/FastAPI/Django/SQLAlchemy.
# --------------------------------------------------------------------------- #
def WsgiMiddleware(app, **kwargs):  # noqa: N802 - middleware factory, class-like name
    """WSGI middleware (Flask, generic WSGI). See ``integrations.wsgi``."""
    from .integrations.wsgi import WsgiMiddleware as _M

    return _M(app, **kwargs)


def AsgiMiddleware(app, **kwargs):  # noqa: N802
    """ASGI middleware (FastAPI, Starlette). See ``integrations.asgi``."""
    from .integrations.asgi import AsgiMiddleware as _M

    return _M(app, **kwargs)


# Aliases requested by the SPEC's per-framework surface.
RestlyticsMiddleware = WsgiMiddleware


def DjangoMiddleware(get_response):  # noqa: N802
    """Django middleware (use the dotted path in ``MIDDLEWARE`` settings instead)."""
    from .integrations.django import RestlyticsDjangoMiddleware

    return RestlyticsDjangoMiddleware(get_response)


def FlaskMiddleware(app, **kwargs):  # noqa: N802
    """Install restlytics on a Flask app (wraps ``app.wsgi_app``)."""
    from .integrations.flask import init_app

    return init_app(app, **kwargs)


def instrument_sqlalchemy(engine_or_class=None, *, capture_sql: Optional[bool] = None):
    """Attach DB CLIENT-span instrumentation to a SQLAlchemy engine."""
    from .integrations.sqlalchemy import instrument

    return instrument(engine_or_class, capture_sql=capture_sql)


def instrument_django():
    """Install the Django DB connection execute-wrapper instrumentation."""
    from .integrations.django import install_db_instrumentation

    return install_db_instrumentation()


def instrument_requests():
    """Best-effort outbound HTTP instrumentation for the ``requests`` library."""
    from .integrations.httpclient import instrument_requests as _f

    return _f()


def instrument_httpx():
    """Best-effort outbound HTTP instrumentation for the ``httpx`` library."""
    from .integrations.httpclient import instrument_httpx as _f

    return _f()
