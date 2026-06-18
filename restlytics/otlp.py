"""OTLP/JSON span model + ``ExportTraceServiceRequest`` payload builder.

The shape here MUST match ``packages/contract`` (``otlp.ts`` /
``attributes.ts``) exactly. The three classic footguns are all handled in this
module:

  * trace/span ids are lowercase hex of the right length and never all-zero
    (enforced in :mod:`restlytics.ids`),
  * ``*UnixNano`` fields are decimal **strings** (int64-safe in JSON),
  * ``intValue`` is a **string**, not a JSON number.

Attribute values are stored as raw Python scalars and converted to the OTLP
AnyValue wrapper at serialization time. Keys recorded via :meth:`Span.set_int`
are forced to ``intValue`` so a value like ``200`` is never emitted as a double.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# Stable identifiers for the SDK, surfaced as resource attributes and the scope
# name. ``telemetry.sdk.name`` is ``restlytics-<lang>`` per SPEC section 4.
SDK_NAME = "restlytics-python"
SDK_LANGUAGE = "python"
SDK_VERSION = "0.1.0"

# OTLP SpanKind enum values we use.
KIND_INTERNAL = 1
KIND_SERVER = 2
KIND_CLIENT = 3

# OTLP status codes.
STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2


# --------------------------------------------------------------------------- #
# AnyValue helpers (exactly one field set; intValue is a string).
# --------------------------------------------------------------------------- #
def string_value(value: str) -> Dict[str, Any]:
    return {"stringValue": str(value)}


def int_value(value: int) -> Dict[str, Any]:
    # CONTRACT: intValue is a STRING, not a JSON number.
    return {"intValue": str(int(value))}


def bool_value(value: bool) -> Dict[str, Any]:
    return {"boolValue": bool(value)}


def double_value(value: float) -> Dict[str, Any]:
    return {"doubleValue": float(value)}


def key_value(key: str, value: Dict[str, Any]) -> Dict[str, Any]:
    return {"key": key, "value": value}


class Span:
    """A single span, accumulated in-request and serialized to OTLP/JSON on flush.

    Timestamps are kept as integer nanoseconds internally and only stringified at
    serialization time (the OTLP/JSON contract requires ``*UnixNano`` to be
    decimal strings). Attribute values are raw scalars converted to AnyValue at
    serialization; ``int`` keys are tracked so they serialize as ``intValue``.
    """

    __slots__ = (
        "trace_id",
        "span_id",
        "parent_span_id",
        "name",
        "kind",
        "start_unix_nano",
        "end_unix_nano",
        "_attributes",
        "_int_keys",
        "_status_code",
        "_status_message",
    )

    def __init__(
        self,
        trace_id: str,
        span_id: str,
        parent_span_id: Optional[str],
        name: str,
        kind: int,
        start_unix_nano: int,
        end_unix_nano: int,
    ) -> None:
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.name = name
        self.kind = kind
        self.start_unix_nano = start_unix_nano
        self.end_unix_nano = end_unix_nano
        # Insertion-ordered dict (Python 3.7+) keeps attribute order stable.
        self._attributes: "Dict[str, Any]" = {}
        self._int_keys: "Dict[str, bool]" = {}
        self._status_code = STATUS_UNSET
        self._status_message: Optional[str] = None

    # -- mutators ---------------------------------------------------------- #
    def set_name(self, name: str) -> "Span":
        self.name = name
        return self

    def set_end(self, end_unix_nano: int) -> "Span":
        self.end_unix_nano = end_unix_nano
        return self

    def set_string(self, key: str, value: str) -> "Span":
        self._attributes[key] = str(value)
        self._int_keys.pop(key, None)
        return self

    def set_int(self, key: str, value: int) -> "Span":
        """Record an int attribute. Serialized as ``intValue`` (a STRING)."""
        self._attributes[key] = int(value)
        self._int_keys[key] = True
        return self

    def set_double(self, key: str, value: float) -> "Span":
        self._attributes[key] = float(value)
        self._int_keys.pop(key, None)
        return self

    def set_bool(self, key: str, value: bool) -> "Span":
        self._attributes[key] = bool(value)
        self._int_keys.pop(key, None)
        return self

    def set_status(self, code: int, message: Optional[str] = None) -> "Span":
        self._status_code = code
        if message is not None:
            # Cap to keep payloads bounded; full stack traces don't belong on the wire.
            self._status_message = message[:1024]
        return self

    # -- accessors --------------------------------------------------------- #
    def status_code(self) -> int:
        return self._status_code

    def get_string(self, key: str) -> Optional[str]:
        val = self._attributes.get(key)
        return val if isinstance(val, str) else None

    def duration_ns(self) -> int:
        """Duration in nanoseconds (clamped non-negative against clock skew)."""
        return max(0, self.end_unix_nano - self.start_unix_nano)

    # -- serialization ----------------------------------------------------- #
    def to_otlp(self) -> Dict[str, Any]:
        """Serialize to the OTLP/JSON Span shape the ingestion contract validates."""
        span: Dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": self.kind,
            # Decimal STRINGS -- int64-safe in JSON.
            "startTimeUnixNano": str(self.start_unix_nano),
            "endTimeUnixNano": str(self.end_unix_nano),
        }

        # parentSpanId is omitted/empty for the root SERVER span.
        if self.parent_span_id:
            span["parentSpanId"] = self.parent_span_id

        if self._attributes:
            span["attributes"] = self._serialize_attributes()

        # Only attach status when it carries signal (OK/ERROR); UNSET is default.
        if self._status_code != STATUS_UNSET:
            status: Dict[str, Any] = {"code": self._status_code}
            if self._status_message:
                status["message"] = self._status_message
            span["status"] = status

        return span

    def _serialize_attributes(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for key, value in self._attributes.items():
            out.append(key_value(key, self._any_value(key, value)))
        return out

    def _any_value(self, key: str, value: Any) -> Dict[str, Any]:
        if self._int_keys.get(key) or (isinstance(value, int) and not isinstance(value, bool)):
            return int_value(int(value))
        if isinstance(value, bool):
            return bool_value(value)
        if isinstance(value, float):
            return double_value(value)
        return string_value(str(value))


def resource_attributes(service_name: str, environment: str) -> List[Dict[str, Any]]:
    """Resource-level KeyValue list: service identity + SDK identity."""
    return [
        key_value("service.name", string_value(service_name)),
        key_value("deployment.environment", string_value(environment)),
        key_value("telemetry.sdk.name", string_value(SDK_NAME)),
        key_value("telemetry.sdk.language", string_value(SDK_LANGUAGE)),
        key_value("telemetry.sdk.version", string_value(SDK_VERSION)),
    ]


def build_payload(
    service_name: str,
    environment: str,
    spans: Sequence[Span],
) -> Dict[str, Any]:
    """Build the top-level OTLP/JSON ``ExportTraceServiceRequest`` body.

    A single ``resourceSpans``/``scopeSpans`` envelope: every span in one request
    shares the same resource.
    """
    otlp_spans = [span.to_otlp() for span in spans]
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": resource_attributes(service_name, environment),
                },
                "scopeSpans": [
                    {
                        "scope": {"name": SDK_NAME, "version": SDK_VERSION},
                        "spans": otlp_spans,
                    }
                ],
            }
        ]
    }
