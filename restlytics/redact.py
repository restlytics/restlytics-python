"""Redaction helpers shared by the framework + HTTP instruments.

Belt-and-suspenders on top of the always-on SQL normalization (SPEC section 6):
scrub outbound ``url.full`` values and provide an attribute firewall.
Request/response bodies, logs, exception content, headers, and binding values
are never captured anywhere in the SDK.
"""

from __future__ import annotations

from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_SEGMENTS = {
    "authorization",
    "auth",
    "cookie",
    "cookies",
    "setcookie",
    "password",
    "passwd",
    "secret",
    "token",
    "accesstoken",
    "refreshtoken",
    "apikey",
    "credential",
    "credentials",
    "body",
    "payload",
    "form",
    "stack",
    "stacktrace",
    "log",
}


def is_sensitive_attribute_key(key: str) -> bool:
    """Reject content-bearing fields, including framework-specific variants."""
    normalized = key.strip().lower().replace("-", ".").replace("_", ".")
    if normalized in {
        "http.request.method",
        "http.response.status.code",
        "restlytics.bindings.count",
    }:
        return False
    return any(segment in _SENSITIVE_SEGMENTS for segment in normalized.split("."))


def redact_url(url: str, query_keys: Iterable[str]) -> str:
    """Remove credentials/fragments and replace every query value with ``REDACTED``.

    ``query_keys`` remains accepted for API compatibility; privacy no longer
    depends on knowing secret key names in advance. Parse failures strip query
    and fragment data instead of returning possibly sensitive input unchanged.
    """
    del query_keys
    if not url:
        return url
    try:
        parts = urlsplit(url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        scrubbed = [(key, "REDACTED") for key, _value in pairs]
        new_query = urlencode(scrubbed)
        hostname = parts.hostname or ""
        if parts.port is not None:
            hostname = "{0}:{1}".format(hostname, parts.port)
        return urlunsplit((parts.scheme, hostname, parts.path, new_query, ""))
    except Exception:
        clean = url.split("#", 1)[0].split("?", 1)[0]
        scheme, separator, remainder = clean.partition("://")
        if separator and "@" in remainder:
            remainder = remainder.rsplit("@", 1)[1]
            return scheme + separator + remainder
        return clean


def redact_exception_message(message: Optional[str]) -> None:
    """Exception text is intentionally omitted; Restlytics is not a crash tracker."""
    del message
    return None


def is_sensitive_header(name: str, sensitive: Iterable[str]) -> bool:
    """Whether ``name`` is a header that must never be captured."""
    lowered = name.lower()
    return lowered in {h.lower() for h in sensitive}
