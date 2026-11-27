# Changelog

All notable changes to AgentFlight. The HTTP API is versioned by its `/v1` prefix; while
the project is in early development (`0.x`), `/v1` can still change, and every change that
can break a client is listed under **Behaviour changes**.

## Unreleased (week 7)

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
