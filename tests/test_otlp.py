"""OTLP wire-format tests -- the three classic footguns + end-to-end tracer flush.

Pure stdlib (uses the NullTransport/LogTransport), no network or framework.
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from restlytics.otlp import (  # noqa: E402
    KIND_CLIENT,
    KIND_SERVER,
    Span,
    build_payload,
    int_value,
)
from restlytics.tracer import Tracer  # noqa: E402
from restlytics.transport import LogTransport  # noqa: E402

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_ALL_ZERO = re.compile(r"^0+$")


class OtlpSpanTest(unittest.TestCase):
    def test_int_value_is_a_string(self):
        self.assertEqual({"intValue": "200"}, int_value(200))

    def test_unix_nano_fields_are_strings(self):
        span = Span("a" * 32, "b" * 16, None, "GET /", KIND_SERVER, 1700, 1800)
        out = span.to_otlp()
        self.assertIsInstance(out["startTimeUnixNano"], str)
        self.assertIsInstance(out["endTimeUnixNano"], str)
        self.assertEqual("1700", out["startTimeUnixNano"])
        self.assertEqual("1800", out["endTimeUnixNano"])

    def test_root_omits_empty_parent_span_id(self):
        span = Span("a" * 32, "b" * 16, None, "GET /", KIND_SERVER, 1, 2)
        self.assertNotIn("parentSpanId", span.to_otlp())
        span2 = Span("a" * 32, "b" * 16, "", "GET /", KIND_SERVER, 1, 2)
        self.assertNotIn("parentSpanId", span2.to_otlp())

    def test_child_keeps_parent_span_id(self):
        span = Span("a" * 32, "c" * 16, "b" * 16, "db", KIND_CLIENT, 1, 2)
        self.assertEqual("b" * 16, span.to_otlp()["parentSpanId"])

    def test_int_attribute_serializes_as_string_intvalue(self):
        span = Span("a" * 32, "b" * 16, None, "GET /", KIND_SERVER, 1, 2)
        span.set_int("http.response.status_code", 200)
        attrs = {kv["key"]: kv["value"] for kv in span.to_otlp()["attributes"]}
        self.assertEqual({"intValue": "200"}, attrs["http.response.status_code"])

    def test_status_only_present_when_set(self):
        span = Span("a" * 32, "b" * 16, None, "GET /", KIND_SERVER, 1, 2)
        self.assertNotIn("status", span.to_otlp())
        span.set_status(2, "boom")
        self.assertEqual({"code": 2, "message": "boom"}, span.to_otlp()["status"])


class PayloadShapeTest(unittest.TestCase):
    def test_envelope_shape_matches_contract(self):
        span = Span("a" * 32, "b" * 16, None, "GET /", KIND_SERVER, 1, 2)
        payload = build_payload("svc", "production", [span])
        # JSON round-trips (no non-serializable values).
        json.dumps(payload)

        rs = payload["resourceSpans"][0]
        res_attrs = {kv["key"]: kv["value"]["stringValue"] for kv in rs["resource"]["attributes"]}
        self.assertEqual("svc", res_attrs["service.name"])
        self.assertEqual("production", res_attrs["deployment.environment"])
        self.assertEqual("restlytics-python", res_attrs["telemetry.sdk.name"])
        self.assertEqual("python", res_attrs["telemetry.sdk.language"])

        scope = rs["scopeSpans"][0]["scope"]
        self.assertEqual("restlytics-python", scope["name"])
        self.assertEqual(span.to_otlp(), rs["scopeSpans"][0]["spans"][0])


class TracerFlushTest(unittest.TestCase):
    def _tracer(self):
        transport = LogTransport()
        return Tracer(transport, "svc", "production", sample_rate=1.0), transport

    def test_end_to_end_flush_produces_valid_ids_and_self_time(self):
        tracer, transport = self._tracer()
        tracer.start_server_span("GET /users/{id}")
        self.assertTrue(tracer.is_sampled())

        now = tracer.now_ns()
        # A DB child and an HTTP child.
        db = tracer.add_child_span("db", now, now + 1000, kind=KIND_CLIENT)
        db.set_string("restlytics.category", "db")
        tracer.increment_db_query_count()
        http = tracer.add_child_span("http", now + 500, now + 1500, kind=KIND_CLIENT)
        http.set_string("restlytics.category", "http")

        tracer.finish_server_span()

        self.assertEqual(1, len(transport.payloads))
        spans = transport.payloads[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        # root + 2 children.
        self.assertEqual(3, len(spans))

        root = spans[0]
        self.assertTrue(_HEX32.match(root["traceId"]))
        self.assertFalse(_ALL_ZERO.match(root["traceId"]))
        self.assertTrue(_HEX16.match(root["spanId"]))
        self.assertEqual(KIND_SERVER, root["kind"])
        self.assertNotIn("parentSpanId", root)

        attrs = {kv["key"]: kv["value"] for kv in root["attributes"]}
        self.assertIn("restlytics.self_ns.db", attrs)
        self.assertIn("restlytics.self_ns.http", attrs)
        self.assertIn("restlytics.self_ns.app", attrs)
        self.assertEqual({"intValue": "1"}, attrs["restlytics.db_query_count"])

        for child in spans[1:]:
            self.assertEqual(root["spanId"], child["parentSpanId"])
            self.assertEqual(KIND_CLIENT, child["kind"])
            self.assertEqual(root["traceId"], child["traceId"])

    def test_not_sampled_emits_nothing(self):
        transport = LogTransport()
        tracer = Tracer(transport, "svc", "production", sample_rate=0.0)
        tracer.start_server_span("GET /")
        self.assertFalse(tracer.is_sampled())
        self.assertIsNone(tracer.add_child_span("db", 0, 1))
        tracer.finish_server_span()
        self.assertEqual([], transport.payloads)

    def test_continues_incoming_traceparent(self):
        tracer, transport = self._tracer()
        tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        tracer.start_server_span("GET /", traceparent=tp)
        self.assertEqual("4bf92f3577b34da6a3ce929d0e0e4736", tracer.trace_id())
        root = tracer.root_span()
        self.assertEqual("00f067aa0ba902b7", root.parent_span_id)
        tracer.finish_server_span()

    def test_buffer_cap_enforced(self):
        transport = LogTransport()
        tracer = Tracer(transport, "svc", "production", sample_rate=1.0, max_spans=3)
        tracer.start_server_span("GET /")
        now = tracer.now_ns()
        added = [tracer.add_child_span("db", now, now + 1) for _ in range(5)]
        self.assertEqual(3, sum(1 for s in added if s is not None))


if __name__ == "__main__":
    unittest.main()
