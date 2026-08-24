"""Redaction helpers shared by the framework + HTTP instruments.

Belt-and-suspenders on top of the always-on SQL normalization (SPEC section 6):
scrub outbound ``url.full`` values and provide an attribute firewall.
Request/response bodies, logs, exception content, headers, and binding values
are never captured anywhere in the SDK.
"""

from __future__ import annotations

import re
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

_LOG_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_LOG_URL_RE = re.compile(r"(?i)https?://[^\s<>\"']+")
_LOG_AUTH_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Z0-9._~+/=-]+")
_LOG_JWT_RE = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_LOG_OPAQUE_SECRET_RE = re.compile(
    r"(?i)\b(?:sk|rk)[-_](?:live|test)[-_][A-Za-z0-9_-]{8,}\b|"
    r"\b(?:gh[pousr]|rl)_[A-Za-z0-9_-]{8,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b"
)
_LOG_SENSITIVE_PAIR_RE = re.compile(
    r"(?ix)"
    r"(?P<prefix>[\"']?(?:"
    r"authorization|proxy[-_.]?authorization|cookie|set[-_.]?cookie|x[-_.]?api[-_.]?key|"
    r"api[-_.]?key|access[-_.]?token|refresh[-_.]?token|token|password|passwd|secret|"
    r"credential(?:s)?|request(?:[-_.]?(?:body|payload|form))?|"
    r"response(?:[-_.]?(?:body|payload|form))?|body|payload|form|bindings?|"
    r"params?|arguments?|args"
    r")[\"']?\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;}\]]+)"
)
_LOG_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

LOG_MESSAGE_MAX_CHARS = 2048
_REDACTED = "[REDACTED]"


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


def redact_exception_message(message: Optional[str]) -> Optional[str]:
    """Exception text is intentionally omitted; Restlytics is not a crash tracker."""
    del message
    return None


def redact_log_message(message: str, max_chars: int = LOG_MESSAGE_MAX_CHARS) -> str:
    """Scrub a native log message before it crosses the export boundary.

    Logs are intentionally treated as untrusted, content-bearing input. The
    scrubber removes common credential/header/body forms, URL credentials and
    query values, standalone JWT-like tokens, and email addresses. It also caps
    the result and strips control characters so a hostile ``LogRecord`` cannot
    grow payloads without bound or inject terminal controls into preview mode.

    This is deliberately more aggressive than span redaction: false-positive
    redaction is preferable to exporting a credential or personal identifier.
    """
    try:
        value = str(message)
        value = _LOG_AUTH_RE.sub(_REDACTED, value)
        value = _LOG_JWT_RE.sub(_REDACTED, value)
        value = _LOG_OPAQUE_SECRET_RE.sub(_REDACTED, value)
        value = _LOG_SENSITIVE_PAIR_RE.sub(
            lambda match: match.group("prefix") + _REDACTED,
            value,
        )
        value = _LOG_EMAIL_RE.sub("[REDACTED_EMAIL]", value)
        value = _LOG_URL_RE.sub(lambda match: redact_url(match.group(0), ()), value)
        value = _LOG_CONTROL_RE.sub("", value)
        limit = max(0, int(max_chars))
        return value[:limit] if limit else ""
    except BaseException:
        # A hostile ``__str__`` implementation must not escape the SDK.
        return _REDACTED


def is_sensitive_header(name: str, sensitive: Iterable[str]) -> bool:
    """Whether ``name`` is a header that must never be captured."""
    lowered = name.lower()
    return lowered in {h.lower() for h in sensitive}
