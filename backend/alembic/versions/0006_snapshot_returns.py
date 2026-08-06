"""Store the daily return chain alongside the value snapshots.

The rentabilidade chart needs a time-weighted return for every day, which means
replaying six years of daily prices. Doing that per request costs a second and a
half; doing it once and keeping the cumulative factor per day turns every range
into two divisions.

Revision ID: 0006_snapshot_returns
Revises: 0005_merge
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_snapshot_returns"
down_revision = "0005_merge"
branch_labels = None
depends_on = None

TABLE = "portfolio_snapshots"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("return_factor", sa.Numeric(28, 16), nullable=False, server_default="1"),
    )
    op.add_column(
        TABLE,
        sa.Column("priced_value", sa.Numeric(20, 6), nullable=False, server_default="0"),
    )
    op.add_column(TABLE, sa.Column("kind_state", sa.JSON(), nullable=True))
    op.add_column(
        TABLE,
        sa.Column("fingerprint", sa.String(64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    for column in ("fingerprint", "kind_state", "priced_value", "return_factor"):
        op.drop_column(TABLE, column)
