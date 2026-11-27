# Changelog

All notable changes to AgentFlight. The HTTP API is versioned by its `/v1` prefix (see
"HTTP API versioning" under 0.1.0); every change that can break a client is listed under
**Behaviour changes**.

## Unreleased

### Added

- **OTLP ingest:** `POST /v1/traces` accepts OTLP/HTTP trace exports
  (`application/x-protobuf` or `application/json`, optionally `Content-Encoding: gzip`), so
  any OpenTelemetry-instrumented agent or Collector can record runs without the SDK. Same
  `ingest` key auth and ingest limits as `POST /v1/spans`; the 16 MiB body cap applies to the
  decompressed body too. Responses follow the OTLP spec: `partial_success` for spans that
  can't be stored, `google.rpc.Status` errors with `400`/`401`/`403`/`413`/`415`, and a
  retryable `503` only when the database is unavailable. Spans with a name or span event
  name over 255 characters are rejected that way, as on `/v1/spans`.
- OTLP spans map onto runs: the trace is the run and its parentless span closes it;
  `gen_ai.*` operations, models, tokens and tool names become `llm` and `tool` events that
  count in run totals and filters; `service.*` and `deployment.environment` become run
  metadata; every other attribute (resource attributes as `otel.resource.*`) is kept.
  Spans with `agentflight.*` attributes are read as on `/v1/spans`. Re-sent batches are
  idempotent. See "OTLP ingest" in the README for limitations (no replay or cost for
  OTLP-only runs).
- The server depends on `opentelemetry-proto`.

## 0.1.0 (2026-11-27)

The first release on PyPI, as **`agentflight-sdk`** (the name `agentflight` is too close
to an existing PyPI project): `pip install agentflight-sdk` for the SDK,
`pip install "agentflight-sdk[server]"` for the server and viewer. The import name is
still `agentflight`. It contains everything
below (weeks 1-7) plus the changes in this section.

### HTTP API versioning

Every endpoint a client uses is under `/v1`: `POST /v1/spans` (ingest),
`GET /v1/runs`, `GET /v1/runs/{run_id}`, `GET /v1/runs/{run_id}/events/{span_id}` and
`DELETE /v1/runs/{run_id}`. `/health` and `/ready` are unversioned, and the viewer's
`/ui` pages are not an API. From 0.1.0 on, `/v1` is stable:

- **Only additive changes within `/v1`:** new endpoints, new optional request fields or
  query parameters, new response fields and new values where a field is documented as
  open-ended. Clients must ignore response fields they don't know.
- **Nothing is removed, renamed or given a new meaning within `/v1`:** paths, methods,
  required fields, field types, status codes, auth scopes, paging and the span JSON that
  `POST /v1/spans` accepts (including the `agentflight.*` attributes the server reads).
- **A breaking change means `/v2`.** `/v2` would be served next to `/v1` for at least one
  minor release after it ships, and `/v1`'s removal would be announced here under
  Behaviour changes first.
- **Any SDK 0.1+ works with any server 0.1+** as long as both speak `/v1`; the SDK only
  uses `POST /v1/spans`, and the replayer the read endpoints.
- Security fixes are the one exception: if a `/v1` behaviour is unsafe it can change in
  a patch release, and the change is listed here.

The Python API is not covered by this: while AgentFlight is `0.x`, a minor release
(0.2, 0.3, ...) can still change it, and every such change is listed under Behaviour
changes.

### Packaging

- **Version 0.1.0**, read by the build from `agentflight.__version__`. Package metadata
  now has classifiers, keywords, project URLs and the license file; README links and
  images are absolute so they work on PyPI; the sdist holds the source, tests, examples
  and scripts only.
- **Release workflow** (`.github/workflows/release.yml`): pushing a tag `v*` runs the
  full CI suite (`ci.yml`, reused), then builds the sdist and wheel, checks the tag
  matches the version, runs `twine check --strict`, smoke-tests the wheel in a clean venv
  (`scripts/smoke_installed.py`) and only then publishes to PyPI as `agentflight-sdk` with
  Trusted Publishing (environment `pypi`, no stored token).

### Added

