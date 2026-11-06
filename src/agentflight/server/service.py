"""Ingest and query logic, kept separate from the HTTP layer."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from agentflight import attributes as A
from agentflight.server.models import Event, Run
from agentflight.server.schemas import EventOut, RunDetail, RunSummary, SpanIn

RUN_STATUS = {"ok": "ok", "error": "error", "unset": "ok"}


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def ingest_spans(session: Session, spans: list[SpanIn]) -> list[str]:
    """Upsert spans as events, creating/updating their runs. Idempotent per span id."""
    run_ids: list[str] = []
    for span in spans:
        run = session.get(Run, span.trace_id)
        if run is None:
            run = Run(id=span.trace_id, status="running", metadata_={})
            session.add(run)
        if span.trace_id not in run_ids:
            run_ids.append(span.trace_id)

        kind = str(span.attributes.get(A.KIND, "span"))
        if kind == A.RUN:
            run.name = str(span.attributes.get(A.RUN_NAME, span.name))
            run.root_span_id = span.span_id
            run.start_time_ns = span.start_time_ns
            run.end_time_ns = span.end_time_ns
            run.status = RUN_STATUS[span.status] if span.end_time_ns else "running"
            meta = _loads(span.attributes.get(A.RUN_METADATA, {}))
            run.metadata_ = meta if isinstance(meta, dict) else {"value": meta}
        elif run.start_time_ns is None or span.start_time_ns < run.start_time_ns:
            run.start_time_ns = span.start_time_ns

        event = session.get(Event, (span.span_id, span.trace_id))
        if event is None:
            event = Event(span_id=span.span_id, run_id=span.trace_id)
            session.add(event)
        event.parent_span_id = span.parent_span_id
        event.kind = kind
        event.name = span.name
        event.status = span.status
        event.status_message = span.status_message
        event.start_time_ns = span.start_time_ns
        event.end_time_ns = span.end_time_ns
        event.attributes = span.attributes
        event.span_events = [e.model_dump() for e in span.events]
        session.flush()
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
    ".truncated",
)


def summarize(run: Run) -> RunSummary:
    tokens_in = tokens_out = 0
    cost: float | None = None
    llm = tool = error_events = 0
    failures: set[str] = set()
    for e in run.events:
        if e.kind == A.LLM:
            llm += 1
            tokens_in += int(e.attributes.get(A.LLM_TOKENS_IN, 0) or 0)
            tokens_out += int(e.attributes.get(A.LLM_TOKENS_OUT, 0) or 0)
            if A.LLM_COST_USD in e.attributes:
                cost = (cost or 0.0) + float(e.attributes[A.LLM_COST_USD])
        elif e.kind == A.TOOL:
            tool += 1
        if e.status == "error":
            error_events += 1
            # Events sharing a failure id are one logical failure. Events from SDKs that
            # don't send ids count individually.
            failures.add(str(e.attributes.get(A.FAILURE_ID) or f"span:{e.span_id}"))
    return RunSummary(
        id=run.id,
        name=run.name,
        status=run.status,
        start_time_ns=run.start_time_ns,
        end_time_ns=run.end_time_ns,
        duration_ms=_duration_ms(run.start_time_ns, run.end_time_ns),
        metadata=run.metadata_ or {},
        event_count=len(run.events),
        llm_calls=llm,
        tool_calls=tool,
        errors=len(failures),
        error_events=error_events,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
    )


def list_runs(
    session: Session, *, limit: int, offset: int, status: str | None
) -> tuple[list[RunSummary], int]:
    query = select(Run).options(selectinload(Run.events))
    count_query = select(func.count()).select_from(Run)
    if status:
        query = query.where(Run.status == status)
        count_query = count_query.where(Run.status == status)
    query = query.order_by(Run.start_time_ns.desc(), Run.id).limit(limit).offset(offset)
    runs = session.scalars(query).all()
    total = session.scalar(count_query) or 0
    return [summarize(r) for r in runs], total


def get_run(session: Session, run_id: str) -> RunDetail | None:
    run = session.scalars(
        select(Run).where(Run.id == run_id).options(selectinload(Run.events))
    ).first()
    if run is None:
        return None
    ordered = sorted(run.events, key=lambda e: (e.start_time_ns, e.end_time_ns or 0))
    return RunDetail(**summarize(run).model_dump(), events=[event_out(e) for e in ordered])


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
