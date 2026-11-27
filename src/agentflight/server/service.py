"""Ingest and query logic, kept separate from the HTTP layer."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    case,
    delete,
    distinct,
    exists,
    false,
    func,
    or_,
    select,
)
from sqlalchemy.orm import Session

from agentflight import attributes as A
from agentflight.server.models import Event, Run
from agentflight.server.schemas import EventOut, RunDetail, RunSummary, SpanIn

# Stored run status from the root span's status. A root span that ended without setting a
# status is "unknown" (not "ok"): nothing reported success.
RUN_STATUS = {"ok": "ok", "error": "error", "unset": "unknown"}
# Every status a client can see. "abandoned" is never stored: it's a "running" run with no
# span received for longer than the stale threshold, decided when the run is read.
STATUSES = ("running", "ok", "error", "unknown", "abandoned")


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


_CHUNK = 500  # ids per IN (...) clause


def _chunks(items: list[str]) -> Iterator[list[str]]:
    for i in range(0, len(items), _CHUNK):
        yield items[i : i + _CHUNK]


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)[:255]


def extract_columns(kind: str, attributes: dict[str, Any]) -> dict[str, Any]:
    """Values copied from span attributes into event columns, so totals and filters are
    plain SQL. Malformed values from non-SDK senders become NULL instead of failing."""
    is_llm = kind == A.LLM
    return {
        "model": _as_str(attributes.get(A.LLM_MODEL)) if is_llm else None,
        "tool_name": _as_str(attributes.get(A.TOOL_NAME)) if kind == A.TOOL else None,
        "tokens_in": _as_int(attributes.get(A.LLM_TOKENS_IN)) if is_llm else None,
        "tokens_out": _as_int(attributes.get(A.LLM_TOKENS_OUT)) if is_llm else None,
        "cost_usd": _as_float(attributes.get(A.LLM_COST_USD)) if is_llm else None,
        "failure_id": _as_str(attributes.get(A.FAILURE_ID)),
    }


def recompute_totals(session: Session, run_ids: list[str]) -> None:
    """Recompute the stored totals of these runs from their events, in SQL.

    Totals are never incremented: ingest is idempotent and spans can be re-sent or
    updated, and distinct failures aren't additive, so each touched run is recomputed.
    """
    is_error = Event.status == "error"
    failure_key = case((is_error, func.coalesce(Event.failure_id, "span:" + Event.span_id)))
    totals = select(
        Event.run_id,
        func.count().label("event_count"),
        func.coalesce(func.sum(case((Event.kind == A.LLM, 1), else_=0)), 0),
        func.coalesce(func.sum(case((Event.kind == A.TOOL, 1), else_=0)), 0),
        func.count(distinct(failure_key)),
        func.coalesce(func.sum(case((is_error, 1), else_=0)), 0),
        func.coalesce(func.sum(Event.tokens_in), 0),
        func.coalesce(func.sum(Event.tokens_out), 0),
        func.sum(Event.cost_usd),
    ).group_by(Event.run_id)
    for chunk in _chunks(run_ids):
        found = {row[0]: row[1:] for row in session.execute(totals.where(Event.run_id.in_(chunk)))}
        for run_id in chunk:
            run = session.get(Run, run_id)
            if run is None:
                continue
            values = found.get(run_id)
            counts = [int(v) for v in values[:7]] if values else [0] * 7
            (
                run.event_count,
                run.llm_calls,
                run.tool_calls,
                run.errors,
                run.error_events,
                run.tokens_in,
                run.tokens_out,
            ) = counts
            cost = values[7] if values else None
            run.cost_usd = None if cost is None else float(cost)


def ingest_spans(session: Session, spans: list[SpanIn]) -> list[str]:
    """Upsert spans as events, creating/updating their runs. Idempotent per span id.

    The whole batch is looked up with one query per table, flushed once, and the totals of
    every run it touches are recomputed in SQL.
    """
    run_ids = list(dict.fromkeys(s.trace_id for s in spans))
    span_ids = list(dict.fromkeys(s.span_id for s in spans))
    runs: dict[str, Run] = {}
    for chunk in _chunks(run_ids):
        runs.update((r.id, r) for r in session.scalars(select(Run).where(Run.id.in_(chunk))))
    events: dict[tuple[str, str], Event] = {}
    for run_chunk in _chunks(run_ids):
        for span_chunk in _chunks(span_ids):
            query = select(Event).where(Event.run_id.in_(run_chunk), Event.span_id.in_(span_chunk))
            events.update(((e.span_id, e.run_id), e) for e in session.scalars(query))

    now = time.time_ns()
    for span in spans:
        run = runs.get(span.trace_id)
        if run is None:
            run = Run(id=span.trace_id, status="running", metadata_={})
            session.add(run)
            runs[span.trace_id] = run
        run.last_seen_ns = now

        kind = str(span.attributes.get(A.KIND, "span"))[:16]
        if kind == A.RUN:
            run.name = str(span.attributes.get(A.RUN_NAME, span.name))[:255]
            run.root_span_id = span.span_id
            run.start_time_ns = span.start_time_ns
            run.end_time_ns = span.end_time_ns
            run.status = RUN_STATUS[span.status] if span.end_time_ns else "running"
            meta = _loads(span.attributes.get(A.RUN_METADATA, {}))
            run.metadata_ = meta if isinstance(meta, dict) else {"value": meta}
        elif run.start_time_ns is None or span.start_time_ns < run.start_time_ns:
            run.start_time_ns = span.start_time_ns

        key = (span.span_id, span.trace_id)
        event = events.get(key)
        if event is None:
            event = Event(span_id=span.span_id, run_id=span.trace_id)
            session.add(event)
            events[key] = event
        event.parent_span_id = span.parent_span_id
        event.kind = kind
        event.name = span.name
        event.status = span.status
        event.status_message = span.status_message
        event.start_time_ns = span.start_time_ns
        event.end_time_ns = span.end_time_ns
        event.attributes = span.attributes
        event.span_events = [e.model_dump() for e in span.events]
        for column, value in extract_columns(kind, span.attributes).items():
            setattr(event, column, value)
    session.flush()
    recompute_totals(session, run_ids)
    session.commit()
    return run_ids


def _duration_ms(start: int | None, end: int | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start) / 1e6


def event_out(event: Event) -> EventOut:
    attrs = {k: _loads(v) if k.endswith(_JSON_SUFFIXES) else v for k, v in event.attributes.items()}
    truncated = attrs.get(A.TRUNCATED)
    return EventOut(
        span_id=event.span_id,
        parent_span_id=event.parent_span_id,
        kind=event.kind,
        name=event.name,
        status=event.status,
        status_message=event.status_message,
        start_time_ns=event.start_time_ns,
        end_time_ns=event.end_time_ns,
        duration_ms=_duration_ms(event.start_time_ns, event.end_time_ns),
        attributes=attrs,
        truncated=truncated if isinstance(truncated, dict) else {},
        span_events=event.span_events,
    )


_JSON_SUFFIXES = (
    ".metadata",
    ".messages",
    ".response",
    ".params",
    ".args",
    ".result",
    ".choice",
    ".old",
    ".new",
    ".input",
    ".output",
    ".truncated",
)


def shown_status(run: Run, abandoned_before_ns: int | None) -> str:
    if (
        run.status == "running"
        and abandoned_before_ns is not None
        and run.last_seen_ns is not None
        and run.last_seen_ns < abandoned_before_ns
    ):
        return "abandoned"
    return run.status


def summarize(run: Run, abandoned_before_ns: int | None = None) -> RunSummary:
    return RunSummary(
        id=run.id,
        name=run.name,
        status=shown_status(run, abandoned_before_ns),
        last_seen_ns=run.last_seen_ns,
        root_span_id=run.root_span_id,
        start_time_ns=run.start_time_ns,
        end_time_ns=run.end_time_ns,
        duration_ms=_duration_ms(run.start_time_ns, run.end_time_ns),
        metadata=run.metadata_ or {},
        event_count=run.event_count,
        llm_calls=run.llm_calls,
        tool_calls=run.tool_calls,
        errors=run.errors,
        error_events=run.error_events,
        tokens_in=run.tokens_in,
        tokens_out=run.tokens_out,
        cost_usd=run.cost_usd,
    )


META_KEY_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"


@dataclass(frozen=True)
class RunFilters:
    """Filters for :func:`list_runs`. Every set field must match (AND)."""

    status: str | None = None
    name: str | None = None  # case-insensitive substring
    since_ns: int | None = None  # run started at or after
    until_ns: int | None = None  # run started before
    has_errors: bool | None = None
    model: str | None = None  # some LLM call used this model
    tool: str | None = None  # some tool call had this name
    metadata: tuple[tuple[str, str], ...] = ()  # string metadata values, exact match


def _status_filter(status: str, abandoned_before_ns: int | None) -> ColumnElement[bool]:
    running = Run.status == "running"
    if status == "abandoned":
        if abandoned_before_ns is None:
            return false()
        return running & (Run.last_seen_ns < abandoned_before_ns)
    if status == "running" and abandoned_before_ns is not None:
        return running & (Run.last_seen_ns.is_(None) | (Run.last_seen_ns >= abandoned_before_ns))
    return Run.status == status


def _filtered(query: Select[Any], f: RunFilters, abandoned_before_ns: int | None) -> Select[Any]:
    if f.status:
        query = query.where(_status_filter(f.status, abandoned_before_ns))
    if f.name:
        query = query.where(Run.name.icontains(f.name, autoescape=True))
    if f.since_ns is not None:
        query = query.where(Run.start_time_ns >= f.since_ns)
    if f.until_ns is not None:
        query = query.where(Run.start_time_ns < f.until_ns)
    if f.has_errors is not None:
        query = query.where(Run.errors > 0 if f.has_errors else Run.errors == 0)
    if f.model:
        query = query.where(exists().where(Event.run_id == Run.id, Event.model == f.model))
    if f.tool:
        query = query.where(exists().where(Event.run_id == Run.id, Event.tool_name == f.tool))
    for key, value in f.metadata:
        query = query.where(Run.metadata_[key].as_string() == value)
    return query


def list_runs(
    session: Session,
    *,
    limit: int,
    offset: int,
    filters: RunFilters | None = None,
    abandoned_before_ns: int | None = None,
) -> tuple[list[RunSummary], int]:
    f = filters or RunFilters()
    query = _filtered(select(Run), f, abandoned_before_ns)
    count_query = _filtered(select(func.count()).select_from(Run), f, abandoned_before_ns)
    query = query.order_by(Run.start_time_ns.desc(), Run.id).limit(limit).offset(offset)
    runs = session.scalars(query).all()
    total = session.scalar(count_query) or 0
    return [summarize(r, abandoned_before_ns) for r in runs], total


# Attribute suffixes that hold recorded payloads (messages, outputs, args...). Left out of
# events when a caller asks for ``payloads=False``.
_PAYLOAD_SUFFIXES = tuple(s for s in _JSON_SUFFIXES if s != ".truncated")


def _without_payloads(event: EventOut) -> EventOut:
    omitted = [k for k in event.attributes if k.endswith(_PAYLOAD_SUFFIXES)]
    if event.span_events:
        omitted.append("span_events")
    attrs = {k: v for k, v in event.attributes.items() if not k.endswith(_PAYLOAD_SUFFIXES)}
    return event.model_copy(update={"attributes": attrs, "span_events": [], "omitted": omitted})


def get_run(
    session: Session,
    run_id: str,
    *,
    events_limit: int | None = None,
    events_offset: int = 0,
    payloads: bool = True,
    abandoned_before_ns: int | None = None,
) -> RunDetail | None:
    """One run with its events in start order. By default every event with its payloads
    (what replay needs); ``events_limit``/``events_offset`` page them and
    ``payloads=False`` leaves recorded payloads out."""
    run = session.get(Run, run_id)
    if run is None:
        return None
    query = (
        select(Event)
        .where(Event.run_id == run_id)
        .order_by(Event.start_time_ns, func.coalesce(Event.end_time_ns, 0), Event.span_id)
        .offset(events_offset)
    )
    if events_limit is not None:
        query = query.limit(events_limit)
    events = [event_out(e) for e in session.scalars(query)]
    if not payloads:
        events = [_without_payloads(e) for e in events]
    return RunDetail(
        **summarize(run, abandoned_before_ns).model_dump(),
        events=events,
        events_offset=events_offset,
        events_limit=events_limit,
    )


def get_event(session: Session, run_id: str, span_id: str) -> EventOut | None:
    """One event with all of its payloads."""
    event = session.get(Event, (span_id, run_id))
    return None if event is None else event_out(event)


def delete_run(session: Session, run_id: str) -> bool:
    """Delete a run and all of its events. Returns False if it didn't exist."""
    if session.get(Run, run_id) is None:
        return False
    session.execute(delete(Event).where(Event.run_id == run_id))
    session.execute(delete(Run).where(Run.id == run_id))
    session.commit()
    return True


def cleanup_expired(session: Session, *, cutoff_ns: int, dry_run: bool = False) -> int:
    """Delete runs that ended (or, if unfinished, started) before ``cutoff_ns``.

    Runs with no timestamps at all are kept. Returns the number of runs affected.
    """
    expired = select(Run.id).where(
        or_(
            Run.end_time_ns < cutoff_ns,
            (Run.end_time_ns.is_(None)) & (Run.start_time_ns < cutoff_ns),
        )
    )
    ids = list(session.scalars(expired))
    if dry_run or not ids:
        return len(ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        session.execute(delete(Event).where(Event.run_id.in_(chunk)))
        session.execute(delete(Run).where(Run.id.in_(chunk)))
    session.commit()
    return len(ids)
