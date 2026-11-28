"""runs.parent_run_id, parent_span_id, parent_source: sub-agent run links

New nullable columns, NULL for existing runs (nothing recorded a parent before), and an
index on parent_run_id for listing a run's children.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("parent_run_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("parent_span_id", sa.String(16), nullable=True))
        batch.add_column(sa.Column("parent_source", sa.String(16), nullable=True))
        batch.create_index("ix_runs_parent_run_id", ["parent_run_id"])


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_index("ix_runs_parent_run_id")
        batch.drop_column("parent_source")
        batch.drop_column("parent_span_id")
        batch.drop_column("parent_run_id")
