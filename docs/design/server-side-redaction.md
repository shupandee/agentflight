# Server-side redaction: design

Status: **approved with changes; PR 1 implemented on a local branch, not pushed.** Written
against `main` at `a6394c6` (0.2.0 plus sub-agent runs and the spool changes). Section 13
records the review decisions. The only measured numbers are in section 9 (one box, method
given); everything else is a limit that already exists in the code.

## 1. Why, and what exists today

Redaction today is **client-side only**: `configure(redact=hook)` runs `hook(key, value)` on
every payload inside the SDK (`sdk._redact`, called from `sdk._encode` and
`sdk._redact_text`) before it is serialized and exported. That covers SDK users who set a
hook. It does nothing for:

- SDK users who didn't set one, or set a weak one;
- anything sent through OTLP (`POST /v1/traces`): any OpenTelemetry SDK or Collector, whose
  `gen_ai.*` prompts and completions, HTTP headers and arbitrary attributes are stored as
  they arrive;
- one place to enforce a policy for every sender.

Server-side redaction adds a policy enforced where all data converges, before storage.

## 2. Where it runs

- `POST /v1/spans` -> `check_spans` (size limits, 413/422) -> **`service.redact_spans`** ->
  `service.ingest_spans(session, kept)`
- `POST /v1/traces` -> `otlp.convert` (each OTLP span becomes a `SpanIn`; `span_problem`
  applies the same limits) -> **`service.redact_spans`** (rejections are added to
  `Converted.reject`) -> `service.ingest_spans(session, kept, roots)`

`redact_spans(policy, spans, max_chars)` returns the spans to store and the reasons for the
rejected ones. It runs **before** anything is derived from a span, because ingest derives
`runs.name`, `runs.metadata`, `runs.dropped_spans`, `runs.parent_*` (from the run span) and
`events.model`, `events.tool_name`, `events.tokens_*`, `events.cost_usd`,
`events.failure_id` (`extract_columns`). Running first means a secret can't survive in a
column, and filters (`meta=`) see redacted values (documented).

The size checks stay first and run on the original span. Redaction can make a value
longer (section 5, "marker over the cap"), never a span bigger than the cap.

The engine is `agentflight/redaction.py`: pure, no server imports. One implementation, two
ingest paths, so the policy can't differ between them.

## 3. What is covered

| Part | Where it lives | Handling |
| --- | --- | --- |
| Payload attributes | `.messages`, `.args`, `.result`, `.input`, ... (`service._JSON_SUFFIXES`) | Parsed as JSON and walked; re-serialized (`json.dumps(..., ensure_ascii=False)`, as the SDK writes them) only if something changed, so unchanged values keep their bytes. |
| Plain string attributes | OTLP `gen_ai.*`, `http.*`, anything else | A string that starts with `{` or `[` (after whitespace) is **try-parsed**; if it is a JSON object or array it is walked like a payload (key rules apply). Otherwise text mode (patterns + text-level `"key": value`). |
| Truncated values | keys listed in `agentflight.truncated` | Text mode only (section 4.1). |
| Structured values | OTLP arrays and kvlists, `otel.links`, `agentflight.run.metadata` from OTLP | Walked as JSON. |
| Nested JSON | a JSON string inside a JSON value (tool-call `arguments`) | Leaves that look like JSON are parsed and walked too, up to depth 32. |
| Span events | `span.events[].attributes` (incl. `exception.message`, `exception.stacktrace`) | Same as attributes, scope `events`. |
| Status message | `span.status_message` | Text mode, scope `status`. |
| Error attributes | `agentflight.error.message` | Plain string attribute. |
| Resource and scope | `otel.resource.*`, `otel.scope.*` | Like any attribute. |

**Never redacted (structural)**, in `redaction.PROTECTED`:
`agentflight.kind`, `agentflight.run.name`, `agentflight.run.dropped_spans`,
`agentflight.run.parent_run_id` / `parent_span_id` / `parent_source`,
**`agentflight.llm.model`**, `agentflight.llm.tokens_in` / `tokens_out` / `cost_usd`,
**`agentflight.tool.name`**, `agentflight.node.name`, `agentflight.node.step`,
`agentflight.decision.name`, `agentflight.state.key`, `agentflight.integration`,
`agentflight.error.type`, `agentflight.failure.id`, `agentflight.truncated`,
`agentflight.redacted`, and every `agentflight.replay.*`. Span and event names are never
touched. **No rule applies to these, pattern rules included** (replay matches steps by tool
and model name; totals, filters and linking read the rest). Naming one exactly in
`attributes` is a startup error; a glob (`agentflight.*`) silently skips them. Names are
never redacted in v1.

