"""runs whose root span ended without a status: ok -> unknown

Before this revision a root span with status "unset" was stored as "ok". It is now
"unknown"; this fixes runs stored earlier. Downgrade maps "unknown" back to "ok".

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

runs = sa.table(
    "runs",
    sa.column("id", sa.String),
    sa.column("status", sa.String),
    sa.column("root_span_id", sa.String),
)
events = sa.table(
    "events",
    sa.column("span_id", sa.String),
    sa.column("run_id", sa.String),
    sa.column("status", sa.String),
)


def upgrade() -> None:
    unset_root = (
        sa.select(events.c.span_id)
        .where(
            events.c.run_id == runs.c.id,
            events.c.span_id == runs.c.root_span_id,
            events.c.status == "unset",
        )
        .exists()
    )
    op.execute(runs.update().where(runs.c.status == "ok", unset_root).values(status="unknown"))


def downgrade() -> None:
    op.execute(runs.update().where(runs.c.status == "unknown").values(status="ok"))
