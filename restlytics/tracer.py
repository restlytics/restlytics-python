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
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import ids
from .intervals import union_length
from .otlp import (
    KIND_CLIENT,
    KIND_SERVER,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNSET,
    Span,
    build_payload,
)
from .transport import Transport

_SELF_TIME_CATEGORIES = ("db", "http", "cache", "queue", "app")


@dataclass
class _RequestState:
    """Mutable state for one in-flight request."""

    enabled: bool = False
    sampled: bool = False
    trace_id: str = ""
    root_parent_span_id: Optional[str] = None
    root_span_id: str = ""
    root_span: Optional[Span] = None
    spans: List[Span] = field(default_factory=list)
    log_records: List[Dict[str, Any]] = field(default_factory=list)
    wall_anchor_ns: int = 0
    mono_anchor_ns: int = 0
    db_query_count: int = 0
    root_category: str = "app"


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
        max_log_records: int = 512,
    ) -> None:
        self._transport = transport
        self._service_name = service_name
        self._environment = environment
        self._sample_rate = sample_rate
        self._max_spans = max_spans
        self._max_log_records = max(1, int(max_log_records))
        self._log_exporter: Optional[Callable[[Sequence[Dict[str, Any]]], None]] = None

    @property
    def transport(self) -> Transport:
        """The configured transport, exposed for diagnostics and graceful shutdown."""
        return self._transport

    # -- state access ------------------------------------------------------ #
    def _state(self) -> Optional[_RequestState]:
        return _current.get()

    def is_sampled(self) -> bool:
        state = self._state()
        return bool(state and state.enabled and state.sampled)

    def trace_id(self) -> str:
        state = self._state()
        return state.trace_id if state else ""

    def current_trace_id(self) -> str:
        """Return the ambient trace id, including for an unsampled trace."""
        state = self._state()
        return state.trace_id if state and state.enabled else ""

    def root_span(self) -> Optional[Span]:
        state = self._state()
        return state.root_span if state else None

    def root_span_id(self) -> Optional[str]:
        state = self._state()
        if state is None or not state.enabled:
            return None
        return state.root_span_id or None

    def current_span_id(self) -> Optional[str]:
        """Return the innermost live span id available to native log hooks.

        Python integrations currently record child operations post-hoc, so the
        active root is the finest live scope available at log-record time.
        """
        return self.root_span_id()

    def current_trace_flags(self) -> Optional[int]:
        """Return the ambient W3C flags value, or ``None`` outside a trace."""
        state = self._state()
        if state is None or not state.enabled:
            return None
        return 1 if state.sampled else 0

    def sampled(self) -> bool:
        state = self._state()
        return bool(state and state.sampled)

    def reset(self) -> None:
        """Clear per-request state for the current context."""
        _current.set(None)

    def set_log_exporter(
        self,
        exporter: Optional[Callable[[Sequence[Dict[str, Any]]], None]],
    ) -> None:
        """Register the native-log drain owned by the active logging handler."""
        self._log_exporter = exporter

    def clear_log_exporter(
        self,
        exporter: Callable[[Sequence[Dict[str, Any]]], None],
    ) -> None:
        if self._log_exporter is exporter:
            self._log_exporter = None

    def buffer_log(self, record: Dict[str, Any]) -> bool:
        """Buffer a log in the ambient unit, returning whether it was handled.

        Reaching the cap triggers a non-blocking size-threshold export through
        the same bounded transport. Normal request/job completion drains a
        smaller remainder after the unit finishes.
        """
        state = self._state()
        if state is None or not state.enabled or self._log_exporter is None:
            return False
        state.log_records.append(record)
        if len(state.log_records) >= self._max_log_records:
            self._flush_logs(state)
        return True

    def flush_logs(self) -> None:
        """Non-blockingly hand ambient buffered logs to the transport."""
        state = self._state()
        if state is not None:
            self._flush_logs(state)

    # -- lifecycle --------------------------------------------------------- #
    def start_server_span(self, name: str, traceparent: Optional[str] = None) -> None:
        """Open the root SERVER span at request start.

        Continues an incoming W3C ``traceparent`` if present, otherwise mints a
        fresh trace id. The sampling decision is HEAD-BASED and made exactly once
        here, keyed off the trace id, so every span in the trace shares its fate.
        """
        self.start_root_span(name, KIND_SERVER, "app", traceparent)

    def start_root_span(
        self,
        name: str,
        kind: int,
        category: str,
        traceparent: Optional[str] = None,
        link_parent: bool = False,
    ) -> None:
        """Open an HTTP or background root with shared propagation and sampling."""
        state = _RequestState(root_category=category)
        state.enabled = True

        incoming = ids.parse_traceparent(traceparent)
        if incoming is not None:
            state.trace_id = incoming.trace_id
            state.root_parent_span_id = incoming.parent_span_id
            # Honor the upstream sampled bit EXACTLY -- the decision is made once,
            # by whoever started the trace. Re-rolling locally would let this
            # service drop a trace its caller kept, tearing distributed traces in
            # half whenever ``sample_rate`` is below 1.0.
            state.sampled = incoming.sampled
        else:
            state.trace_id = ids.trace_id()
            state.root_parent_span_id = None
            state.sampled = self._sample_decision(state.trace_id)

        # A non-recording (unsampled) span still has a valid SpanContext. Keeping
        # its id lets ERROR logs remain correlated even when the trace itself is
        # intentionally absent.
        state.root_span_id = ids.span_id()

        # Anchor wall-clock <-> monotonic clocks together.
        state.wall_anchor_ns = time.time_ns()
        state.mono_anchor_ns = time.monotonic_ns()

        _current.set(state)

        if not state.sampled:
            return  # not sampled: stay cheap, record nothing

        now = self._now_ns(state)
        state.root_span = Span(
            trace_id=state.trace_id,
            span_id=state.root_span_id,
            parent_span_id=state.root_parent_span_id,
            name=name,
            kind=kind,
            start_unix_nano=now,
            end_unix_nano=now,
        )
        state.root_span.set_string("restlytics.category", category)
        if link_parent and state.root_parent_span_id:
            state.root_span.add_link(state.trace_id, state.root_parent_span_id)

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

    def start_child_span(
        self,
        name: str,
        category: str,
        kind: int = KIND_CLIENT,
        span_id: Optional[str] = None,
    ) -> Optional[Span]:
        state = self._state()
        if state is None or not (state.enabled and state.sampled) or state.root_span is None:
            return None
        if len(state.spans) >= self._max_spans:
            return None
        now = self._now_ns(state)
        span = Span(
            trace_id=state.trace_id,
            span_id=span_id or ids.span_id(),
            parent_span_id=state.root_span.span_id,
            name=name,
            kind=kind,
            start_unix_nano=now,
            end_unix_nano=now,
        )
        span.set_string("restlytics.category", category)
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
        self.finish_root_span()

    def finish_root_span(self, failed: bool = False) -> None:
        """Finish any root span; failure status never includes exception content."""
        state = self._state()
        if state is None or not state.enabled:
            self.reset()
            return
        try:
            if state.sampled and state.root_span is not None:
                state.root_span.set_end(self._now_ns(state))

                self._attach_self_time(state)
                state.root_span.set_int("restlytics.db_query_count", state.db_query_count)
                state.root_span.set_string("restlytics.category", state.root_category)
                if failed:
                    state.root_span.set_status(STATUS_ERROR)
                elif state.root_span.status_code() == STATUS_UNSET:
                    state.root_span.set_status(STATUS_OK)

                self._flush(state)
            # Logs are an independent signal and must drain even when this trace
            # was not sampled.
            self._flush_logs(state)
        finally:
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

    def _flush_logs(self, state: _RequestState) -> None:
        if not state.log_records:
            return
        records = list(state.log_records)
        state.log_records.clear()
        exporter = self._log_exporter
        if exporter is None:
            return
        try:
            exporter(records)
        except BaseException:
            # Native logging must remain outside the host failure domain.
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
        self_queue = union_length(by_cat["queue"])
        # app self-time = explicit app-category child time + the root's own
        # exclusive (uncovered) time. Mirrors the ingestion service's computation.
        self_app = union_length(by_cat["app"]) + max(0, root_dur - union_length(all_intervals))

        root.set_int("restlytics.self_ns.db", self_db)
        root.set_int("restlytics.self_ns.http", self_http)
        root.set_int("restlytics.self_ns.cache", self_cache)
        root.set_int("restlytics.self_ns.queue", self_queue)
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
