"""Redaction helpers shared by the framework + HTTP instruments.

Belt-and-suspenders on top of the always-on SQL normalization (SPEC section 6):
scrub sensitive query-string keys from outbound ``url.full`` and provide a
sensitive-header check. Request/response bodies and binding values are never
captured anywhere in the SDK.
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def redact_url(url: str, query_keys: Iterable[str]) -> str:
    """Return ``url`` with sensitive query-string values replaced by ``REDACTED``.

    Best-effort and never throws -- on any parse error the original (still
    non-body) URL is returned unchanged.
    """
    if not url:
        return url
    try:
        sensitive = {k.lower() for k in query_keys}
        parts = urlsplit(url)
        if not parts.query:
            return url
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        scrubbed = [
            (k, "REDACTED" if k.lower() in sensitive else v) for k, v in pairs
        ]
        new_query = urlencode(scrubbed)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    except Exception:
        return url


def is_sensitive_header(name: str, sensitive: Iterable[str]) -> bool:
    """Whether ``name`` is a header that must never be captured."""
    lowered = name.lower()
    return lowered in {h.lower() for h in sensitive}
