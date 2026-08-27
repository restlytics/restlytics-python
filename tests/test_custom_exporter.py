"""Public custom-exporter contract, safety boundary, and lifecycle tests."""

import json
import logging
import threading
import time

import restlytics
from restlytics import Exporter, ExporterTransport
from restlytics.config import Config
from restlytics.transport import Transport


def _record(message):
    return logging.LogRecord(
        "customer-app",
        logging.ERROR,
        "/private/customer/app.py",
        12,
        message,
        (),
        None,
    )


def test_init_exporter_receives_production_trace_and_log_payloads_without_project_key():
    class CaptureExporter(Exporter):
        def __init__(self):
            self.traces = []
            self.logs = []
            self.flush_timeouts = []
            self.shutdown_timeouts = []

        def export_traces(self, payload):
            self.traces.append(payload)

        def export_logs(self, payload):
            self.logs.append(payload)

        def flush(self, timeout_ms=2000):
            self.flush_timeouts.append(timeout_ms)
            return True

        def shutdown(self, timeout_ms=2000):
            self.shutdown_timeouts.append(timeout_ms)
            return True

    exporter = CaptureExporter()
    secret_key = "rl_tenant_identity_must_not_reach_exporter"
    tracer = restlytics.init(
        key=secret_key,
        service_name="checkout",
        environment="production",
        logs=True,
        exporter=exporter,
    )
    assert isinstance(tracer.transport, ExporterTransport)
    assert tracer.transport.exporter is exporter

    tracer.start_server_span("GET /orders/{id}")
    handler = restlytics.get_log_handler()
    assert handler is not None
    handler.handle(_record("authorization=Bearer CUSTOMER_SECRET"))
    tracer.finish_server_span()

    assert tracer.transport.flush(1000)
    assert len(exporter.traces) == 1
    assert len(exporter.logs) == 1
    assert "resourceSpans" in exporter.traces[0]
    assert "resourceLogs" in exporter.logs[0]
    serialized = json.dumps([exporter.traces, exporter.logs])
    assert secret_key not in serialized
    assert "CUSTOMER_SECRET" not in serialized
    assert "REDACTED" in serialized

    assert restlytics.shutdown(1000)
    assert exporter.flush_timeouts
    assert exporter.shutdown_timeouts


def test_callback_and_lifecycle_failures_are_counted_and_never_escape():
    class ExplodingExporter(Exporter):
        def export_traces(self, payload):
            del payload
            raise SystemExit("trace callback failed")

        def export_logs(self, payload):
            del payload
            raise KeyboardInterrupt("log callback failed")

        def flush(self, timeout_ms=2000):
            del timeout_ms
            raise RuntimeError("flush callback failed")

        def shutdown(self, timeout_ms=2000):
            del timeout_ms
            raise RuntimeError("shutdown callback failed")

    def exploding_diagnostic(_message):
        raise RuntimeError("diagnostic callback failed")

    tracer = restlytics.init(
        config=Config(key="key", logs=True),
        exporter=ExplodingExporter(),
        on_error=exploding_diagnostic,
    )
    tracer.start_server_span("failed callbacks")
    handler = restlytics.get_log_handler()
    assert handler is not None
    assert handler.handle(_record("safe host log"))
    tracer.finish_server_span()

    assert tracer.transport.flush(1000) is False
    snapshot = restlytics.diagnostics()
    assert snapshot is not None
    assert snapshot.accepted_batches == 2
    assert snapshot.failed_batches == 2
    assert restlytics.shutdown(1000) is False


def test_custom_exporter_queue_and_shutdown_deadline_are_bounded():
    gate = threading.Event()

    class BlockingExporter(Exporter):
        def export_traces(self, payload):
            del payload
            gate.wait(2)

    exporter = BlockingExporter()
    tracer = restlytics.init(
        key="key",
        exporter=exporter,
        exporter_queue_capacity=2,
    )

    started = time.monotonic()
    for index in range(10):
        tracer.start_server_span("request {0}".format(index))
        tracer.finish_server_span()
    assert time.monotonic() - started < 0.25

    snapshot = restlytics.diagnostics()
    assert snapshot is not None
    assert snapshot.accepted_batches <= 3  # one in flight + two queued
    assert snapshot.dropped_batches >= 7
    assert snapshot.queue_capacity == 2

    started = time.monotonic()
    assert restlytics.shutdown(25) is False
    assert time.monotonic() - started < 0.25

    gate.set()
    assert restlytics.shutdown(1000)


def test_provider_false_lifecycle_results_are_preserved_without_throwing():
    class RefusingExporter(Exporter):
        def export_traces(self, payload):
            del payload

        def flush(self, timeout_ms=2000):
            del timeout_ms
            return False

        def shutdown(self, timeout_ms=2000):
            del timeout_ms
            return False

    tracer = restlytics.init(key="key", exporter=RefusingExporter())
    assert tracer.transport.flush(1000) is False
    assert restlytics.shutdown(1000) is False


def test_shutdown_deadline_also_bounds_sdk_managed_log_handler_flush():
    gate = threading.Event()

    class BlockingFlushExporter(Exporter):
        def export_traces(self, payload):
            del payload

        def flush(self, timeout_ms=2000):
            del timeout_ms
            gate.wait(2)
            return True

    exporter = BlockingFlushExporter()
    restlytics.init(key="key", logs=True, exporter=exporter)

    started = time.monotonic()
    assert restlytics.shutdown(25) is False
    assert time.monotonic() - started < 0.25

    gate.set()
    assert restlytics.shutdown(1000)


def test_legacy_transport_impl_and_hostile_diagnostics_remain_safe():
    class LegacyTransport(Transport):
        def __init__(self):
            self.payloads = []

        def send(self, payload):
            self.payloads.append(payload)

        def diagnostics(self):
            raise RuntimeError("legacy diagnostic failed")

        def close(self, timeout_ms=2000):
            del timeout_ms
            raise RuntimeError("legacy close failed")

    legacy = LegacyTransport()
    tracer = restlytics.init(key="key", transport_impl=legacy)
    assert tracer.transport is legacy
    tracer.start_server_span("legacy")
    tracer.finish_server_span()
    assert len(legacy.payloads) == 1
    assert restlytics.diagnostics() is None
    assert restlytics.shutdown(25) is False


def test_exporter_is_available_from_both_supported_public_import_paths():
    from restlytics.transport import Exporter as TransportExporter
    from restlytics.transport import ExporterTransport as TransportAdapter

    assert restlytics.Exporter is Exporter is TransportExporter
    assert restlytics.ExporterTransport is ExporterTransport is TransportAdapter
