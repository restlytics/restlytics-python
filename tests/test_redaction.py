"""Privacy boundary tests shared conceptually with every native SDK."""

import json
import unittest

from restlytics.otlp import KIND_SERVER, STATUS_ERROR, Span
from restlytics.redact import is_sensitive_attribute_key, redact_url


class RedactionTest(unittest.TestCase):
    def test_url_removes_credentials_fragments_and_every_query_value(self):
        value = redact_url(
            "https://alice:password@example.test/orders?token=abc&unknown=customer-secret#raw",
            ["token"],
        )
        for secret in ("alice", "password", "abc", "customer-secret", "raw"):
            self.assertNotIn(secret, value)

    def test_span_boundary_drops_content_bearing_fields(self):
        span = Span("a" * 32, "b" * 16, None, "GET /users/{id}", KIND_SERVER, 1, 2)
        span.set_string("http.request.method", "GET")
        span.set_string("http.request.header.authorization", "Bearer abc.def.ghi")
        span.set_string("django.request.body", "password=hunter2")
        span.set_string("log.body", "alice@example.test")
        span.set_string("url.full", "https://example.test/?unknown=customer-secret")
        span.set_status(STATUS_ERROR, "login failed for alice@example.test password=hunter2")

        payload = span.to_otlp()
        encoded = json.dumps(payload)
        for secret in ("hunter2", "alice@example.test", "customer-secret", "authorization"):
            self.assertNotIn(secret, encoded)
        self.assertNotIn("message", payload["status"])
        self.assertTrue(is_sensitive_attribute_key("fastapi.request.payload"))
        self.assertFalse(is_sensitive_attribute_key("restlytics.bindings_count"))


if __name__ == "__main__":
    unittest.main()
