"""Framework-friendly context managers for jobs, commands, schedules, and enqueue I/O."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, MutableMapping, Optional, TypeVar

from . import ids
from .otlp import KIND_CLIENT, KIND_CONSUMER, KIND_SERVER, STATUS_ERROR
from .tracer import Tracer

T = TypeVar("T")


def _stable(value: str, fallback: str) -> str:
    cleaned = "".join(char for char in str(value) if ord(char) >= 32 and ord(char) != 127).strip()
    return cleaned[:200] or fallback


def _tracer(value: Optional[Tracer]) -> Tracer:
    if value is not None:
        return value
    from . import get_tracer

    return get_tracer()


@dataclass
class CommandExecution:
    tracer: Tracer

    def set_exit_code(self, value: int) -> None:
        root = self.tracer.root_span()
        code = int(value)
        if root is not None:
            root.set_int("restlytics.command.exit_code", code)
            if code != 0:
                root.set_status(STATUS_ERROR)


@contextmanager
def job(
    name: str,
    *,
    system: str,
    destination: str,
    attempt: int = 1,
    max_attempts: Optional[int] = None,
    enqueued_ns: Optional[int] = None,
    message_id: Optional[str] = None,
    traceparent: Optional[str] = None,
    tracer: Optional[Tracer] = None,
) -> Iterator[None]:
    active = _tracer(tracer)
    work_name = _stable(name, "unnamed-job")
    active.start_root_span(work_name, KIND_CONSUMER, "job", traceparent, link_parent=True)
    root = active.root_span()
    if root is not None:
        root.set_string("restlytics.work.name", work_name)
        root.set_string("restlytics.job.name", work_name)
        root.set_string("messaging.system", _stable(system, "unknown"))
        root.set_string("messaging.destination.name", _stable(destination, "unknown"))
        root.set_string("messaging.operation.type", "process")
        root.set_int("restlytics.job.attempt", max(1, int(attempt)))
        if max_attempts is not None:
            root.set_int("restlytics.job.max_attempts", max(1, int(max_attempts)))
        if enqueued_ns is not None:
            root.set_int("restlytics.job.enqueued_ns", int(enqueued_ns))
        if message_id:
            root.set_string("messaging.message.id", _stable(message_id, "unknown"))
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        active.finish_root_span(failed=failed)


@contextmanager
def command(
    name: str,
    *,
    traceparent: Optional[str] = None,
    tracer: Optional[Tracer] = None,
) -> Iterator[CommandExecution]:
    active = _tracer(tracer)
    work_name = _stable(name, "unnamed-command")
    active.start_root_span(work_name, KIND_SERVER, "command", traceparent)
    root = active.root_span()
    if root is not None:
        root.set_string("restlytics.work.name", work_name)
        root.set_string("restlytics.command.name", work_name)
        root.set_int("restlytics.command.exit_code", 0)
    execution = CommandExecution(active)
    failed = False
    try:
        yield execution
    except BaseException:
        failed = True
        raise
    finally:
        active.finish_root_span(failed=failed)


@contextmanager
def schedule(
    name: str,
    *,
    cron: str,
    traceparent: Optional[str] = None,
    tracer: Optional[Tracer] = None,
) -> Iterator[None]:
    active = _tracer(tracer)
    work_name = _stable(name, "unnamed-schedule")
    active.start_root_span(work_name, KIND_SERVER, "schedule", traceparent)
    root = active.root_span()
    if root is not None:
        root.set_string("restlytics.work.name", work_name)
        root.set_string("restlytics.schedule.name", work_name)
        root.set_string("restlytics.schedule.cron", _stable(cron, "unknown"))
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        active.finish_root_span(failed=failed)


@contextmanager
def enqueue(
    carrier: MutableMapping[str, Any],
    *,
    system: str,
    destination: str,
    tracestate: Optional[str] = None,
    tracer: Optional[Tracer] = None,
) -> Iterator[MutableMapping[str, Any]]:
    active = _tracer(tracer)
    trace_id = active.trace_id()
    enqueue_span_id = ids.span_id()
    if trace_id:
        envelope: Dict[str, str] = {
            "traceparent": ids.format_traceparent(trace_id, enqueue_span_id, active.sampled())
        }
        if tracestate and tracestate.strip():
            envelope["tracestate"] = tracestate.strip()[:512]
        carrier["__restlytics"] = envelope
    span = active.start_child_span(
        "send {0}".format(_stable(destination, "unknown")),
        "queue",
        KIND_CLIENT,
        enqueue_span_id,
    )
    if span is not None:
        span.set_string("messaging.system", _stable(system, "unknown"))
        span.set_string("messaging.destination.name", _stable(destination, "unknown"))
        span.set_string("messaging.operation.type", "send")
    try:
        yield carrier
    except BaseException:
        if span is not None:
            span.set_status(STATUS_ERROR)
        raise
    finally:
        if span is not None:
            span.set_end(active.now_ns())
