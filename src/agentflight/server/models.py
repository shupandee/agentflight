"""Database schema: one row per run, one row per recorded event (span)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # OTel trace id (hex)
    name: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    root_span_id: Mapped[str | None] = mapped_column(String(16))
    start_time_ns: Mapped[int | None] = mapped_column(BigInteger, index=True)
    end_time_ns: Mapped[int | None] = mapped_column(BigInteger)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict)
    # Server clock (ns) when a span of this run was last ingested.
    last_seen_ns: Mapped[int | None] = mapped_column(BigInteger)

    # Totals over the run's events. Recomputed from the events table (not incremented)
    # for every run an ingest batch touches, so re-sent spans can't make them drift.
    event_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    llm_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_events: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens_in: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    tokens_out: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    cost_usd: Mapped[float | None] = mapped_column(Float)
    # Spans the SDK dropped from a full export queue before the run ended, as reported on
    # the run span (agentflight.run.dropped_spans). 0 when none were reported.
    dropped_spans: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    events: Mapped[list[Event]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Event.start_time_ns"
    )


class Event(Base):
    __tablename__ = "events"

    span_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    parent_span_id: Mapped[str | None] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))
    status_message: Mapped[str | None] = mapped_column(Text)
    start_time_ns: Mapped[int] = mapped_column(BigInteger)
    end_time_ns: Mapped[int | None] = mapped_column(BigInteger)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    span_events: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)

    # Copied out of ``attributes`` at ingest so totals and filters are plain SQL.
    model: Mapped[str | None] = mapped_column(String(255))
    tool_name: Mapped[str | None] = mapped_column(String(255))
    tokens_in: Mapped[int | None] = mapped_column(BigInteger)
    tokens_out: Mapped[int | None] = mapped_column(BigInteger)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    failure_id: Mapped[str | None] = mapped_column(String(255))

    run: Mapped[Run] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_events_run_start", "run_id", "start_time_ns"),
        Index("ix_events_model", "model"),
        Index("ix_events_tool_name", "tool_name"),
    )
