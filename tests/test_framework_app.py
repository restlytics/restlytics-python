"""Real FastAPI application to deployed-compatible ingest validation."""

import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

import restlytics
from restlytics.config import Config
from restlytics.integrations.fastapi import init_app
from restlytics.transport import HttpTransport

PROJECT_KEY = "rk_project_alpha"
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
SECRET = "customer-secret-must-not-leave-the-app"


class CaptureServer(ThreadingHTTPServer):
    captures = []
    status = 202
    event = threading.Event()


class CaptureHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(gzip.decompress(self.rfile.read(length)).decode("utf-8"))
        self.server.captures.append(  # type: ignore[attr-defined]
            {"key": self.headers.get("x-restlytics-key"), "path": self.path, "payload": payload}
        )
        self.server.event.set()  # type: ignore[attr-defined]
        self.send_response(self.server.status)  # type: ignore[attr-defined]
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, _format, *_args):
        return


def _root(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _attributes(payload):
    return {item["key"]: item["value"] for item in _root(payload).get("attributes", [])}


def _wait_for_capture(server, count):
    deadline = time.monotonic() + 2.0
    while len(server.captures) < count and time.monotonic() < deadline:
        server.event.wait(0.05)
        server.event.clear()
    assert len(server.captures) >= count, "timed out waiting for ingest"


def test_real_fastapi_app_emits_tenant_safe_otlp_and_survives_ingest_failure():
    server = CaptureServer(("127.0.0.1", 0), CaptureHandler)
    server.captures = []
    server.status = 202
    server.event = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    transport = HttpTransport("http://127.0.0.1:{0}".format(server.server_port), PROJECT_KEY, timeout_ms=300)
    restlytics.init(
        config=Config(key=PROJECT_KEY, service_name="fastapi-beta-app", environment="test"),
        transport_impl=transport,
    )
    app = FastAPI()
    init_app(app)

    @app.get("/orders/{order_id}")
    async def order(order_id: int):
        return {"id": order_id}

    @app.get("/fail/{order_id}")
    async def fail(order_id: int):
        return Response(status_code=503, content="unavailable")

    try:
        with TestClient(app) as client:
            response = client.get(
                "/orders/42?token=" + SECRET,
                headers={
                    "authorization": "Bearer " + SECRET,
                    "cookie": "session=" + SECRET,
                    "traceparent": TRACEPARENT,
                },
            )
            assert response.status_code == 200
            _wait_for_capture(server, 1)

            capture = server.captures[0]
            payload = capture["payload"]
            assert capture["path"] == "/v1/traces"
            assert capture["key"] == PROJECT_KEY
            assert _root(payload)["traceId"] == "4bf92f3577b34da6a3ce929d0e0e4736"
            assert _root(payload)["parentSpanId"] == "00f067aa0ba902b7"
            assert _attributes(payload)["http.route"] == {"stringValue": "/orders/{order_id}"}
            assert PROJECT_KEY not in json.dumps(payload)
            assert SECRET not in json.dumps(payload)

            server.status = 503
            response = client.get("/orders/43")
            assert response.status_code == 200
            _wait_for_capture(server, 2)

            server.status = 202
            response = client.get("/fail/44")
            assert response.status_code == 503
            _wait_for_capture(server, 3)
            failed_payload = server.captures[2]["payload"]
            assert _root(failed_payload)["status"]["code"] == 2
            assert _attributes(failed_payload)["http.route"] == {"stringValue": "/fail/{order_id}"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