## 4. Policy and config

```text
AGENTFLIGHT_REDACTION_FILE=/etc/agentflight/redaction.toml   # unset: server-side redaction off
AGENTFLIGHT_REDACTION_ON_ERROR=redact | reject               # default: redact (section 6)
```

```toml
version = 1
marker = "[REDACTED:{rule}]"            # default

[[rules]]
name = "credentials"
keys = ["api_key", "password", "authorization"]   # dict keys at any depth, case-insensitive

[[rules]]
name = "tokens"
preset = ["bearer", "aws-access-key-id", "openai-key"]

[[rules]]
name = "internal-ids"
patterns = ['\bACCT-[0-9]{8}\b']                   # stdlib re

[[rules]]
name = "resource-secrets"
attributes = ["otel.resource.deploy.token"]       # exact or fnmatch globs
action = "drop"                                   # mask (default) | drop
scope = ["attributes", "events", "status"]        # default: all three
```

- **Rules are additive**, applied in file order. Attribute rules first (whole value);
  then, per value, key rules on dict entries and pattern rules on strings.
- **Actions:** `mask` replaces the matched substring (patterns) or the value (keys,
  attributes) with the marker; `drop` removes the attribute or dict entry and is only valid
  with `keys` and `attributes`. In text mode a `drop` key rule masks (it can't remove a
  pair from text safely).
- **Presets are key-shaped only** (low false-positive risk): `bearer`
  (`(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{16,}=*`), `aws-access-key-id`
  (`\b(?:AKIA|ASIA)[0-9A-Z]{16}\b`), `openai-key` (`\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}`).
  Emails and card numbers are documentation examples (your own `patterns`), not presets.
- **Validated at startup:** unknown settings, bad version, bad regex, unknown preset or
  scope, `drop` with patterns, a rule with no matcher, duplicate names, a structural
  attribute named exactly, a missing or unparsable file: the server refuses to start.
- **Regex engine: stdlib `re`.** Startup runs a **best-effort** check that rejects the
  classic catastrophic shape (a quantified group containing a quantifier, e.g. `(a+)+`).
  It is a heuristic, documented as such; it doesn't prove a pattern safe.
- **Idempotent:** markers and `[REDACTION_FAILED]` must not match any pattern (startup
  check), and a key whose value is already its marker isn't counted again. Ingest is
  idempotent and senders re-send (retries, spool, Collectors).
- **Restart only.** No reload on SIGHUP in v1.
- **Settings:** `Settings.redaction_file: str | None`, `Settings.redaction_on_error`;
  the policy is compiled once in `create_app` (`app.state.redaction`).

### 4.1 Truncated payloads and text mode

The SDK cuts a payload over `max_attr_chars` and lists the key in `agentflight.truncated`.
A cut payload is no longer JSON, and turning every such value into `[REDACTION_FAILED]`
would lose most large payloads as soon as a key rule exists. Instead, for keys listed in
`agentflight.truncated`, and for any JSON-suffixed or JSON-looking value that fails to
parse, the server uses **text mode**:

1. pattern rules on the whole text;
2. for every key rule key, text-level matching of `"key"\s*:\s*value` (key
   case-insensitive), where value is a JSON string (also one cut off at the end of the
   text), number, `true`, `false` or `null`; the value is replaced by the JSON-quoted
   marker.

Documented limits: a secret the truncation cut in half may no longer match a pattern (for
example an `sk-` key cut below 20 characters); a key whose value is an object or array is
not matched in text mode (its string leaves still get the pattern rules); a key name cut by
the truncation can't match.

## 5. Markers and the `agentflight.redacted` map

- Masked values become `[REDACTED:<rule>]` (configurable `marker`, `{rule}` substituted).
- The server writes one span attribute, `agentflight.redacted`, a JSON map:
  `{"<attribute key>" | "status_message" | "events[<i>].<key>": {"rules": [...], "count": n,
  "dropped"?: true, "over_cap"?: true}}`. It is written only when something was redacted.
