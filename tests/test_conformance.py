"""Shared cross-language OTLP and trace-behavior conformance fixture."""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from restlytics.ids import parse_traceparent  # noqa: E402
from restlytics.otlp import (  # noqa: E402
    SDK_LANGUAGE,
    SDK_NAME,
    SDK_VERSION,
    Span,
    build_payload,
)
from restlytics.tracer import Tracer  # noqa: E402
from restlytics.transport import NullTransport  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "v1"


def load_properties():
    values = {}
    for line in (FIXTURES / "vectors.properties").read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        values[key] = value
    return values


class ConformanceTest(unittest.TestCase):
    def test_shared_otlp_propagation_redaction_error_and_sampling_fixture(self):
        fixture = load_properties()
        span = Span(
            fixture["trace.id"],
            fixture["span.id"],
            fixture["span.parent_id"],
            fixture["span.name"],
            int(fixture["span.kind"]),
            int(fixture["span.start_ns"]),
            int(fixture["span.end_ns"]),
        )
        span.set_string(fixture["attribute.string.key"], fixture["attribute.string.value"])
        span.set_int(fixture["attribute.int.key"], int(fixture["attribute.int.value"]))
        span.set_bool(fixture["attribute.bool.key"], fixture["attribute.bool.value"] == "true")
        span.set_string(fixture["redaction.attribute_key"], fixture["redaction.attribute_value"])
        span.set_status(int(fixture["error.status_code"]), fixture["error.message"])

        expected_text = (FIXTURES / "otlp.expected.json").read_text(encoding="utf-8")
        expected_text = expected_text.replace("${SDK_NAME}", SDK_NAME)
        expected_text = expected_text.replace("${SDK_LANGUAGE}", SDK_LANGUAGE)
        expected_text = expected_text.replace("${SDK_VERSION}", SDK_VERSION)
        self.assertEqual(
            json.loads(expected_text),
            build_payload(fixture["service.name"], fixture["deployment.environment"], [span]),
        )

        sampled = parse_traceparent(fixture["propagation.sampled"])
        self.assertEqual(fixture["trace.id"], sampled.trace_id)
        self.assertEqual(fixture["span.id"], sampled.parent_span_id)
        self.assertTrue(sampled.sampled)
        self.assertFalse(parse_traceparent(fixture["propagation.unsampled"]).sampled)
        self.assertIsNone(parse_traceparent(fixture["propagation.invalid"]))

        zero = Tracer(
            NullTransport(),
            "fixture",
            "fixture",
            sample_rate=float(fixture["sampling.root_rate_zero"]),
        )
        zero.start_server_span("fixture")
        self.assertFalse(zero.is_sampled())
        zero.reset()
        one = Tracer(
            NullTransport(),
            "fixture",
            "fixture",
            sample_rate=float(fixture["sampling.root_rate_one"]),
        )
        one.start_server_span("fixture")
        self.assertTrue(one.is_sampled())
        one.reset()


if __name__ == "__main__":
    unittest.main()
