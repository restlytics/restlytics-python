"""SQL normalization -> a literal-free template string.

Two jobs:
 1. PII / redaction -- strip every literal so we NEVER ship customer values
    (emails, tokens, ids) inside ``db.query.summary``. Only the shape survives.
 2. N+1 grouping -- collapse the query down to a stable fingerprint so that
    ``SELECT * FROM users WHERE id = 1`` and ``... id = 2`` map to the same key.
    ``IN (?, ?, ?)`` lists of varying length also collapse to ``IN (?)`` so a
    batched query and its single-row cousin don't fragment the grouping.

This is deliberately a best-effort lexical normalizer, not a real SQL parser --
it must be fast (runs on every query) and never throw. The algorithm mirrors the
Laravel reference SDK exactly so every language emits the same grouping key.
"""

from __future__ import annotations

import re

# String literals: single- and double-quoted, with escaped-quote / doubled-quote
# support. ``re.DOTALL`` so multi-line literals are handled.
_STR_SINGLE_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'", re.DOTALL)
_STR_DOUBLE_RE = re.compile(r'"(?:[^"\\]|\\.|"")*"', re.DOTALL)

# Numeric literals (hex, decimal/scientific, integer). Word boundaries so we
# don't mangle identifiers like ``column2``.
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_DECIMAL_RE = re.compile(r"\b\d+\.\d+(?:[eE][+-]?\d+)?\b")
_INT_RE = re.compile(r"\b\d+\b")

# Existing positional / named placeholders all normalize to ``?``.
_INDEXED_PLACEHOLDER_RE = re.compile(r"\?\d+")  # ?1, ?2 (some drivers)
_NAMED_PLACEHOLDER_RE = re.compile(r"[:$]\w+")  # :name, $1

# Collapse ``IN (?, ?, ?)`` -> ``IN (?)`` so list length doesn't fragment groups.
_IN_LIST_RE = re.compile(r"\bin\s*\(\s*\?(?:\s*,\s*\?)*\s*\)", re.IGNORECASE)

# Collapse multi-row VALUES tuples: (?, ?), (?, ?) -> (?)
_MULTI_TUPLE_RE = re.compile(
    r"\(\s*\?(?:\s*,\s*\?)*\s*\)(?:\s*,\s*\(\s*\?(?:\s*,\s*\?)*\s*\))+"
)
_SINGLE_TUPLE_RE = re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)")

# Whitespace runs (incl. newlines/tabs).
_WS_RE = re.compile(r"\s+")


def normalize(sql: str) -> str:
    """Normalize a raw SQL string into a stable, literal-free template."""
    if sql is None:
        return ""

    s = sql

    # Drop string literals -> ``?`` so they read like positional bindings.
    s = _STR_SINGLE_RE.sub("?", s)
    s = _STR_DOUBLE_RE.sub("?", s)

    # Normalize existing placeholders FIRST, so the trailing digit in ``$1`` /
    # ``?1`` is consumed by the placeholder rule rather than the numeric-literal
    # rule (which would leave a dangling ``$``/``?`` prefix).
    s = _INDEXED_PLACEHOLDER_RE.sub("?", s)  # ?1, ?2
    s = _NAMED_PLACEHOLDER_RE.sub("?", s)  # :name, $1

    # Drop numeric literals.
    s = _HEX_RE.sub("?", s)
    s = _DECIMAL_RE.sub("?", s)
    s = _INT_RE.sub("?", s)

    # Collapse IN lists and VALUES tuples.
    s = _IN_LIST_RE.sub("IN (?)", s)
    s = _MULTI_TUPLE_RE.sub("(?)", s)
    s = _SINGLE_TUPLE_RE.sub("(?)", s)

    # Squash whitespace, trim, lowercase.
    s = _WS_RE.sub(" ", s).strip()
    return s.lower()


def operation_of(sql: str) -> str:
    """Best-effort first SQL keyword (``select`` / ``insert`` / ...), lowercased.

    Used for the optional ``db.operation.name`` attribute. Never throws.
    """
    if not sql:
        return ""
    stripped = sql.lstrip()
    # Skip a leading line comment if present.
    match = re.match(r"[A-Za-z]+", stripped)
    return match.group(0).lower() if match else ""
