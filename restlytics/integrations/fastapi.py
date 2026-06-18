"""FastAPI integration -- a thin convenience wrapper over the ASGI middleware.

FastAPI is built on Starlette (ASGI), so request tracing is the :class:`AsgiMiddleware`.
The route TEMPLATE comes from ``scope["route"].path`` (e.g. ``/users/{id}``),
which Starlette sets once the request is matched -- the ASGI middleware reads it
on finish.

Usage::

    import restlytics
    from fastapi import FastAPI
    from restlytics.integrations.fastapi import init_app

    restlytics.init(service_name="my-fastapi-app")
    app = FastAPI()
    init_app(app)            # or: app.add_middleware(restlytics.AsgiMiddleware)
"""

from __future__ import annotations

from typing import Optional

from .asgi import AsgiMiddleware


def init_app(app, ignore_paths: Optional[list] = None):
    """Install restlytics on a FastAPI/Starlette ``app`` and return it."""
    app.add_middleware(AsgiMiddleware, ignore_paths=ignore_paths)
    return app
