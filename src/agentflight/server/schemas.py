"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SpanEventIn(BaseModel):
    name: str
    time_ns: int
    attributes: dict[str, Any] = Field(default_factory=dict)


class SpanIn(BaseModel):
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    name: str
    start_time_ns: int
    end_time_ns: int | None = None
    status: Literal["unset", "ok", "error"] = "unset"
    status_message: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    events: list[SpanEventIn] = Field(default_factory=list)


class IngestRequest(BaseModel):
    spans: list[SpanIn]


class IngestResponse(BaseModel):
    accepted: int
    runs: list[str]


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
    span_events: list[dict[str, Any]]
    # With ``payloads=false``: attribute keys (and "span_events") left out of this event
    omitted: list[str] = Field(default_factory=list)


class RunSummary(BaseModel):
    id: str
    name: str | None
    status: str
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


class RunDetail(RunSummary):
    events: list[EventOut]
    # Paging of ``events``; ``event_count`` is the total. ``events_limit`` is null when
    # every event from ``events_offset`` on is included.
    events_offset: int = 0
    events_limit: int | None = None


class RunList(BaseModel):
    runs: list[RunSummary]
    total: int
