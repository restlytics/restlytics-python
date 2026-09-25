"""Outbound HTTP must propagate the exact CLIENT span context."""

import re

import httpx

import restlytics
from restlytics.config import Config
from restlytics.transport import Transport


class CaptureTransport(Transport):
    def __init__(self):
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)


def _config():
    return Config(
        key="rk_test",
        service_name="python-test",
        environment="test",
    )


def test_httpx_injects_traceparent_with_recorded_client_span_id():
    captures = []
    transport = CaptureTransport()
    tracer = restlytics.init(config=_config(), transport_impl=transport)
    restlytics.instrument_httpx()
    tracer.start_server_span(
        "GET /proxy",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    )

    def respond(request):
        captures.append(request.headers["traceparent"])
        return httpx.Response(200, request=request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        response = client.get(
            "https://api.example.test/orders?token=secret",
            headers={"traceparent": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-00"},
        )
    assert response.status_code == 200
    tracer.finish_server_span()

    header = captures[0]
    assert re.fullmatch(
        r"00-4bf92f3577b34da6a3ce929d0e0e4736-[0-9a-f]{16}-01",
        header,
    )
    spans = transport.payloads[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 2
    assert spans[1]["spanId"] == header.split("-")[2]
    assert spans[1]["parentSpanId"] == spans[0]["spanId"]


def test_httpx_propagates_unsampled_context_without_recording():
    captures = []
    transport = CaptureTransport()
    tracer = restlytics.init(config=_config(), transport_impl=transport)
    restlytics.instrument_httpx()
    tracer.start_server_span(
        "GET /proxy",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00",
    )

    def respond(request):
        captures.append(request.headers["traceparent"])
        return httpx.Response(200, request=request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        response = client.get("https://api.example.test/orders")
    assert response.status_code == 200
    tracer.finish_server_span()

    assert re.fullmatch(
        r"00-4bf92f3577b34da6a3ce929d0e0e4736-[0-9a-f]{16}-00",
        captures[0],
    )
    assert transport.payloads == []