- **Overwrite, not merge.** The map is always the server's own: an incoming
  `agentflight.redacted` (client-sent or forged) is removed before redaction and replaced by
  the server's result; a re-sent span gets a freshly computed map (events are replaced on
  re-send, so the map describes the stored version).
- `EventOut.redacted` returns the map (parsed); `.redacted` is in `_JSON_SUFFIXES` and
  kept with `payloads=false` (it holds rule names and counts, no values).
- **Marker over the cap:** a marker can be longer than what it replaces. If a redacted
  string value would exceed `AGENTFLIGHT_MAX_ATTRIBUTE_CHARS`, the whole value is stored as
  the marker of the first rule that hit, with `"over_cap": true`. It is never cut (a cut
  could split a marker or leave part of a secret).

## 6. Failure behaviour (fail closed)

| Case | Result |
| --- | --- |
| No policy file | Spans stored as sent (today's behaviour). |
| Invalid policy | Server refuses to start. |
| Redacting one value raises, `on_error=redact` (default) | That value is stored as `[REDACTION_FAILED]`, map entry `{"rules": ["<error>"]}`; the span is stored. |
| Redacting one value raises, `on_error=reject` | The span isn't stored. |
| The whole span can't be processed (engine crash), `redact` | Every non-structural string, list or map attribute, every such span event attribute, and the status message become `[REDACTION_FAILED]`; structural attributes, numbers and booleans are kept. |
| Same, `reject` | The span isn't stored. |
| Truncated or unparsable JSON with key rules | Text mode (4.1), not `[REDACTION_FAILED]`. |
| Rejected spans on `/v1/spans` | **The other spans are stored.** `202` with `IngestResponse.accepted` = spans stored, `rejected` = n, `error_message` naming spans by id and attribute key, never values (`rejected` and `error_message` are always present; `error_message` is `null` when `rejected` is 0) (mirrors OTLP `partial_success`). Not a 422 for the batch. |
| Rejected spans on `/v1/traces` | Counted in `partial_success.rejected_spans` / `error_message`; the rest stored. |

The SDK exporter logs one warning per export for spans the server reports as rejected, and
doesn't retry or spool them (they would fail the same way).

**No echo of input in errors.** FastAPI's default 422 body includes `input` (the offending
value, possibly a payload). The server installs a `RequestValidationError` handler that
returns only `loc`, `msg`, `type`. OTLP errors were already value-free
(`otlp._validation_message`). Database errors are logged without SQL parameters
(`create_engine(..., hide_parameters=True)`). These apply with or without a policy.

## 7. Replay and fork

**Today:** `_check` compares the live input, encoded by the SDK exactly as when recording
(`sdk._encode`, with the client hook), with the recorded value. Client-side redaction
replays because the same hook produces the same marker. Server-side redaction breaks that:
the live value is the original, the recorded one has markers, so a comparison can't tell
"same input" from "different input".

**Phase 1 (PR 1):**
- `Step.redacted` carries the event's `redacted` map.
- A step whose recorded **input** key is in the map raises `RedactedInputError` (a
  `ReplayDivergence`) naming the step, the key and the rule names, never values. It is
  raised **always, even with `on_divergence="live"`**: a redacted input is not an ordinary
  divergence, so the general live switch doesn't cover it.
- Going live past it needs its own opt-in: `replay(recording, on_redacted_input="live")`
  (and `fork(...)`). The step then diverges to live like a mismatch (`diverged_at`,
  `divergence`), and the run records the opt-in as
  `agentflight.replay.on_redacted_input = "live"` (only set when used).
