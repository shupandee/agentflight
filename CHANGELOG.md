# Changelog

All notable changes to AgentFlight. The HTTP API is versioned by its `/v1` prefix; while
the project is in early development (`0.x`), `/v1` can still change, and every change that
can break a client is listed under **Behaviour changes**.

## Unreleased (week 5)

### Behaviour changes

- **A run whose root span ends without a status is now `unknown`, not `ok`.** Nothing
  reported success, so it no longer looks successful. Migration `0003` updates runs stored
  earlier (its downgrade maps them back to `ok`). Runs recorded with the SDK are not
  affected: the SDK always sets `ok` or `error`.
- **`GET /v1/runs?status=` is validated.** Allowed values are `running`, `ok`, `error`,
  `unknown` and `abandoned`; anything else gets `422` instead of an empty list.
- **Ingest is bounded.** `POST /v1/spans` answers `413` when a request breaks a size limit
  (body size, spans per batch, attribute size or count, events per span) and stores none
  of it. Span and span event names over 255 characters get `422` (on Postgres they used
  to fail with a database error). The defaults are generous and configurable; see
  "Ingest limits" in the README.
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
  read-only session cookie (`HttpOnly`, `SameSite=Strict`, `Secure` on HTTPS). New
  settings: `AGENTFLIGHT_SESSION_SECRET`, `AGENTFLIGHT_SESSION_HOURS`,
  `AGENTFLIGHT_COOKIE_SECURE`.
- **Scoped API keys:** `AGENTFLIGHT_API_KEYS="<key>:ingest,<key>:read,<key>:read+delete"`
  next to the all-permission `AGENTFLIGHT_API_KEY`.
- **Run filters** on `GET /v1/runs`: `name`, `since_ns`, `until_ns`, `has_errors`,
  `model`, `tool` and `meta=key:value`.
- **Paged run detail:** `GET /v1/runs/{id}` takes `events_limit`, `events_offset` and
  `payloads=false`. The default is unchanged: every event with its payloads.
- `GET /v1/runs/{run_id}/events/{span_id}` returns one event with its payloads.
- **`abandoned` status:** a `running` run with no span received for
  `AGENTFLIGHT_STALE_AFTER_MINUTES` (default `60`, `0` turns it off), decided when runs
  are read.
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
