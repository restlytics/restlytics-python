"""Flask integration.

Flask is a WSGI framework, so request tracing reuses the WSGI middleware. The one
Flask-specific piece is the route TEMPLATE: Flask resolves the matched
``url_rule`` during dispatch, so we publish it into the WSGI ``environ`` from a
``before_request`` hook (where ``request.url_rule`` is available) under the
``restlytics.route`` key the WSGI middleware reads on finish.

Usage::

    import restlytics
    from restlytics.integrations.flask import init_app

    restlytics.init(service_name="my-flask-app")
    app = Flask(__name__)
    init_app(app)
"""

from __future__ import annotations

from typing import Optional

from .wsgi import WsgiMiddleware


def init_app(app, ignore_paths: Optional[list] = None):
    """Install restlytics on a Flask ``app`` and return it.

    Wraps ``app.wsgi_app`` with the WSGI middleware and registers a
    ``before_request`` hook that exposes the matched route template.
    """
    # Publish the matched route template into the WSGI environ so the middleware
    # can stamp ``http.route`` with the TEMPLATE (e.g. ``/users/<int:id>``), never
    # the raw URL.
    @app.before_request
    def _restlytics_route_hint():  # pragma: no cover - exercised under a real server
        try:
            from flask import request

            rule = request.url_rule
            if rule is not None and getattr(rule, "rule", None):
                request.environ["restlytics.route"] = rule.rule
        except Exception:
            pass

    app.wsgi_app = WsgiMiddleware(app.wsgi_app, ignore_paths=ignore_paths)
    return app