- A step whose recorded **output** is redacted raises `ReplayError` pointing at
  `fork(recording, at=N, output=...)`, which supplies that output (the override path
  doesn't read the recorded output).
- Client-redacted runs replay as before.

**Phase 2 (deferred):** verified replay under the same rules (a policy id on the run, the
replayer applying the same policy to live inputs before comparing).

## 8. Not retroactive

The policy applies to spans received after the server (re)starts. Stored spans are not
rewritten. A retroactive command is **design only, not in PR 1**:

```text
agentflight-server redact --run <id> [--run <id> ...]     # by run id
agentflight-server redact --since <time> [--until <time>] # by time window
agentflight-server redact --all                           # everything: must be explicit
                                                          # default: dry run (counts only)
agentflight-server redact ... --apply                     # rewrite
```

It would reuse the engine on stored events and rewrite `agentflight.redacted`. Not covered:
backups, database WAL/replicas, and anything already exported elsewhere; documented.

## 9. Performance

One machine: 6 vCPU Intel Xeon (cloud VM), Linux 6.12, Python 3.13.5, SQLAlchemy 2.1.9,
FastAPI 0.165.0, in-memory SQLite. **Postgres not measured.** Method: `bench_ingest.py`
(a throwaway script, not in the repo): one `POST /v1/spans` through FastAPI's `TestClient`
of 512 SDK-shaped LLM spans (2.36 MB of JSON payload: five messages and a response each),
fresh database per repetition, two warm-up posts dropped, 15 timed posts, median reported;
three rounds, with the main and branch runs alternated in separate processes.

| | Round 1 | Round 2 | Round 3 |
| --- | --- | --- | --- |
| no policy file, main (`a6394c6`) | 43.0 ms | 43.1 ms | 44.1 ms |
| no policy file, this branch | 44.8 ms | 43.3 ms | 43.6 ms |
| policy on, this branch (2 rules) | 149.1 ms | 150.1 ms | 148.9 ms |

The off path shows no regression: the three rounds differ by -0.5 to +1.8 ms (noise). The
policy: 2 rules (one key rule with 3 keys, one rule with 3 presets). With it, a
payload-heavy batch costs about three times the no-policy request. Not measured: Postgres,
real agent payloads, OTLP, other rule counts. A speed-up
(skip the JSON parse and walk when nothing can match) follows in section 9.1.

### 9.1 Skipping the JSON walk

`Policy._nothing_can_match(text, scope)` returns True only when the full parse-and-walk
would leave the text unchanged, and the value is then kept as is. It says False (and the
full walk runs) when the text has a backslash (an escape can make a decoded string differ
from the raw text: `\u0061pi_key`, `\n` for `\s`), when a key rule name appears in the
lowercased text (non-ASCII text is walked if a key isn't ASCII), when any pattern matches
the raw text, or when the scope has a pattern with an anchor or lookaround (`^ $ \A \Z
(?= (?! (?<= (?<! (?(`), which can match a string on its own but not inside the JSON text.
Differential tests compare it against the full walk on 600 random documents per policy,
and break when any of the three guards is removed.

Measured with the same method (3 rounds of 15 posts, medians per round; policy: 2 rules;
shape "plain" has no escapes or key names in the payload, "newlines" has `\n` in the message
text, "keyed" has an `api_key` key in each message's metadata):

| Policy on, median of round 1 / 2 / 3 | Before | After |
| --- | --- | --- |
| SQLite in memory, plain | 148.1 / 148.0 / 149.1 ms | 121.6 / 122.3 / 121.6 ms |
| SQLite in memory, newlines | 150.2 / 149.6 / 151.0 ms | 150.0 / 148.7 / 148.9 ms |
| SQLite in memory, keyed | 161.9 / 161.5 / 161.9 ms | 162.9 / 163.0 / 163.4 ms |
| Postgres 17 (local), plain | 165.9 / 170.5 / 168.5 ms | 136.7 / 137.2 / 138.5 ms |
| Postgres 17 (local), newlines | 170.8 / 168.4 / 168.5 ms | 165.7 / 165.7 / 164.1 ms |
| Postgres 17 (local), keyed | 181.5 / 179.1 / 180.2 ms | 178.8 / 179.2 / 179.1 ms |

Without a policy file: about 44 ms (SQLite) and 60-62 ms (Postgres) in every shape, before
and after (the code isn't reached). So the speed-up is about 18% for payloads with nothing
to redact, none for payloads with newlines or a key name. A profile of the plain case after
the change puts about 73 of 78 ms in `re.Pattern.search` (the three presets scanning 2.4 MB),
so the next cost is the pattern scan itself, not the JSON walk.

## 10. Tests (PR 1, `tests/test_redaction.py`)

- Policy: every validation error; the server refuses to start (bad preset, bad TOML,
  missing file, bad `ON_ERROR`); env parsing; the README example policy loads and works.
- Engine: presets (and short look-alikes kept), key rules at any depth and case, unchanged
  values keep their bytes, drop, scope, custom marker, idempotence.
- Change 1 (truncated): text mode on truncated payloads, a value cut mid-string, the
  documented miss for a secret cut by truncation, unparsable JSON, end to end through the
  SDK with `max_attr_chars`.
- Change 2 (JSON in strings): `gen_ai.input.messages` and other plain strings, non-JSON
  strings untouched, JSON inside JSON strings, lists/maps/links, end to end over OTLP.
- Change 3 (replay): `RedactedInputError` with and without `on_divergence="live"`,
  `on_redacted_input="live"` going live and recorded on the run (and absent otherwise),
  validation, redacted output -> `ReplayError`, `fork(output=...)` works.
- Change 4 (never redacted): tool, model and run names and structural attributes survive
  pattern rules and globs, on the engine and through ingest; exact names refused.
- Change 5 (reject mode): default stores `[REDACTION_FAILED]`; `reject` stores the other
  spans and returns `rejected`/`error_message` with no values; an all-rejected batch is
  still `202`; OTLP `partial_success`.
- Change 6 (canary): a secret through SDK attributes, JSON payloads, JSON in a plain
  string, nested JSON, run metadata, the error status/attributes/exception event, state
  changes, LLM messages and response, OTLP resource attributes, link attributes, span
  events and status: absent from every table, every API response, every viewer page
  (list, run, events, each event) and the logs. Error responses: 422 bodies for bad ids,
  types, over-long names, wrong shapes and broken JSON don't echo it. Crash path: every
  value including `status_message` and event attributes becomes `[REDACTION_FAILED]`; in
  `reject` mode the span is rejected. A negative control (a policy that matches nothing)
  was run once by hand to confirm the canary test fails without redaction.
- Reply fields: `error_message` is `null` (and `rejected` 0) with and without a policy;
  the SDK logs one warning for rejected spans and neither retries nor spools them;
  `on_divergence="live"` alone still raises `RedactedInputError`, and only
  `on_redacted_input="live"` goes live and is recorded on the run.
- Smaller: marker over the cap (engine and server limit), re-send overwrites the map
  (including a forged one and a clean re-send), no policy stores as sent, `payloads=false`
  keeps the map, `hide_parameters` on both engines.

## 11. Rollout

1. **PR 1 (this branch):** engine, policy file and settings, both ingest routes,
   `IngestResponse.rejected`/`error_message`, `agentflight.redacted` and
   `EventOut.redacted`, replay phase 1 with the `on_redacted_input` opt-in, 422 without
   input, `hide_parameters`, README and CHANGELOG, tests. No migration.
2. Deferred: viewer badge and run-level redacted count (would need migration `0006`), the
   retroactive command (section 8), replay phase 2, performance work (section 9), an
   optional `re2` engine.

## 12. Client hook and server policy

Both can run: the client hook first (in the SDK), then the server policy. The server stays
silent about it (no warning). Markers don't match rules, so double redaction changes nothing
further.

## 13. Review decisions

1. Default on error: `redact` (store with `[REDACTION_FAILED]`); `reject` is a setting.
2. Rejections on `/v1/spans`: partial storage plus a count, not a 422.
3. Regex: stdlib `re`; the startup pattern check is best-effort.
4. Names are never redacted in v1; `agentflight.tool.name` and `agentflight.llm.model`
   are on the never-redacted list and pattern rules skip them.
5. Replay phase 2 deferred.
6. Run-level count deferred; no migration `0006` in PR 1.
7. Presets: key-shaped only (bearer, AWS key id, `sk-`); emails and cards as doc examples.
8. Policy changes: restart only.
9. Retroactive command: by run id or time window, explicit `--all`, dry run by default;
   design only (section 8).
10. Client hook and server rules both present: silent.

Changes requested in review and where they are: truncated payloads (4.1), JSON in plain
strings (3), replay opt-in (7), never-redacted names (3), partial rejection (6), canary on
error responses and the crash path (10), marker over the cap and overwrite on re-send (5),
phase numbering (7, 11).
