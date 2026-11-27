"""event columns for totals and filters, stored run totals

Copies model, tool name, tokens, cost and failure id out of event attributes into
columns, adds stored totals and a last-seen time to runs, and backfills both.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-27
"""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

EVENT_COLUMNS: list[tuple[str, Any]] = [
    ("model", sa.String(255)),
    ("tool_name", sa.String(255)),
    ("tokens_in", sa.BigInteger()),
    ("tokens_out", sa.BigInteger()),
    ("cost_usd", sa.Float()),
    ("failure_id", sa.String(255)),
]
BATCH = 1000
COUNTERS = ["event_count", "llm_calls", "tool_calls", "errors", "error_events"]

# Lightweight table definitions for the backfill (independent of the app models).
events = sa.table(
    "events",
    sa.column("span_id", sa.String),
    sa.column("run_id", sa.String),
    sa.column("kind", sa.String),
    sa.column("status", sa.String),
    sa.column("start_time_ns", sa.BigInteger),
    sa.column("end_time_ns", sa.BigInteger),
    sa.column("attributes", sa.JSON),
    *(sa.column(name, type_) for name, type_ in EVENT_COLUMNS),
)
runs = sa.table(
    "runs",
    sa.column("id", sa.String),
    sa.column("last_seen_ns", sa.BigInteger),
    sa.column("cost_usd", sa.Float),
    sa.column("tokens_in", sa.BigInteger),
    sa.column("tokens_out", sa.BigInteger),
    *(sa.column(c, sa.Integer) for c in COUNTERS),
)


def _num(value: Any, cast: type) -> Any:
    if value is None or isinstance(value, bool):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)[:255]


def _columns(kind: str, attrs: dict[str, Any]) -> dict[str, Any]:
    # Same rules as agentflight.server.service.extract_columns at the time of writing.
    llm = kind == "llm"
    return {
        "model": _text(attrs.get("agentflight.llm.model")) if llm else None,
        "tool_name": _text(attrs.get("agentflight.tool.name")) if kind == "tool" else None,
        "tokens_in": _num(attrs.get("agentflight.llm.tokens_in"), int) if llm else None,
        "tokens_out": _num(attrs.get("agentflight.llm.tokens_out"), int) if llm else None,
        "cost_usd": _num(attrs.get("agentflight.llm.cost_usd"), float) if llm else None,
        "failure_id": _text(attrs.get("agentflight.failure.id")),
    }


def _backfill_events(conn: sa.Connection) -> None:
    # Keyset pagination so large tables aren't loaded into memory at once.
    after: tuple[str, str] | None = None
    while True:
        query = sa.select(
            events.c.run_id, events.c.span_id, events.c.kind, events.c.attributes
        ).order_by(events.c.run_id, events.c.span_id)
        if after is not None:
            query = query.where(sa.tuple_(events.c.run_id, events.c.span_id) > after)
        rows = conn.execute(query.limit(BATCH)).all()
        if not rows:
            return
        for run_id, span_id, kind, attrs in rows:
            values = _columns(kind, attrs if isinstance(attrs, dict) else {})
            if any(v is not None for v in values.values()):
                conn.execute(
                    events.update()
                    .where(events.c.span_id == span_id, events.c.run_id == run_id)
                    .values(**values)
                )
        after = (rows[-1][0], rows[-1][1])


def _backfill_runs(conn: sa.Connection) -> None:
    is_error = events.c.status == "error"
    failure_key = sa.case(
        (is_error, sa.func.coalesce(events.c.failure_id, "span:" + events.c.span_id))
    )
    totals = sa.select(
        events.c.run_id,
        sa.func.count(),
        sa.func.coalesce(sa.func.sum(sa.case((events.c.kind == "llm", 1), else_=0)), 0),
        sa.func.coalesce(sa.func.sum(sa.case((events.c.kind == "tool", 1), else_=0)), 0),
        sa.func.count(sa.distinct(failure_key)),
        sa.func.coalesce(sa.func.sum(sa.case((is_error, 1), else_=0)), 0),
        sa.func.coalesce(sa.func.sum(events.c.tokens_in), 0),
        sa.func.coalesce(sa.func.sum(events.c.tokens_out), 0),
        sa.func.sum(events.c.cost_usd),
        # No ingest times were stored before this migration; use the latest span time.
        sa.func.max(sa.func.coalesce(events.c.end_time_ns, events.c.start_time_ns)),
    ).group_by(events.c.run_id)
    for row in conn.execute(totals).all():
        run_id, *counts, tokens_in, tokens_out, cost, last_seen = row
        conn.execute(
            runs.update()
            .where(runs.c.id == run_id)
            .values(
                **{c: int(v) for c, v in zip(COUNTERS, counts, strict=True)},
                tokens_in=int(tokens_in),
                tokens_out=int(tokens_out),
                cost_usd=None if cost is None else float(cost),
                last_seen_ns=last_seen,
            )
        )


def upgrade() -> None:
    with op.batch_alter_table("events") as batch:
        for name, type_ in EVENT_COLUMNS:
            batch.add_column(sa.Column(name, type_))
        batch.create_index("ix_events_model", ["model"])
        batch.create_index("ix_events_tool_name", ["tool_name"])
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("last_seen_ns", sa.BigInteger))
        for name in COUNTERS:
            batch.add_column(sa.Column(name, sa.Integer, nullable=False, server_default="0"))
        batch.add_column(sa.Column("tokens_in", sa.BigInteger, nullable=False, server_default="0"))
        batch.add_column(sa.Column("tokens_out", sa.BigInteger, nullable=False, server_default="0"))
        batch.add_column(sa.Column("cost_usd", sa.Float))
        batch.create_index("ix_runs_name", ["name"])
        batch.create_index("ix_runs_status", ["status"])

    conn = op.get_bind()
    _backfill_events(conn)
    _backfill_runs(conn)


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_index("ix_runs_status")
        batch.drop_index("ix_runs_name")
        for name in ["cost_usd", "tokens_out", "tokens_in", *reversed(COUNTERS), "last_seen_ns"]:
            batch.drop_column(name)
    with op.batch_alter_table("events") as batch:
        batch.drop_index("ix_events_tool_name")
        batch.drop_index("ix_events_model")
        for name, _ in reversed(EVENT_COLUMNS):
            batch.drop_column(name)
