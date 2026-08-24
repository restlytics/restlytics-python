"""Native Python logging export as trace-correlated OTLP/JSON logs.

The integration is opt-in and deliberately captures a very small surface:
source-redacted message text, deterministic severity, logger name, event time,
and the ambient Restlytics trace/span context. ``args``, arbitrary extras,
exception text/stacks, paths, process arguments, and thread data are never
serialized.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, Optional, Sequence, Tuple

from .config import DEFAULT_LOGS_MIN_SEVERITY
from .otlp import SDK_NAME, SDK_VERSION, key_value, resource_attributes, string_value
from .redact import redact_log_message
from .transport import Transport, is_reporting_transport_diagnostic

# Python stdlib levels have fewer states than OTel. Custom numeric levels are
# assigned to the nearest standard Python level; ties choose the more severe
# bucket. This makes mapping stable for every integer without inventing a second
# sampling/filter scale.
_SEVERITY_BUCKETS = (
    (logging.DEBUG, 5, "DEBUG"),
    (logging.INFO, 9, "INFO"),
    (logging.WARNING, 13, "WARN"),
    (logging.ERROR, 17, "ERROR"),
    (logging.CRITICAL, 18, "ERROR2"),
)
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def map_severity(level: int) -> Tuple[int, str]:
    """Map a Python logging level to one deterministic OTel severity pair."""
    try:
        numeric = int(level)
    except (TypeError, ValueError, OverflowError):
        return 0, "UNSPECIFIED"
    if numeric <= logging.NOTSET:
        return 0, "UNSPECIFIED"
    _host_level, severity_number, severity_text = min(
        _SEVERITY_BUCKETS,
        key=lambda bucket: (abs(numeric - bucket[0]), -bucket[0]),
    )
    return severity_number, severity_text


def build_log_record(
    record: logging.LogRecord,
    tracer: Any,
    *,
    observed_time_ns: Optional[int] = None,
) -> Dict[str, Any]:
    """Convert one stdlib ``LogRecord`` at the source redaction boundary."""
    severity_number, severity_text = map_severity(getattr(record, "levelno", 0))
    event_time_ns = _event_time_ns(record)
    observed_ns = max(0, int(observed_time_ns if observed_time_ns is not None else time.time_ns()))

    log_record: Dict[str, Any] = {
        "timeUnixNano": str(event_time_ns),
        "observedTimeUnixNano": str(observed_ns),
        "severityNumber": severity_number,
        "severityText": severity_text,
        "body": string_value(_safe_message(record)),
    }

    # Arbitrary LogRecord extras are intentionally ignored. Logger identity is
    # the sole structured field and is scrubbed/capped like all source content.
    logger_name = redact_log_message(str(getattr(record, "name", "root")), max_chars=200)
    if logger_name:
        log_record["attributes"] = [key_value("logger.name", string_value(logger_name))]

    trace_id, span_id, flags = _ambient_context(tracer)
    if trace_id:
        log_record["traceId"] = trace_id
        log_record["flags"] = flags
        if span_id:
            log_record["spanId"] = span_id
    return log_record


def build_logs_payload(
    service_name: str,
    environment: str,
    records: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build an OTLP ``ExportLogsServiceRequest`` using trace-identical resource data."""
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": resource_attributes(service_name, environment),
                },
                "scopeLogs": [
                    {
                        "scope": {"name": SDK_NAME, "version": SDK_VERSION},
                        "logRecords": list(records),
                    }
                ],
            }
        ]
    }


