"""Environment + native config resolution.

Same keys as every other restlytics SDK (SPEC section 7), with a Python-native
:class:`Config` dataclass surface. The core stays pure stdlib: env vars are read
with :func:`os.environ.get` and there is no dependency on ``python-dotenv`` (the
host app loads its own ``.env`` if it uses one).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

# Default ingest endpoint; the SDK POSTs to ``{ingest_url}/v1/traces``.
DEFAULT_INGEST_URL = "https://ingest.restlytics.com"
DEFAULT_SAMPLE_RATE = 1.0
DEFAULT_TIMEOUT_MS = 2000
DEFAULT_MAX_SPANS = 2000

# Query-string keys scrubbed from outbound ``url.full`` (SPEC section 6).
DEFAULT_REDACT_QUERY_KEYS: List[str] = [
    "token",
    "api_key",
    "apikey",
    "password",
    "secret",
    "access_token",
    "key",
    "signature",
]

# Sensitive headers that are never captured (belt-and-suspenders; we don't
# capture headers at all by default, but instruments use this list if they do).
DEFAULT_REDACT_HEADERS: List[str] = [
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "proxy-authorization",
]

# Paths/routes skipped entirely (no span opened). Trailing ``*`` wildcards work.
DEFAULT_IGNORE_PATHS: List[str] = [
    "/health",
    "/healthz",
    "/up",
    "/favicon.ico",
]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    """Resolved SDK configuration."""

    key: str = ""
    ingest_url: str = DEFAULT_INGEST_URL
    service_name: str = "python"
    environment: str = "production"
    sample_rate: float = DEFAULT_SAMPLE_RATE
    transport: str = "http"  # http | curl (alias) | null | log
    capture_sql: bool = False
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    max_spans: int = DEFAULT_MAX_SPANS

    # Per-instrument toggles.
    instrument_db: bool = True
    instrument_http: bool = True
    instrument_cache: bool = True

    ignore_paths: List[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_PATHS))
    redact_query_keys: List[str] = field(default_factory=lambda: list(DEFAULT_REDACT_QUERY_KEYS))
    redact_headers: List[str] = field(default_factory=lambda: list(DEFAULT_REDACT_HEADERS))

    @property
    def enabled(self) -> bool:
        """The SDK quietly disables itself when there is no key (safe to ship)."""
        return bool(self.key) and self.transport not in ("null",)

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        """Build a :class:`Config` from environment variables, then apply overrides.

        Explicit keyword overrides (passed to :func:`restlytics.init`) win over env.
        ``None`` overrides are ignored so callers can pass-through optionals.
        """
        cfg = cls(
            key=os.environ.get("RESTLYTICS_KEY", "") or "",
            ingest_url=os.environ.get("RESTLYTICS_INGEST_URL", DEFAULT_INGEST_URL) or DEFAULT_INGEST_URL,
            service_name=os.environ.get("RESTLYTICS_SERVICE_NAME", "")
            or os.environ.get("SERVICE_NAME", "")
            or "python",
            environment=os.environ.get("RESTLYTICS_ENV", "")
            or os.environ.get("APP_ENV", "")
            or "production",
            sample_rate=_env_float("RESTLYTICS_SAMPLE_RATE", DEFAULT_SAMPLE_RATE),
            transport=os.environ.get("RESTLYTICS_TRANSPORT", "http") or "http",
            capture_sql=_env_bool("RESTLYTICS_CAPTURE_SQL", False),
            timeout_ms=_env_int("RESTLYTICS_TIMEOUT_MS", DEFAULT_TIMEOUT_MS),
            max_spans=_env_int("RESTLYTICS_MAX_SPANS", DEFAULT_MAX_SPANS),
            instrument_db=_env_bool("RESTLYTICS_INSTRUMENT_DB", True),
            instrument_http=_env_bool("RESTLYTICS_INSTRUMENT_HTTP", True),
            instrument_cache=_env_bool("RESTLYTICS_INSTRUMENT_CACHE", True),
        )

        ignore_env = os.environ.get("RESTLYTICS_IGNORE_PATHS")
        if ignore_env:
            cfg.ignore_paths = [p.strip() for p in ignore_env.split(",") if p.strip()]

        for name, value in overrides.items():
            if value is not None and hasattr(cfg, name):
                setattr(cfg, name, value)

        return cfg
