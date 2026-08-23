"""Bounded-delivery and lifecycle tests for the production transport."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from restlytics.transport import HttpTransport


class _BlockingTransport(HttpTransport):
    def __init__(self, gate):
        self.gate = gate
        super().__init__("http://example.test", "rl_test", queue_capacity=4)

    def _post(self, body):
        del body
        self.gate.wait(1)
        return True


def test_send_is_non_blocking_bounded_observable_and_flushable():
    gate = threading.Event()
    errors = []
    transport = _BlockingTransport(gate)
    transport._on_error = errors.append

    started = time.monotonic()
    for _ in range(10):
        transport.send({})
    assert time.monotonic() - started < 0.25
    snapshot = transport.diagnostics()
    assert snapshot.accepted_batches <= 5  # one active + four queued
    assert snapshot.dropped_batches >= 5
    assert snapshot.queue_capacity == 4
    assert any("queue is full" in message for message in errors)

    gate.set()
    assert transport.close(2000)
    assert transport.diagnostics().delivered_batches == snapshot.accepted_batches
    transport.send({})
    assert transport.diagnostics().dropped_batches == snapshot.dropped_batches + 1


def test_timeout_is_counted_swallowed_and_not_retried():
    class SlowHandler(BaseHTTPRequestHandler):
        attempts = 0

        def do_POST(self):  # noqa: N802 - stdlib callback name
            type(self).attempts += 1
            time.sleep(0.2)

        def log_message(self, _format, *args):
            del args

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    transport = HttpTransport(
        "http://127.0.0.1:{0}".format(server.server_port),
        "rl_test",
        timeout_ms=100,
    )
    try:
        transport.send({})
        assert transport.flush(1000)
        assert SlowHandler.attempts == 1
        assert transport.diagnostics().failed_batches == 1
    finally:
        transport.close()
        server.shutdown()
        server.server_close()
