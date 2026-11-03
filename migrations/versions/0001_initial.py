"""initial schema: runs and events

Revision ID: 0001
Revises:
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(255)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("root_span_id", sa.String(16)),
        sa.Column("start_time_ns", sa.BigInteger),
        sa.Column("end_time_ns", sa.BigInteger),
        sa.Column("metadata", JSONType, nullable=False),
    )
    op.create_index("ix_runs_start_time_ns", "runs", ["start_time_ns"])
    op.create_table(
        "events",
        sa.Column("span_id", sa.String(16), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(32),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("parent_span_id", sa.String(16)),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_message", sa.Text),
        sa.Column("start_time_ns", sa.BigInteger, nullable=False),
        sa.Column("end_time_ns", sa.BigInteger),
        sa.Column("attributes", JSONType, nullable=False),
        sa.Column("span_events", JSONType, nullable=False),
    )
    op.create_index("ix_events_run_start", "events", ["run_id", "start_time_ns"])


def downgrade() -> None:
    op.drop_index("ix_events_run_start", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_runs_start_time_ns", table_name="runs")
    op.drop_table("runs")