- **Bounded on-disk spool (opt-in):** `configure(spool_dir=...)` or
  `AGENTFLIGHT_SPOOL_DIR`. Spans whose export failed in a way that may clear up are kept
  on disk and sent at the start of later exports (including after a restart), oldest
  first, within the attempt cap and with the usual 413 splitting. Bounded by size
  (`spool_max_bytes`, 64 MiB) and age (`spool_max_age_s`, 24 h), private (`0700`/`0600`),
  fail-open (full disk, unwritable directory, corrupt files) and never on the agent's
  thread, so it needs `batch=True`.
- **`agentflight-server migrate [revision]`** applies the database migrations, which now
  ship inside the package (`agentflight/server/migrations`), so a server installed from a
  wheel can create and upgrade its database without a source checkout. `alembic upgrade
  head` from a checkout still works. The Docker image uses the new command.

### Changed

- **Benchmark rerun** with run spans on their own lane (5 repeats): every dropped span
  was recorded on its run and every run closed, also in the tight loop that drops about
  half the spans. The benchmark now reports how many runs the server has with a final
  status.
- **Export errors are reported as `HTTP <status> from <url>`** instead of the httpx
  exception's repr, and other 4xx answers are counted as rejected for good (not spooled).
- **Run spans skip the batch queue.** A run's root span goes to a small lane of its own,
  sent by a dedicated thread as soon as the run ends (with a deadline, the exporter's
  `timeout`), so a run whose other spans were dropped still closes with its status and
  `dropped_spans` instead of showing as `abandoned`. Ending a run never waits on the
  network; `flush()`/`shutdown()` wait for the lane within their timeouts. The queue
  slots that used to be kept for run spans are gone. New:
  `AgentFlightExporter.export_within(spans, deadline_s)`.

## Week 7 (merged in #5)

### Added

