"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SpanEventIn(BaseModel):
    name: str = Field(max_length=255)
    time_ns: int
    attributes: dict[str, Any] = Field(default_factory=dict)


class SpanIn(BaseModel):
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    name: str = Field(max_length=255)
    start_time_ns: int
    end_time_ns: int | None = None
    status: Literal["unset", "ok", "error"] = "unset"
    status_message: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    events: list[SpanEventIn] = Field(default_factory=list)


class IngestRequest(BaseModel):
    spans: list[SpanIn]


class IngestResponse(BaseModel):
    accepted: int  # spans stored
    runs: list[str]
    # Spans not stored (server-side redaction failed with AGENTFLIGHT_REDACTION_ON_ERROR=
    # reject), like OTLP partial_success. The message never contains attribute values.
    rejected: int = 0
    error_message: str = ""


class EventOut(BaseModel):
    span_id: str
    parent_span_id: str | None
    kind: str
    name: str
    status: str
    status_message: str | None
    start_time_ns: int
    end_time_ns: int | None
    duration_ms: float | None
    attributes: dict[str, Any]
    # attribute key -> {original_length, stored_length, reason} for truncated payloads
    truncated: dict[str, Any]
    # attribute key (or "status_message", "events[i].key") -> {rules, count, ...} for
    # values the server's redaction policy replaced; see agentflight.redaction
    redacted: dict[str, Any] = Field(default_factory=dict)
    span_events: list[dict[str, Any]]
    # With ``payloads=false``: attribute keys (and "span_events") left out of this event
    omitted: list[str] = Field(default_factory=list)


class RunSummary(BaseModel):
    id: str
    name: str | None
    # running | ok | error | unknown (ended without a status) | abandoned (running, but no
    # span received for longer than the server's stale threshold)
    status: str
    last_seen_ns: int | None  # server time the last span of this run was received
    root_span_id: str | None  # the run span, once it has arrived
    start_time_ns: int | None
    end_time_ns: int | None
    duration_ms: float | None
    metadata: dict[str, Any]
    event_count: int
    llm_calls: int
    tool_calls: int
    errors: int  # distinct logical failures
    error_events: int  # every event with error status
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    # Spans the SDK reported dropping (full export queue) before the run ended; the
    # recording is incomplete when this is above 0. Best effort: see the README.
    dropped_spans: int = 0
    # Set on a sub-agent run: the run that started it, and the span of that run it was
    # started from. The parent need not be stored (it can arrive later or be deleted).
    parent_run_id: str | None = None
    parent_span_id: str | None = None
    # "explicit" (the sender named the parent) or "inferred" (from OTLP span links)
    parent_source: str | None = None


class RunDetail(RunSummary):
    # Runs that name this one as their parent (list them with ``GET /v1/runs?parent_run_id=``)
    child_run_count: int = 0
    events: list[EventOut]
    # Paging of ``events``; ``event_count`` is the total. ``events_limit`` is null when
    # every event from ``events_offset`` on is included.
    events_offset: int = 0
    events_limit: int | None = None


class RunList(BaseModel):
    runs: list[RunSummary]
    total: int
