# restlytics — Python SDK

Framework-native request, database, outbound-HTTP and cache tracing for Python,
shipped to [restlytics](https://restlytics.com) in **OTLP/JSON**.

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
```

Other recognized vars: `RESTLYTICS_TIMEOUT_MS` (default `2000`),
`RESTLYTICS_MAX_SPANS` (default `2000`), `RESTLYTICS_IGNORE_PATHS`
(comma-separated, supports trailing `*`), and per-instrument toggles
`RESTLYTICS_INSTRUMENT_DB` / `_HTTP` / `_CACHE`.

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

The `url.full` query string is redacted of sensitive keys; request/response
bodies are never captured.

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

---

## Transports & testing

```python
from restlytics.transport import LogTransport, NullTransport
import restlytics

# Capture payloads instead of sending (great for tests):
lt = LogTransport()
restlytics.init(key="k", transport_impl=lt)
# ... drive a request ...
assert lt.payloads  # list of the OTLP dicts that would have been sent
```

`RESTLYTICS_TRANSPORT=null` disables delivery while keeping instrumentation;
`=log` captures/logs payloads. With no `RESTLYTICS_KEY`, the SDK installs a
no-op transport and stays completely inert.

---

## Safety

- **Fire-and-forget**: the OTLP POST runs on a daemon thread after the response,
  with a hard ~2s timeout. A slow or down ingest endpoint never affects requests.
- **Never throws**: every instrument path swallows its own errors.
- **Redaction**: SQL normalized (literals stripped), bindings only counted,
  outbound query strings + sensitive headers scrubbed, no request/response bodies.
- **Bounded**: per-request span buffer capped (default 2000), state reset per
  request via `contextvars` (thread- and asyncio-safe).

---

## Development

```bash
# No third-party deps needed for the core or the tests:
python3 -m unittest discover -s tests
```

The unit tests cover **SQL normalization** and **interval-union self-time** (plus
the OTLP wire shape) and run with **zero** third-party dependencies.

## License

MIT — see [LICENSE](./LICENSE).