class RestlyticsLogHandler(logging.Handler):
    """A never-raising stdlib handler backed by the SDK's bounded transport queue.

    Each accepted record becomes one small OTLP batch and is non-blockingly
    enqueued on ``HttpTransport``'s fixed-capacity worker. :meth:`flush` drains
    accepted trace and log work within the configured deadline.
    """

    def __init__(
        self,
        transport: Transport,
        tracer: Any,
        service_name: str,
        environment: str,
        *,
        min_severity: int = DEFAULT_LOGS_MIN_SEVERITY,
        flush_timeout_ms: int = 2000,
    ) -> None:
        super().__init__(level=logging.NOTSET)
        self._transport = transport
        self._tracer = tracer
        self._service_name = str(service_name)
        self._environment = str(environment)
        self._min_severity = _clamp_severity(min_severity)
        self._flush_timeout_ms = max(0, int(flush_timeout_ms))
        self._local = threading.local()
        self._restlytics_closed = False
        self._exporter = self._export_records
        try:
            registrar = getattr(self._tracer, "set_log_exporter", None)
            if callable(registrar):
                registrar(self._exporter)
        except BaseException:
            pass

    @property
    def min_severity(self) -> int:
        return self._min_severity

    def handle(self, record: logging.LogRecord) -> bool:
        """Run filters and emission without allowing user logging to fail."""
        try:
            return bool(super().handle(record))
        except BaseException:
            return False

    def emit(self, record: logging.LogRecord) -> None:
        if (
            self._restlytics_closed
            or getattr(self._local, "emitting", False)
            or is_reporting_transport_diagnostic()
        ):
            return
        try:
            self._local.emitting = True
            severity_number, _severity_text = map_severity(getattr(record, "levelno", 0))
            if severity_number < self._min_severity:
                return
            otlp_record = build_log_record(record, self._tracer)
            buffer_log = getattr(self._tracer, "buffer_log", None)
            if callable(buffer_log) and buffer_log(otlp_record):
                return
            self._export_records([otlp_record])
        except BaseException:
            # Formatting, redaction, payload construction, and custom transports
            # are all outside the host application's failure domain.
            return
        finally:
            self._local.emitting = False

    def flush(self) -> None:
        try:
            flush_logs = getattr(self._tracer, "flush_logs", None)
            if callable(flush_logs):
                flush_logs()
            self._transport.flush(self._flush_timeout_ms)
        except BaseException:
            return

    def close(self) -> None:
        if self._restlytics_closed:
            return
        try:
            self.flush()
        finally:
            self._restlytics_closed = True
            try:
                clearer = getattr(self._tracer, "clear_log_exporter", None)
                if callable(clearer):
                    clearer(self._exporter)
            except BaseException:
                pass
            try:
                super().close()
            except BaseException:
                pass

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        """Never write handler failures to stderr or propagate them."""
        del record

    def _export_records(self, records: Sequence[Dict[str, Any]]) -> None:
        if self._restlytics_closed or not records:
            return
        payload = build_logs_payload(
            self._service_name,
            self._environment,
            records,
        )
        self._transport.send_logs(payload)


def _safe_message(record: logging.LogRecord) -> str:
    # An attached exception/stack may contain request data, credentials, and
    # bindings. Do not even interpolate the source message on that path.
    if (
        getattr(record, "exc_info", None) is not None
        or getattr(record, "exc_text", None) is not None
        or getattr(record, "stack_info", None) is not None
    ):
        return "[EXCEPTION REDACTED]"
    try:
        message = record.getMessage()
    except BaseException:
        return "[REDACTED]"
    return redact_log_message(message)


def _event_time_ns(record: logging.LogRecord) -> int:
    try:
        created = float(record.created)
        if created < 0 or created != created:  # negative or NaN
            return 0
        return max(0, int(created * 1_000_000_000))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0


def _ambient_context(tracer: Any) -> Tuple[str, str, int]:
    try:
        trace_id = str(tracer.current_trace_id() or "")
        span_id = str(tracer.current_span_id() or "")
        flags_value = tracer.current_trace_flags()
        flags = int(flags_value) if flags_value is not None else 0
    except BaseException:
        return "", "", 0
    if not _TRACE_ID_RE.match(trace_id) or not trace_id.strip("0"):
        return "", "", 0
    if not _SPAN_ID_RE.match(span_id) or not span_id.strip("0"):
        span_id = ""
    return trace_id, span_id, max(0, flags)


def _clamp_severity(value: int) -> int:
    try:
        return min(24, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_LOGS_MIN_SEVERITY


# Short alias for callers that prefer the host framework's naming convention.
LoggingHandler = RestlyticsLogHandler
