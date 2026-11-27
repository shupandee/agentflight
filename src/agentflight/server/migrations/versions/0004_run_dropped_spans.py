"""runs.dropped_spans: spans the SDK dropped before the run ended

New column, 0 for existing runs: SDKs before this release never reported drops, so there
is nothing to backfill.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(
            sa.Column("dropped_spans", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("dropped_spans")
