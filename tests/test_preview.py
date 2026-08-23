"""Local-only telemetry preview contract."""

import json
import unittest

import restlytics
from restlytics.otlp import KIND_SERVER, Span, build_payload
from restlytics.transport import PreviewTransport


class PreviewTest(unittest.TestCase):
    def test_reports_redacted_payload_and_sizes_without_networking(self):
        output = []
        transport = PreviewTransport(0.25, output.append)
        span = Span("a" * 32, "b" * 16, None, "GET /users/{id}", KIND_SERVER, 1, 2)
        span.set_string("url.full", "https://user:secret@example.test/users/1?token=secret")
        span.set_string("http.request.body", "do-not-export")
        transport.send(build_payload("preview-app", "production", [span]))

        self.assertEqual(len(transport.reports), 1)
        report = transport.reports[0]
        self.assertFalse(report["networkRequestMade"])
        self.assertEqual(report["configuredSampleRate"], 0.25)
        self.assertEqual(report["spanCount"], 1)
        self.assertGreater(report["jsonBytes"], report["gzipBytes"])
        encoded = json.dumps(report)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("do-not-export", encoded)
        self.assertIn("REDACTED", encoded)

    def test_preview_works_without_an_ingest_key(self):
        tracer = restlytics.init(key="", transport="preview", sample_rate=1.0)
        self.assertIsInstance(tracer.transport, PreviewTransport)


if __name__ == "__main__":
    unittest.main()
