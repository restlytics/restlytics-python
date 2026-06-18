"""Shared helpers for the framework adapters (path matching, HTTP attributes)."""

from __future__ import annotations

import fnmatch
from typing import Iterable, Optional

from ..otlp import STATUS_ERROR, STATUS_OK, STATUS_UNSET, Span


def should_trace(path: str, ignore_paths: Iterable[str]) -> bool:
    """Whether ``path`` should be traced given the ignore-list (supports ``*``)."""
    normalized = "/" + (path or "").lstrip("/")
    for pattern in ignore_paths:
        pat = "/" + str(pattern).lstrip("/")
        if pat == normalized or fnmatch.fnmatch(normalized, pat):
            return False
    return True


def apply_http_attributes(
    root: Span,
    method: str,
    route_template: str,
    status_code: Optional[int],
) -> None:
    """Stamp the SERVER span with method/route/status + derive the status code.

    ``route_template`` must already be the route TEMPLATE (e.g. ``/users/{id}``),
    never the raw URL -- that is the #1 correctness rule.
    """
    method = method or "GET"
    template = route_template or "/"

    root.set_name("{0} {1}".format(method, template))
    root.set_string("http.request.method", method)
    root.set_string("http.route", template)
    root.set_string("restlytics.category", "app")

    if status_code is not None:
        root.set_int("http.response.status_code", int(status_code))

    code = status_code or 0
    if code >= 500:
        if root.status_code() != STATUS_ERROR:
            root.set_status(STATUS_ERROR, "HTTP {0}".format(code))
    elif root.status_code() == STATUS_UNSET:
        root.set_status(STATUS_OK)
