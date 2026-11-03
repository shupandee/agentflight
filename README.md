# AgentFlight

A black box recorder for AI agents. AgentFlight records what an agent does during a run:
model calls, tool executions, decisions, state changes, token spend and failures. Every run
is stored as an ordered timeline you can query, and (from week 3) replay.

> Status: early development (week 1, recorder core). APIs may change.

## Architecture

```mermaid
flowchart LR
    subgraph Agent process
        A[Your agent code] -->|run / llm_call / @tool / decision / state_change| SDK[agentflight SDK]
        SDK --> OTel[OpenTelemetry spans<br/>private TracerProvider]
        OTel --> EXP[AgentFlightExporter<br/>batched HTTP JSON]
    end
    EXP -->|POST /v1/spans| API[FastAPI ingest server]
    API --> DB[(PostgreSQL<br/>runs + events)]
    C[curl / viewer] -->|GET /v1/runs, /v1/runs/:id| API
```

- **One run = one OpenTelemetry trace.** `agentflight.run(...)` opens the root span; its
  trace id is the run id. Everything recorded inside it is a child span.
- **Each span carries `agentflight.*` attributes** (`kind`, model, messages, tokens, tool
  args/result, error type...). Structured values are stored as JSON.
- The SDK uses its **own TracerProvider**, so it doesn't change any global OTel setup your
  app already has.
- The server **upserts by span id** (re-sent batches are idempotent) and accepts child spans
  before their root span arrives (the run shows as `running` until then).

## Quickstart

```bash
# 1. Server + Postgres (applies Alembic migrations on start)
docker compose up --build

# or without Docker, using SQLite:
pip install -e ".[server]"
alembic upgrade head            # uses AGENTFLIGHT_DATABASE_URL, default sqlite:///./agentflight.db
uvicorn agentflight.server.main:app

# 2. Record the toy agent
pip install -e .
python examples/toy_agent.py

# 3. Inspect
curl localhost:8000/v1/runs
curl localhost:8000/v1/runs/<run_id>
```

## Recording API

```python
import agentflight

agentflight.configure("http://localhost:8000")


@agentflight.tool  # records name, bound args, result or exception
def search(query: str) -> list[str]: ...


with agentflight.run("support-agent", metadata={"ticket": 42}) as run:
    with agentflight.llm_call("gpt-4o-mini", messages) as call:
        resp = client.chat(...)  # your model client
        call.record(resp.text, tokens_in=..., tokens_out=..., cost_usd=...)  # cost optional
    agentflight.decision("next_step", "search", reason="needs docs")
    agentflight.state_change("plan", new_plan, old=old_plan)
    try:
        search("refund policy")
    except Exception as exc:
        agentflight.error(exc)  # handled error: recorded, run continues

agentflight.flush()  # before exit, so batched spans are sent
```

- Latency is the span duration (`duration_ms` in the API).
- An exception escaping `run`, `llm_call` or a `@tool` marks that span (and the run) as
  `error` and is re-raised unchanged.
- `@agentflight.tool` works on sync and async functions; `@agentflight.tool(name="...")`
  overrides the name.
- If the server is unreachable, export fails with a logged warning; your agent keeps running.

## Failure isolation

AgentFlight follows one rule: **observability must never become a dependency for
successful agent execution.** `tests/test_failure_isolation.py` checks each guarantee.

Guarantees:

- **Return values and exceptions pass through unchanged.** `run`, `llm_call` and `@tool`
  return what your code returns and re-raise your exact exception object, even if
  recording that exception fails.
- **Recorder failures never raise into agent code.** Each of these is caught and logged as a
  warning on the `agentflight` logger: the server being down, HTTP 4xx/5xx, redirects,
  malformed or truncated responses, transport exceptions, timeouts, exceptions from the
  exporter's `export` / `force_flush` / `shutdown`, exceptions from span processor hooks,
  and a broken tracer or serializer. If a span can't be opened, your code still runs under
  a non-recording span.
- **Weird payloads are safe.** Payloads that JSON can't serialize fall back to `repr`.
  If `repr` itself raises, the value is stored as `<unrepresentable Type>`. This covers
  circular structures, `NaN`, bytes, locks, generators and objects whose `__repr__` or
  `__str__` raise. Values are cut off at 32,000 characters.
- **No blocking in the default mode.** With `batch=True` (the default), spans are exported
  on a background thread, so a hanging server doesn't slow the agent down.
- **Bounded waits.** `flush(timeout_millis=...)` and `shutdown()` never raise. They wait at
  most about the given flush timeout plus the exporter's HTTP timeout (`configure(...,
  timeout=5.0)`). Interpreter exit is also clean: AgentFlight replaces OpenTelemetry's
  atexit hook with a fail-safe one.
- **A bad configuration disables recording instead of raising.**

Limitations (known, not yet handled):

- **Lost spans.** If export fails, those spans are dropped. There's no retry, no disk
  buffer and no dead-letter queue yet. If the server is unreachable, the run is simply
  missing from the recording.
- **The batch queue is bounded.** OpenTelemetry's default limit is 2048 spans. When the
  queue is full, new spans are dropped rather than blocking the agent.
- **`batch=False` blocks.** It exports inside the agent's thread, so each span can block
  for up to the HTTP timeout. Use it only for tests or debugging.
- **A partly broken span processor can mis-parent later spans.** If a processor fails while
  a span is closing, the current-span context may not be restored. The agent still runs,
  but later spans in that run may be attached to the wrong parent.
- **Custom exporters must honor timeouts.** If you write a custom exporter
  that blocks forever (ignoring timeouts), only `flush`/`shutdown` timeouts protect you.
- **`BaseException`s are recorded and re-raised.** `KeyboardInterrupt` and `SystemExit` are
  never swallowed. Only ordinary `Exception`s raised by the instrumentation are suppressed.

## HTTP API

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/v1/spans` | Ingest a batch of spans (`{"spans": [...]}`) |
| `GET` | `/v1/runs` | List runs, newest first (`limit`, `offset`, `status`) with token/cost/error totals |
| `GET` | `/v1/runs/{run_id}` | One run with its events ordered by start time |
| `GET` | `/health` | Liveness (process is up) |
| `GET` | `/ready` | Readiness (database reachable and migrated); 503 otherwise |

Interactive docs at `/docs` when the server is running.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
```

Tests use in-memory SQLite by default. Set `AGENTFLIGHT_TEST_DATABASE_URL` to a Postgres URL
to run the same suite against Postgres (CI does both).

CI also runs a Compose smoke test. It brings up `docker compose up --wait` (Postgres plus
the server, both health-checked), runs `examples/toy_agent.py` against it, and uses
`scripts/compose_smoke.py` to check through the API that both runs were persisted with the
expected token, cost and error totals. The stack is always torn down afterwards. You can
run it locally:

```bash
docker compose up --build --wait
python scripts/compose_smoke.py
docker compose down --volumes
```

## Roadmap

- **Week 1: recorder core** (this branch): SDK, OTel exporter, ingest API, Postgres storage,
  migrations, tests, CI.
- **Week 2: integrations.** Auto-instrumentation for OpenAI-style chat clients (tokens and
  usage taken from responses) and LangGraph, plus a sample agent recording real runs.
- **Week 3: deterministic replay and fork.** Re-run a recorded trace step by step, serving
  cached model and tool outputs, and fork a run from any step with changed inputs.
- **Week 4: React viewer and benchmarks.** Run timeline, decision tree, token spend and
  highlighted failures; README screenshots; recorder overhead measured on real runs and
  published with the method used.

## License

MIT
