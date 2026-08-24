"""Native logging signal conformance, safety, and delivery tests."""

import gzip
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import restlytics
from restlytics.config import Config, DEFAULT_LOGS_MIN_SEVERITY
from restlytics.logs import RestlyticsLogHandler, build_logs_payload, map_severity
from restlytics.otlp import build_payload
from restlytics.tracer import Tracer
from restlytics.transport import HttpTransport, LogTransport, Transport


def _record(level, message, *, name="application", exc_info=None):
    return logging.LogRecord(
        name,
        level,
        "/private/source/path.py",
        42,
        message,
        (),
        exc_info,
    )


def _otlp_record(payload):
    return payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]


def test_config_is_opt_in_and_resolves_min_severity(monkeypatch):
    monkeypatch.delenv("RESTLYTICS_LOGS", raising=False)
    monkeypatch.delenv("RESTLYTICS_LOGS_MIN_SEVERITY", raising=False)
    config = Config.from_env()
    assert config.logs is False
    assert config.logs_min_severity == DEFAULT_LOGS_MIN_SEVERITY == 13

    monkeypatch.setenv("RESTLYTICS_LOGS", "true")
    monkeypatch.setenv("RESTLYTICS_LOGS_MIN_SEVERITY", "17")
    config = Config.from_env()
    assert config.logs is True
    assert config.logs_min_severity == 17


def test_severity_mapping_is_exact_and_custom_levels_are_deterministic():
    assert map_severity(logging.DEBUG) == (5, "DEBUG")
    assert map_severity(logging.INFO) == (9, "INFO")
    assert map_severity(logging.WARNING) == (13, "WARN")
    assert map_severity(logging.ERROR) == (17, "ERROR")
    assert map_severity(logging.CRITICAL) == (18, "ERROR2")
    assert map_severity(logging.NOTSET) == (0, "UNSPECIFIED")

    # Midpoint ties consistently choose the more severe standard bucket.
    assert map_severity(15) == (9, "INFO")
    assert map_severity(25) == (13, "WARN")
    assert map_severity(35) == (17, "ERROR")
    assert map_severity(45) == (18, "ERROR2")


def test_handler_exports_otlp_logs_and_drops_below_threshold():
    transport = LogTransport()
    tracer = Tracer(transport, "checkout", "test")
    handler = RestlyticsLogHandler(transport, tracer, "checkout", "test")

    assert handler.handle(_record(logging.INFO, "routine detail"))
    assert transport.log_payloads == []

    assert handler.handle(_record(logging.WARNING, "inventory is low"))
    assert len(transport.log_payloads) == 1
    payload = transport.log_payloads[0]
    record = _otlp_record(payload)
    assert record["severityNumber"] == 13
    assert record["severityText"] == "WARN"
    assert record["body"] == {"stringValue": "inventory is low"}
    assert record["attributes"] == [
        {"key": "logger.name", "value": {"stringValue": "application"}}
    ]
    assert isinstance(record["timeUnixNano"], str)
    assert isinstance(record["observedTimeUnixNano"], str)

    trace_resource = build_payload("checkout", "test", [])["resourceSpans"][0]["resource"]
    logs_resource = payload["resourceLogs"][0]["resource"]
    assert logs_resource == trace_resource


def test_logs_capture_sampled_and_unsampled_context_but_omit_ids_outside_trace():
    transport = LogTransport()
    tracer = Tracer(transport, "worker", "test", sample_rate=1.0)
    handler = RestlyticsLogHandler(transport, tracer, "worker", "test")

    handler.handle(_record(logging.ERROR, "outside"))
    outside = _otlp_record(transport.log_payloads[-1])
    assert "traceId" not in outside
    assert "spanId" not in outside
    assert "flags" not in outside

    tracer.start_server_span("GET /orders/{id}")
    expected_trace_id = tracer.current_trace_id()
    expected_span_id = tracer.current_span_id()
    previous_batches = len(transport.log_payloads)
    handler.handle(_record(logging.ERROR, "inside sampled"))
    assert len(transport.log_payloads) == previous_batches  # buffered until unit completion
    tracer.finish_server_span()
    sampled = _otlp_record(transport.log_payloads[-1])
    assert sampled["traceId"] == expected_trace_id
    assert sampled["spanId"] == expected_span_id
    assert sampled["flags"] == 1

    unsampled_tracer = Tracer(transport, "worker", "test", sample_rate=0.0)
    unsampled_handler = RestlyticsLogHandler(transport, unsampled_tracer, "worker", "test")
    unsampled_tracer.start_server_span("GET /orders/{id}")
    expected_trace_id = unsampled_tracer.current_trace_id()
    expected_span_id = unsampled_tracer.current_span_id()
    previous_batches = len(transport.log_payloads)
    unsampled_handler.handle(_record(logging.ERROR, "inside unsampled"))
    assert len(transport.log_payloads) == previous_batches
    unsampled_tracer.finish_server_span()
    unsampled = _otlp_record(transport.log_payloads[-1])
    assert unsampled["traceId"] == expected_trace_id
    assert unsampled["spanId"] == expected_span_id
    assert unsampled["flags"] == 0