- **Configurable batch queue:** `configure(max_queue_size=...)` or
  `AGENTFLIGHT_MAX_QUEUE_SIZE` (then OpenTelemetry's `OTEL_BSP_MAX_QUEUE_SIZE`), passed
  to the batch span processor. The default is unchanged (2048).
- **Dropped spans are counted.** The SDK refuses spans itself when the queue is full
  (instead of OpenTelemetry silently dropping the oldest queued span), counts them, and
  records each run's count on its run span (`agentflight.run.dropped_spans`, best effort:
  spans dropped after the run span ended can't be on it). The total is
  `get_recorder().dropped_spans`.
- **Incomplete recordings are visible.** Runs have `dropped_spans` (migration `0004`, 0
  for existing runs) in `/v1/runs` and run detail, and the viewer's run list and run page
  show "incomplete recording: N spans dropped".
- **Benchmark repeats and ingest rate.** `scripts/bench_overhead.py` now repeats the
  whole benchmark (`--repeats`, default 5) and reports the spread, measures the server's
  ingest rate in spans/s, and counts the spans the SDK dropped. The README's numbers are
  from 5 repeats; the week-6 numbers were a single run.
- **Replay warns about incomplete recordings** as `replay()`/`fork()` start, and
  `Recording` has `dropped_spans` and `incomplete`.

### Changed

- **Split-on-413 retries stop at the first other failure.** A timeout, transport error,
  5xx or credentials error during an export drops the halves not yet sent instead of
  trying each of them. The attempt cap stays.
- **`flush()` and `shutdown()` are bounded by AgentFlight**, not only by OpenTelemetry
  (whose 1.45 `force_flush` ignores its timeout). `shutdown()` takes `timeout_millis`
  (default 30 s). `flush()` returns `False` when it runs out of time.

## Week 6 (merged in #4)

### Added

- **Span tree in the viewer.** The run page nests each span under its parent (siblings in
  start order) and pages in tree order. Spans whose parent is missing start their own
  subtree. The `/v1` API's event order is unchanged.
- **README screenshots** of the run list, run page, span tree and a fork run, under
  `docs/screenshots/`, taken by the browser smoke test.
- **Overhead benchmark.** `scripts/bench_overhead.py` measures what recording adds per
  span (no exporter, serialization only, HTTP with and without batching) and counts what
  the server stored. Results and method are in the README under "Overhead".

### Changed

- **Split-on-413 retries are capped per export.** The exporter makes at most
  `max_export_attempts` requests per export (default `32`, `configure(...)` takes it);
  spans still unsent then are dropped. Each export that drops spans now logs one warning
  with the count, instead of one per failed request.

## Week 5 (merged in #3)

### Behaviour changes

- **A run whose root span ends without a status is now `unknown`, not `ok`.** Nothing
  reported success, so it no longer looks successful. Migration `0003` updates runs stored
  earlier (its downgrade maps them back to `ok`). Runs recorded with the SDK are not
  affected: the SDK always sets `ok` or `error`.
- **API keys must be at least 32 characters.** The server refuses to start if
  `AGENTFLIGHT_API_KEY` or any `AGENTFLIGHT_API_KEYS` key is shorter (the error names the
  setting, never the key). There is still no rate limit on failed keys; long random keys
  are what keep guessing impractical until there is.
- **Runs that stop reporting become `abandoned`, not `running` forever.** A run still
  `running` with no span received for `AGENTFLIGHT_STALE_AFTER_MINUTES` (default `60`,
  `0` turns it off) is shown and filtered as `abandoned`. It is decided when runs are
  read, nothing stored changes, and a late span makes the run `running` again. Clients
  that treat anything other than `running`/`ok`/`error` as unexpected need updating.
- **`GET /v1/runs?status=` is validated.** Allowed values are `running`, `ok`, `error`,
  `unknown` and `abandoned`; anything else gets `422` instead of an empty list.
- **Ingest is bounded.** `POST /v1/spans` answers `413` when a request breaks a size limit
  (body size, spans per batch, attribute size or count, events per span) and stores none
  of it. Span and span event names over 255 characters get `422` (on Postgres they used
  to fail with a database error). The body cap defaults to 16 MiB; all limits are
  configurable; see "Ingest limits" in the README. The SDK's exporter splits a batch in
  half and retries when the server answers `413`, so a big batch isn't dropped whole.
- **Exception messages and stack traces go through the `redact` hook** (keys
  `agentflight.error.message` and `exception.stacktrace`) and are capped at
  `max_attr_chars`. The SDK now builds the OpenTelemetry `exception` span event itself
  instead of calling `span.record_exception()`, so the stored message and stack trace are
  the redacted ones.

### Added

- **Viewer** at `/ui`: run list with filters, run timeline with highlighted failures, and
  an event panel with payloads loaded on demand. Server-rendered with Jinja2 and a
  vendored htmx 2.0.11, under a Content-Security-Policy with no inline script or style.
- **Viewer sign-in** with a key that has the `read` permission, kept as a signed,
  read-only session cookie (`HttpOnly`, `SameSite=Strict`, `Secure` on HTTPS). The
  signing secret is derived from the configured API keys (so it works across workers
  with no setup, and changing a key ends all sessions) unless
  `AGENTFLIGHT_SESSION_SECRET` is set. Sign-in and sign-out posts without a same-host
  `Origin` header are refused. Other new settings: `AGENTFLIGHT_SESSION_HOURS`,
  `AGENTFLIGHT_COOKIE_SECURE`.
- **Scoped API keys:** `AGENTFLIGHT_API_KEYS="<key>:ingest,<key>:read,<key>:read+delete"`
  next to the all-permission `AGENTFLIGHT_API_KEY`.
- **Run filters** on `GET /v1/runs`: `name`, `since_ns`, `until_ns`, `has_errors`,
  `model`, `tool` and `meta=key:value`.
- **Paged run detail:** `GET /v1/runs/{id}` takes `events_limit`, `events_offset` and
  `payloads=false`. The default is unchanged: every event with its payloads.
- `GET /v1/runs/{run_id}/events/{span_id}` returns one event with its payloads.
- Run summaries include `last_seen_ns` and `root_span_id`. Run detail includes
  `events_offset` and `events_limit`, and each event has an `omitted` list.

### Changed

- Run totals (calls, errors, tokens, cost) are stored on the run row and recomputed in SQL
  for every run an ingest batch touches, so listing runs no longer reads their events.
  Migration `0002` adds the columns and backfills them.
- Ingest looks up each batch with one query per table and flushes once, instead of once
  per span.

### Fixed

- SQLite migrations no longer delete events when a batch migration rebuilds the `runs`
  table (foreign keys are turned off while migrating).

## 0.1.0.dev0 (weeks 1-4)

Recorder SDK and OpenTelemetry exporter, ingest API with Postgres and SQLite storage,
API-key auth, retention, truncation and redaction, OpenAI-style and LangGraph
integrations, and deterministic replay and fork.
