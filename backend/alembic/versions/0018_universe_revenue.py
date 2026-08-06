"""Trailing revenue and net income on the universe rows.

Both were computed during the ingest and then discarded once the ratios were
derived from them, which loses the two figures a reader most often wants to see
next to a margin — and, for US rows, the only size ranking available at all.
No bulk source publishes US prices, so a US row has no market capitalisation
and revenue is what stands in for it.

Revision ID: 0018_universe_revenue
Revises: 0017_universe_indexes
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_universe_revenue"
down_revision = "0017_universe_indexes"
branch_labels = None
depends_on = None

BIGMONEY = sa.Numeric(24, 2)


def upgrade() -> None:
    op.add_column("asset_universe", sa.Column("revenue", BIGMONEY, nullable=True))
    op.add_column("asset_universe", sa.Column("net_income", BIGMONEY, nullable=True))


def downgrade() -> None:
    op.drop_column("asset_universe", "net_income")
    op.drop_column("asset_universe", "revenue")
