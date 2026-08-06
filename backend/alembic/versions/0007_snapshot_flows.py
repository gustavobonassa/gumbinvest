"""Store the flow weights the money-weighted return needs.

A time-weighted return treats a bad year holding R$ 20 mil exactly like a bad
year holding R$ 800 mil. Modified Dietz does not: every contribution counts for
the fraction of the window it was actually invested. Doing that for an arbitrary
window needs two running sums per day — the net flow, and the same flow
multiplied by its date — from which any window's weighted capital falls out in
one subtraction.

Revision ID: 0007_snapshot_flows
Revises: 0006_snapshot_returns
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_snapshot_flows"
down_revision = "0006_snapshot_returns"
branch_labels = None
depends_on = None

TABLE = "portfolio_snapshots"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("flow", sa.Numeric(20, 6), nullable=False, server_default="0"))
    op.add_column(
        TABLE, sa.Column("flow_time", sa.Numeric(30, 6), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column(TABLE, "flow_time")
    op.drop_column(TABLE, "flow")
