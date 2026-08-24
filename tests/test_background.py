import asyncio

import pytest

from restlytics.background import command, enqueue, job, schedule
from restlytics.tracer import Tracer
from restlytics.transport import LogTransport


def make_tracer():
    transport = LogTransport()
    return Tracer(transport, "worker", "production"), transport


def root(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def attr(span, key):
    return next(value["value"] for value in span["attributes"] if value["key"] == key)


def test_job_propagates_queue_context_and_records_success_without_payload_data():
    tracer, transport = make_tracer()
    carrier = {"customer": "not-exported"}
    with job("billing.reconcile", system="redis", destination="billing", attempt=2, tracer=tracer):
        with enqueue(carrier, system="redis", destination="emails", tracer=tracer):
            pass

    payload = transport.payloads[0]
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans[0]["kind"] == 5
    assert spans[0]["status"] == {"code": 1}
    assert attr(spans[0], "restlytics.job.attempt") == {"intValue": "2"}
    assert attr(spans[1], "restlytics.category") == {"stringValue": "queue"}
    assert carrier["__restlytics"]["traceparent"].endswith("-{0}-01".format(spans[1]["spanId"]))
    assert "not-exported" not in str(payload)


def test_job_continues_context_links_boundary_and_redacts_failure():
    tracer, transport = make_tracer()
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    with pytest.raises(RuntimeError):
        with job("send.email", system="redis", destination="emails", traceparent=traceparent, tracer=tracer):
            raise RuntimeError("customer secret")
    span = root(transport.payloads[0])
    assert span["traceId"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert span["links"][0]["attributes"][0]["value"] == {"stringValue": "enqueue"}
    assert span["status"] == {"code": 2}
    assert "customer secret" not in str(span)


def test_contextvars_isolate_concurrent_schedules_and_command_exit_failure():
    tracer, transport = make_tracer()

    async def scheduled():
        with schedule("nightly-digest", cron="0 3 * * *", tracer=tracer):
            await asyncio.sleep(0)

    async def commanded():
        with command("reports:generate", tracer=tracer) as execution:
            execution.set_exit_code(2)
            await asyncio.sleep(0)

    async def run_both():
        await asyncio.gather(scheduled(), commanded())

    asyncio.run(run_both())
    roots = [root(payload) for payload in transport.payloads]
    assert len({span["traceId"] for span in roots}) == 2
    assert next(span for span in roots if span["name"] == "nightly-digest")["status"] == {"code": 1}
    assert next(span for span in roots if span["name"] == "reports:generate")["status"] == {"code": 2}


def test_unsampled_queue_context_propagates_without_exporting():
    transport = LogTransport()
    tracer = Tracer(transport, "worker", "production", sample_rate=0.0)
    carrier = {}

    with job("billing.reconcile", system="redis", destination="billing", tracer=tracer):
        with enqueue(carrier, system="redis", destination="emails", tracer=tracer):
            pass

    assert carrier["__restlytics"]["traceparent"].endswith("-00")
    assert transport.payloads == []
