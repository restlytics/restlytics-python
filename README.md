# restlytics — Python SDK

Framework-native request, database, outbound-HTTP and cache tracing—plus opt-in,
trace-correlated native logs—for Python, shipped to
[restlytics](https://restlytics.com) in **OTLP/JSON**.

> **One contract, every language.** This SDK emits the exact same wire format as
> every other restlytics SDK (see [`../SPEC.md`](../SPEC.md)) and obeys the same
> safety rules: **fire-and-forget, gzipped, ~2s timeout, every error swallowed —
> the host app is never blocked and the SDK never throws into it.**

- **Frameworks:** Django · FastAPI · Flask
- **DB:** SQLAlchemy (`after_cursor_execute`) · Django connection execute-wrapper
- **Outbound HTTP (optional):** `requests` · `httpx`
- **Pure-stdlib core** — `import restlytics` pulls in **no** third-party packages.
- Python **3.9+**.

---

## Install

```bash
pip install restlytics
```

Optional framework extras (install the framework alongside the SDK):

```bash
pip install "restlytics[django]"
pip install "restlytics[fastapi]"
pip install "restlytics[flask]"
pip install "restlytics[sqlalchemy]"
```

---

## Configure (`.env`)

The SDK reads these environment variables (explicit `init(...)` keyword args win
over env):

```dotenv
# Required — your project's ingest key (sent as X-Restlytics-Key). Empty ⇒ SDK is inert.
RESTLYTICS_KEY=rl_xxxxxxxxxxxxxxxxxxxxx

# Where to send traces. The SDK POSTs to {INGEST_URL}/v1/traces.
RESTLYTICS_INGEST_URL=https://ingest.restlytics.com

# Resource attributes.
RESTLYTICS_SERVICE_NAME=my-api
RESTLYTICS_ENV=production

# Head-based sampling, 0.0–1.0 (decided once per trace). 1.0 = capture everything.
RESTLYTICS_SAMPLE_RATE=1.0

# Transport: http (default) | null | log. ("curl" is accepted as an http alias.)
RESTLYTICS_TRANSPORT=http

# Capture raw SQL text (db.query.text, capped 2048 chars). OFF by default — only
# the normalized, literal-free db.query.summary is sent regardless. Bindings are
# NEVER sent, only counted.
RESTLYTICS_CAPTURE_SQL=false

# Export Python logging records to /v1/logs. OFF by default.
RESTLYTICS_LOGS=false

# Minimum OTel severityNumber. 13 = WARN (the default); 17 = ERROR.
RESTLYTICS_LOGS_MIN_SEVERITY=13
```

Other recognized vars: `RESTLYTICS_TIMEOUT_MS` (default `2000`),
`RESTLYTICS_MAX_SPANS` (default `2000`), `RESTLYTICS_IGNORE_PATHS`
(comma-separated, supports trailing `*`), and per-instrument toggles
`RESTLYTICS_INSTRUMENT_DB` / `_HTTP` / `_CACHE`.

The equivalent native configuration is `Config(logs=True,
logs_min_severity=13)`, or `restlytics.init(logs=True,
logs_min_severity=13)`.

> The SDK does not load `.env` itself — your app already does (e.g.
> `python-dotenv`, Django settings, your process manager). Call
> `restlytics.init()` after the env is loaded.

---

## Quick start

Always call `restlytics.init()` once at startup, then install the middleware.

### Flask

```python
import restlytics
from flask import Flask
from restlytics.integrations.flask import init_app

restlytics.init(service_name="my-flask-app")

app = Flask(__name__)
init_app(app)   # wraps app.wsgi_app + supplies the matched route template

# DB spans (if you use SQLAlchemy):
from myapp.db import engine
restlytics.instrument_sqlalchemy(engine)
```

### FastAPI

```python
import restlytics
from fastapi import FastAPI
from restlytics.integrations.fastapi import init_app

restlytics.init(service_name="my-fastapi-app")

app = FastAPI()
init_app(app)   # or: app.add_middleware(restlytics.AsgiMiddleware)

# DB spans (SQLAlchemy — pass the engine, or engine.sync_engine for async):
from myapp.db import engine
restlytics.instrument_sqlalchemy(engine)
```

### Django

In `settings.py`, add the middleware **first** so it wraps the whole request:

```python
MIDDLEWARE = [
    "restlytics.integrations.django.RestlyticsDjangoMiddleware",
    # ... your existing middleware ...
]
```

Initialize and install DB instrumentation in an `AppConfig.ready()`:

```python
# myapp/apps.py
from django.apps import AppConfig
import restlytics

class MyAppConfig(AppConfig):
    name = "myapp"

    def ready(self):
        restlytics.init(service_name="my-django-app")
        restlytics.instrument_django()   # connection execute-wrapper DB spans
```

> The Django middleware also installs DB instrumentation lazily on the first
> request, so `instrument_django()` is optional but recommended for early
> connections.

### Outbound HTTP (optional)

Best-effort CLIENT spans for outbound calls — call once after `init()`:

```python
restlytics.instrument_requests()   # for the `requests` library
restlytics.instrument_httpx()      # for the `httpx` library
```

Every `url.full` query value is redacted and credentials/fragments are removed;
request/response headers and bodies are never captured.

---

## What gets captured

Per HTTP request: **one trace** = a root **SERVER** span (`kind=2`) plus a
**CLIENT** child span (`kind=3`) for every DB query / outbound HTTP call / cache
op. After the response is flushed, the SDK stamps per-category **self-time**
(`restlytics.self_ns.{db,http,cache,app}`, interval-union so parallel children
don't over-count) on the SERVER span and fire-and-forgets the gzipped OTLP batch.

- `http.route` is always the **route template** (`/users/{id}`), never the raw URL.
- `db.query.summary` is **normalized and literal-free** (`select * from users
  where id = ?`) — the N+1 grouping key. Bindings are **counted, never sent**.
- W3C `traceparent` is continued when present (distributed tracing).
- Sampling is **head-based** and decided once per trace.

### Background jobs, commands, and schedules

Wrap the framework callback itself so DB/HTTP/cache spans keep using the same
ambient context. Use stable handler names—never ids, arguments, or payloads.

```python
from restlytics import command, enqueue, job, schedule

with enqueue(payload, system="redis", destination="billing") as carrier:
    queue.publish(carrier)

with job(
    "billing.reconcile",
    system="redis",
    destination="billing",
    attempt=message.attempt,
    traceparent=message.data.get("__restlytics", {}).get("traceparent"),
):
    reconcile(message.data)

with command("manage.py migrate") as execution:
    execution.set_exit_code(run_migrations())

with schedule("nightly-digest", cron="0 3 * * *"):
    send_digest()
```

The namespaced carrier continues the producer trace and preserves its sampling
decision. Job roots also link to the enqueue span. Failed work exports only an
`ERROR` status—exception content and carrier payloads are never exported—and
enqueue time is isolated in `restlytics.self_ns.queue`.

### Native Python logs (opt-in)

Set `RESTLYTICS_LOGS=true` before `restlytics.init()`. The SDK installs one
`logging.Handler` on the root logger and exports accepted records to
`{RESTLYTICS_INGEST_URL}/v1/logs` with the same key, gzip encoding, timeout,
bounded queue, and fire-and-forget behavior as traces:

```python
import logging
import restlytics

restlytics.init(logs=True, logs_min_severity=13)
logging.getLogger("checkout").warning("inventory is low")
```

For explicit logger placement instead of root capture, leave `logs` disabled
and opt in directly:

```python
logger = logging.getLogger("checkout")
handler = restlytics.instrument_logging(logger, min_severity=17)
```

Python levels map deterministically to OTel: `DEBUG=5`, `INFO=9`, `WARNING=13
(WARN)`, `ERROR=17`, and `CRITICAL=18 (ERROR2)`. Custom numeric levels map to
the nearest standard level, choosing the more severe bucket on a tie.

Inside a request or background unit, each record carries the ambient
`traceId`, root `spanId`, and sampled flag; outside one, those fields are
omitted. Logs are independent of trace sampling, so an ERROR emitted during an
unsampled request is still exported with valid correlation IDs and `flags=0`.
Because Python DB/HTTP child spans are recorded after the operation completes,
the live root span is the correlation scope available to native logging.

Message text is scrubbed at capture time. Credential/header/body/binding forms,
URL credentials and query values, JWT-like tokens, email addresses, exception
messages/stacks, arbitrary `LogRecord` extras, source paths, and process/thread
metadata are not exported. Scrubbed messages are capped at 2048 characters.
Redaction is intentionally conservative; applications should still avoid
putting secrets or personal data in log messages.

Within an active request/job, up to 512 records are batched and handed to the
asynchronous transport after the unit completes; reaching 512 first triggers a
non-blocking size flush. Boot-time and other out-of-context records enqueue
immediately. `restlytics.shutdown()` performs a bounded final drain.

---

## Transports & testing

### Customer exporters

Use the public, provider-neutral `Exporter` contract when a design partner
needs to hand production-shaped telemetry to its own pipeline. It receives the
same source-redacted OTLP dictionaries used by the built-in HTTP transport for
both signals; the SDK key and other tenant credentials are never passed to it.

```python
from restlytics import Exporter
import restlytics


class EventPipelineExporter(Exporter):
    def __init__(self, client):
        self.client = client

    def export_traces(self, payload):
        self.client.publish_json("observability.traces", payload)

    def export_logs(self, payload):
        self.client.publish_json("observability.logs", payload)

    def flush(self, timeout_ms=2000):
        return self.client.flush(timeout_ms=timeout_ms)

    def shutdown(self, timeout_ms=2000):
        return self.client.close(timeout_ms=timeout_ms)


restlytics.init(
    service_name="checkout-api",
    environment="production",
    logs=True,
    exporter=EventPipelineExporter(existing_pipeline_client),
)
```

`init(exporter=...)` is a full delivery mode and does not require a Restlytics
key. Export callbacks run serially on one SDK-owned daemon worker behind a
fixed 64-batch queue (override with `exporter_queue_capacity=`). Enqueue is
non-blocking: saturation drops the new batch and increments diagnostics instead
of delaying the host. Exceptions—including `BaseException` subclasses—from
export, flush, shutdown, and `on_error` callbacks are contained.

`flush(timeout_ms)` and `shutdown(timeout_ms)` should honor the supplied deadline
and return `False` if their provider-owned work remains. The SDK also bounds its
own wait and returns `False` if a callback blocks or fails. Exporter payloads
must be treated as read-only. Provider retries, persistence, authentication, and
delivery acknowledgements remain the provider's responsibility; the SDK does
not add unbounded retries or pass its project key across this boundary.

The older `transport_impl=` hook remains available for existing SDK-internal
and test transports. New integrations should use `exporter=` so they receive
the bounded asynchronous safety wrapper automatically. If both are supplied,
`exporter=` takes precedence.

### Built-in and test transports

```python
from restlytics.transport import LogTransport, NullTransport, PreviewTransport
import restlytics

# Capture payloads instead of sending (great for tests):
lt = LogTransport()
restlytics.init(key="k", transport_impl=lt)
# ... drive a request ...
assert lt.payloads  # list of the OTLP dicts that would have been sent
```

`RESTLYTICS_TRANSPORT=null` disables delivery while keeping instrumentation;
`=log` captures/logs payloads. `RESTLYTICS_TRANSPORT=preview` needs no ingest key
and emits a structured local report containing the redacted production payload,
configured sampling rate, span count, and JSON/gzip byte sizes. It explicitly
reports `networkRequestMade: false` and never opens a socket. Set
`RESTLYTICS_SAMPLE_RATE=1` for a deterministic one-request review. With no key
and any non-preview transport, the SDK stays completely inert.

---

## Safety

- **Fire-and-forget**: a fixed 64-batch queue and one daemon worker own encoding,
  gzip, and OTLP trace/log POSTs after the response, with a hard ~2s timeout.
- **Never throws**: every instrument path swallows its own errors.
- **Redaction**: SQL normalized (literals stripped), bindings only counted, every outbound
  query value scrubbed, and no request/response headers, bodies, or exception content exported.
  Opt-in native logs additionally scrub credentials, PII, and content-bearing fields at source.
- **Bounded**: per-request span buffer capped (default 2000), state reset per
  request via `contextvars` (thread- and asyncio-safe). Saturation drops the new
  batch instead of blocking or growing threads; delivery is never retried.

Delivery counters contain no payload data, and shutdown is explicitly bounded:

```python
health = restlytics.diagnostics()
print(health.dropped_batches, health.failed_batches)
restlytics.shutdown(timeout_ms=2000)
```

---

## Development

```bash
# No third-party deps needed for the core or the tests:
python3 -m unittest discover -s tests
```

The unit tests cover **SQL normalization**, **interval-union self-time**, native
log severity/correlation/redaction/failure isolation, and the OTLP wire shape.
They run with **zero** third-party dependencies beyond the optional framework
gate installed by CI.

## Cross-language conformance

CI pins [`restlytics/sdk-conformance@v1.1.0`](https://github.com/restlytics/sdk-conformance)
and compares the vendored fixture before testing. The suite proves exact semantic OTLP output,
W3C propagation, root sampling, source redaction, and error-status behavior shared by all seven SDKs.
The release gate also boots a real FastAPI application and sends its request telemetry over gzip HTTP
to a deployed-compatible ingest server. It proves route templates, trace continuation, 202/503 handling,
error status, and that the project key plus request secrets stay out of the payload. FastAPI is beta-validated;
Django and Flask remain preview until they pass the same real-app gate.

## License

MIT — see [LICENSE](./LICENSE).