def test_in_unit_log_buffer_is_capped_and_flushes_on_size_and_completion():
    transport = LogTransport()
    tracer = Tracer(transport, "bounded", "test", max_log_records=2)
    handler = RestlyticsLogHandler(transport, tracer, "bounded", "test")
    tracer.start_server_span("bounded work")

    for index in range(5):
        handler.handle(_record(logging.ERROR, "record {0}".format(index)))
    assert len(transport.log_payloads) == 2

    tracer.finish_server_span()
    assert len(transport.log_payloads) == 3
    batches = [
        payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        for payload in transport.log_payloads
    ]
    assert all(len(batch) <= 2 for batch in batches)
    assert [record["body"]["stringValue"] for batch in batches for record in batch] == [
        "record 0",
        "record 1",
        "record 2",
        "record 3",
        "record 4",
    ]


def test_source_redaction_removes_credentials_pii_bodies_bindings_and_exceptions():
    transport = LogTransport()
    tracer = Tracer(transport, "payments", "test")
    handler = RestlyticsLogHandler(transport, tracer, "payments", "test")
    canaries = (
        "PW_CANARY",
        "EMAIL_CANARY",
        "USER_CANARY",
        "PASS_CANARY",
        "QUERY_CANARY",
        "BEARER_CANARY",
        "BODY_CANARY",
        "BINDING_CANARY",
        "SECRET_CANARY",
        "REQUEST_CANARY",
        "RESPONSE_CANARY",
        "sk_live_12345678CANARY",
        "rl_12345678CANARY",
    )
    message = (
        'password="PW_CANARY" user=EMAIL_CANARY@example.com '
        "url=https://USER_CANARY:PASS_CANARY@example.com/pay?token=QUERY_CANARY#fragment "
        "Authorization: Bearer BEARER_CANARY "
        'request_body="BODY_CANARY" request="REQUEST_CANARY" response=RESPONSE_CANARY '
        'bindings="BINDING_CANARY" secret=SECRET_CANARY '
        "keys sk_live_12345678CANARY rl_12345678CANARY"
    )
    record = _record(logging.ERROR, message)
    record.request_body = "EXTRA_BODY_CANARY"
    record.api_key = "EXTRA_KEY_CANARY"
    handler.handle(record)
    serialized = json.dumps(transport.log_payloads[-1])
    for canary in canaries + ("EXTRA_BODY_CANARY", "EXTRA_KEY_CANARY"):
        assert canary not in serialized
    assert "[REDACTED" in serialized
    assert "https://example.com/pay?token=REDACTED" in serialized
    assert "/private/source/path.py" not in serialized

    try:
        raise ValueError("EXCEPTION_CANARY password=EXCEPTION_SECRET_CANARY")
    except ValueError:
        exception_record = _record(
            logging.ERROR,
            "operation failed: EXCEPTION_MESSAGE_CANARY",
            exc_info=sys.exc_info(),
        )
    handler.handle(exception_record)
    exception_serialized = json.dumps(transport.log_payloads[-1])
    assert "EXCEPTION_CANARY" not in exception_serialized
    assert "EXCEPTION_SECRET_CANARY" not in exception_serialized
    assert "EXCEPTION_MESSAGE_CANARY" not in exception_serialized
    assert _otlp_record(transport.log_payloads[-1])["body"] == {
        "stringValue": "[EXCEPTION REDACTED]"
    }


