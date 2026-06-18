"""Per-request tracer: trace id, root SERVER span, child buffer, sampling, reset.

Concurrency model
-----------------
A single :class:`Tracer` instance is created once at :func:`restlytics.init` and
shared by every request. The *per-request* state lives in a :class:`contextvars.
ContextVar`, so concurrent threads (WSGI workers) and concurrent ``asyncio``
tasks (ASGI) each see their own trace without interfering. ``contextvars`` is the
async-safe analogue of thread-local storage and is what the SPEC's "reset
per-request state" rule maps to in Python.

Timing model
------------
We use :func:`time.monotonic_ns` for DURATIONS (immune to NTP/clock jumps) and
anchor it once to a single wall-clock reading (:func:`time.time_ns`) so we can
emit absolute epoch-nanosecond timestamps. Each span's absolute time is
``wall_anchor_ns + (monotonic_now - mono_anchor_ns)``.
"""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import ids
from .intervals import union_length
from .otlp import (
    KIND_CLIENT,
    KIND_SERVER,
    STATUS_ERROR,
    Span,
    build_payload,
)
from .transport import Transport

_SELF_TIME_CATEGORIES = ("db", "http", "cache", "app")


@dataclass
class _RequestState:
    """Mutable state for one in-flight request."""

    enabled: bool = False
    sampled: bool = False
    trace_id: str = ""
    root_parent_span_id: Optional[str] = None
    root_span: Optional[Span] = None
    spans: List[Span] = field(default_factory=list)
    wall_anchor_ns: int = 0
    mono_anchor_ns: int = 0
    db_query_count: int = 0


# The active request state. ``None`` means "no active trace in this context".
_current: "contextvars.ContextVar[Optional[_RequestState]]" = contextvars.ContextVar(
    "restlytics_request_state", default=None
)


