"""Trace / span id generation and W3C ``traceparent`` handling.

OTLP/JSON wants lowercase-hex ids: 32 chars (16 bytes) for a trace id, 16 chars
(8 bytes) for a span id. The ingestion contract additionally rejects all-zero
ids, so we make sure the random bytes are never empty.

Pure stdlib (``os`` / ``re``) so the core stays import-safe with no third-party
dependencies.
"""

from __future__ import annotations

import os
import re
from typing import NamedTuple, Optional

# 00-<32hex>-<16hex>-<2hex>
_TRACEPARENT_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_ALL_ZERO_RE = re.compile(r"^0+$")


class Traceparent(NamedTuple):
    """Parsed W3C ``traceparent`` header."""

    trace_id: str
    parent_span_id: str
    sampled: bool


def trace_id() -> str:
    """32 lowercase hex chars (16 random bytes), never all-zero."""
    return _random_hex(16)


def span_id() -> str:
    """16 lowercase hex chars (8 random bytes), never all-zero."""
    return _random_hex(8)


def _random_hex(num_bytes: int) -> str:
    # os.urandom is cryptographically secure and always available. The all-zero
    # probability is negligible, but the contract forbids it, so guard.
    while True:
        hex_str = os.urandom(num_bytes).hex()
        if not _ALL_ZERO_RE.match(hex_str):
            return hex_str


def parse_traceparent(header: Optional[str]) -> Optional[Traceparent]:
    """Parse a W3C ``traceparent`` header.

    Format: ``version-traceid-spanid-flags``, e.g.
    ``00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01``.

    Returns ``None`` when absent or malformed so the caller falls back to a fresh
    trace. Continuing an incoming traceparent lets a single distributed trace
    stitch together across services.
    """
    if not header:
        return None

    match = _TRACEPARENT_RE.match(header.strip().lower())
    if match is None:
        return None

    trace, parent, flags = match.group(2), match.group(3), match.group(4)

    # Reject the invalid all-zero trace/parent ids per the W3C spec.
    if _ALL_ZERO_RE.match(trace) or _ALL_ZERO_RE.match(parent):
        return None

    # Low bit of the flags byte is the "sampled" flag.
    sampled = (int(flags, 16) & 0x01) == 0x01
    return Traceparent(trace_id=trace, parent_span_id=parent, sampled=sampled)


def format_traceparent(trace_id_: str, span_id_: str, sampled: bool) -> str:
    """Build a W3C ``traceparent`` value for outbound injection (optional)."""
    return "00-{0}-{1}-{2:02x}".format(trace_id_, span_id_, 1 if sampled else 0)
