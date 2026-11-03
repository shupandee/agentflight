"""Database schema: one row per run, one row per recorded event (span)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # OTel trace id (hex)
    name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="running")
    root_span_id: Mapped[str | None] = mapped_column(String(16))
    start_time_ns: Mapped[int | None] = mapped_column(BigInteger, index=True)
    end_time_ns: Mapped[int | None] = mapped_column(BigInteger)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict)

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

    run: Mapped[Run] = relationship(back_populates="events")

    __table_args__ = (Index("ix_events_run_start", "run_id", "start_time_ns"),)