def test_hostile_records_and_transport_failures_never_escape_host_logging():
    class ExplodingTransport(Transport):
        def send(self, payload):
            del payload
            raise RuntimeError("trace transport failed")

        def send_logs(self, payload):
            del payload
            raise RuntimeError("logs transport failed")

        def flush(self, timeout_ms=2000):
            del timeout_ms
            raise RuntimeError("flush failed")

    class ExplodingMessage:
        def __str__(self):
            raise RuntimeError("format failed")

    tracer = Tracer(ExplodingTransport(), "failure", "test")
    handler = RestlyticsLogHandler(tracer.transport, tracer, "failure", "test")
    assert handler.handle(_record(logging.ERROR, ExplodingMessage()))
    handler.flush()
    handler.close()


def test_transport_error_logger_does_not_create_a_recursive_export_loop():
    class FailingLogsTransport(HttpTransport):
        def _post_logs(self, body):
            del body
            self._report("simulated log delivery failure")
            return False

    logger = logging.getLogger("restlytics-test-transport-errors")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    transport = FailingLogsTransport("http://example.test", "rl_test")
    tracer = Tracer(transport, "failure", "test")
    handler = RestlyticsLogHandler(transport, tracer, "failure", "test")
    logger.addHandler(handler)
    transport._on_error = logger.error
    try:
        logger.error("initial application error")
        assert transport.flush(2000)
        snapshot = transport.diagnostics()
        assert snapshot.accepted_batches == 1
        assert snapshot.failed_batches == 1
    finally:
        logger.removeHandler(handler)
        handler.close()
        transport.close()


def test_http_transport_routes_gzipped_logs_to_v1_logs_and_flushes():
    class CaptureHandler(BaseHTTPRequestHandler):
        requests = []

        def do_POST(self):  # noqa: N802 - stdlib callback name
            length = int(self.headers.get("Content-Length", "0"))
            body = gzip.decompress(self.rfile.read(length))
            type(self).requests.append((self.path, self.headers, json.loads(body)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, _format, *args):
            del args

    server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    transport = HttpTransport(
        "http://127.0.0.1:{0}".format(server.server_port),
        "rl_test_key",
    )
    try:
        payload = build_logs_payload("delivery", "test", [])
        transport.send_logs(payload)
        assert transport.flush(2000)
        assert len(CaptureHandler.requests) == 1
        path, headers, decoded = CaptureHandler.requests[0]
        assert path == "/v1/logs"
        assert headers["X-Restlytics-Key"] == "rl_test_key"
        assert headers["Content-Type"] == "application/json"
        assert headers["Content-Encoding"] == "gzip"
        assert decoded == payload
    finally:
        transport.close()
        server.shutdown()
        server.server_close()


def test_logs_share_the_transport_queue_cap_and_nonblocking_drop_policy():
    gate = threading.Event()

    class BlockingLogsTransport(HttpTransport):
        def _post_logs(self, body):
            del body
            gate.wait(1)
            return True

    transport = BlockingLogsTransport("http://example.test", "rl_test", queue_capacity=2)
    try:
        payload = build_logs_payload("bounded", "test", [])
        for _ in range(10):
            transport.send_logs(payload)
        snapshot = transport.diagnostics()
        assert snapshot.accepted_batches <= 3  # one in flight and two queued
        assert snapshot.dropped_batches >= 7
        assert snapshot.queue_capacity == 2
        gate.set()
        assert transport.flush(2000)
    finally:
        gate.set()
        transport.close()


def test_init_only_installs_the_public_handler_when_logs_are_enabled():
    root = logging.getLogger()
    disabled_transport = LogTransport()
    restlytics.init(
        config=Config(key="key", logs=False),
        transport_impl=disabled_transport,
    )
    assert restlytics.get_log_handler() is None

    enabled_transport = LogTransport()
    try:
        restlytics.init(
            config=Config(key="key", service_name="auto", logs=True),
            transport_impl=enabled_transport,
        )
        handler = restlytics.get_log_handler()
        assert isinstance(handler, restlytics.RestlyticsLogHandler)
        assert handler in root.handlers
        root.handle(_record(logging.ERROR, "automatic capture"))
        assert len(enabled_transport.log_payloads) == 1
    finally:
        restlytics.shutdown()
    assert handler not in root.handlers