class Tracer:
    """Owns sampling + self-time computation + flush; per-request state is contextual."""

    def __init__(
        self,
        transport: Transport,
        service_name: str,
        environment: str,
        sample_rate: float = 1.0,
        max_spans: int = 2000,
    ) -> None:
        self._transport = transport
        self._service_name = service_name
        self._environment = environment
        self._sample_rate = sample_rate
        self._max_spans = max_spans

    # -- state access ------------------------------------------------------ #
    def _state(self) -> Optional[_RequestState]:
        return _current.get()

    def is_sampled(self) -> bool:
        state = self._state()
        return bool(state and state.enabled and state.sampled)

    def trace_id(self) -> str:
        state = self._state()
        return state.trace_id if state else ""

    def root_span(self) -> Optional[Span]:
        state = self._state()
        return state.root_span if state else None

    def root_span_id(self) -> Optional[str]:
        span = self.root_span()
        return span.span_id if span else None

    def reset(self) -> None:
        """Clear per-request state for the current context."""
        _current.set(None)

    # -- lifecycle --------------------------------------------------------- #
    def start_server_span(self, name: str, traceparent: Optional[str] = None) -> None:
        """Open the root SERVER span at request start.

        Continues an incoming W3C ``traceparent`` if present, otherwise mints a
        fresh trace id. The sampling decision is HEAD-BASED and made exactly once
        here, keyed off the trace id, so every span in the trace shares its fate.
        """
        state = _RequestState()
        state.enabled = True

        incoming = ids.parse_traceparent(traceparent)
        if incoming is not None:
            state.trace_id = incoming.trace_id
            state.root_parent_span_id = incoming.parent_span_id
            # Respect an upstream "not sampled" decision; only re-roll if sampled.
            state.sampled = incoming.sampled and self._sample_decision(state.trace_id)
        else:
            state.trace_id = ids.trace_id()
            state.root_parent_span_id = None
            state.sampled = self._sample_decision(state.trace_id)

        # Anchor wall-clock <-> monotonic clocks together.
        state.wall_anchor_ns = time.time_ns()
        state.mono_anchor_ns = time.monotonic_ns()

        _current.set(state)

        if not state.sampled:
            return  # not sampled: stay cheap, record nothing

        now = self._now_ns(state)
        state.root_span = Span(
            trace_id=state.trace_id,
            span_id=ids.span_id(),
            parent_span_id=state.root_parent_span_id,
            name=name,
            kind=KIND_SERVER,
            start_unix_nano=now,
            end_unix_nano=now,
        )

    def add_child_span(
        self,
        name: str,
        start_ns: int,
        end_ns: int,
        kind: int = KIND_CLIENT,
    ) -> Optional[Span]:
        """Create a CLIENT child span over an absolute ``[start_ns, end_ns]`` window.

        DB/HTTP/cache instrumentation often only learns of a span AFTER it
        finished, so callers back-date the start. Returns ``None`` when not
        sampled or when the buffer cap is hit (telemetry must never grow
        unbounded).
        """
        state = self._state()
        if state is None or not (state.enabled and state.sampled) or state.root_span is None:
            return None
        if len(state.spans) >= self._max_spans:
            return None

        span = Span(
            trace_id=state.trace_id,
            span_id=ids.span_id(),
            parent_span_id=state.root_span.span_id,
            name=name,
            kind=kind,
            start_unix_nano=start_ns,
            end_unix_nano=end_ns,
        )
        state.spans.append(span)
        return span

    def increment_db_query_count(self) -> None:
        state = self._state()
        if state is not None:
            state.db_query_count += 1

    def now_ns(self) -> int:
        """Absolute current time in epoch nanoseconds for the current context."""
        state = self._state()
        if state is None:
            return time.time_ns()
        return self._now_ns(state)

    def finish_server_span(self) -> None:
        """Close the root span, compute self-time rollups, and flush the batch."""
        state = self._state()
        if state is None or not (state.enabled and state.sampled) or state.root_span is None:
            self.reset()
            return

        state.root_span.set_end(self._now_ns(state))

        self._attach_self_time(state)
        state.root_span.set_int("restlytics.db_query_count", state.db_query_count)
        state.root_span.set_string("restlytics.category", "app")

        self._flush(state)
        self.reset()

    def _flush(self, state: _RequestState) -> None:
        """Build the OTLP payload and hand it to the transport (fire-and-forget)."""
        if state.root_span is None:
            return
        try:
            all_spans = [state.root_span] + state.spans
            payload = build_payload(self._service_name, self._environment, all_spans)
            self._transport.send(payload)
        except Exception:
            # Telemetry must never throw into the host application.
            pass

    # -- internals --------------------------------------------------------- #
    @staticmethod
    def _now_ns(state: _RequestState) -> int:
        return state.wall_anchor_ns + (time.monotonic_ns() - state.mono_anchor_ns)

    def _attach_self_time(self, state: _RequestState) -> None:
        root = state.root_span
        if root is None:
            return

        root_start = root.start_unix_nano
        root_dur = root.duration_ns()

        by_cat: Dict[str, List[tuple]] = {cat: [] for cat in _SELF_TIME_CATEGORIES}
        all_intervals: List[tuple] = []

        for child in state.spans:
            # Normalize to offsets from root start; clamp inverted intervals (skew).
            start = child.start_unix_nano - root_start
            end = child.end_unix_nano - root_start
            if end < start:
                end = start
            all_intervals.append((start, end))
            by_cat[self._category_of(child)].append((start, end))

        self_db = union_length(by_cat["db"])
        self_http = union_length(by_cat["http"])
        self_cache = union_length(by_cat["cache"])
        # app self-time = explicit app-category child time + the root's own
        # exclusive (uncovered) time. Mirrors the ingestion service's computation.
        self_app = union_length(by_cat["app"]) + max(0, root_dur - union_length(all_intervals))

        root.set_int("restlytics.self_ns.db", self_db)
        root.set_int("restlytics.self_ns.http", self_http)
        root.set_int("restlytics.self_ns.cache", self_cache)
        root.set_int("restlytics.self_ns.app", self_app)

    @staticmethod
    def _category_of(span: Span) -> str:
        cat = span.get_string("restlytics.category")
        if cat in _SELF_TIME_CATEGORIES:
            return cat
        return "app"

    def _sample_decision(self, trace_id_: str) -> bool:
        """Head-based trace-id-ratio sampling, deterministic in the trace id."""
        if self._sample_rate >= 1.0:
            return True
        if self._sample_rate <= 0.0:
            return False

        # Use the last 8 hex chars (32 bits) as the entropy source.
        tail = trace_id_[-8:] or "0"
        bucket = int(tail, 16)  # 0 .. 2^32-1
        ratio = bucket / 0xFFFFFFFF
        return ratio < self._sample_rate
