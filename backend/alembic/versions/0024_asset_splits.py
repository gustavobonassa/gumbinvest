"""Share splits as the exchange declared them.

``price_history`` is written in today's shares — a provider divides the whole
pre-split series by the ratio — while the ledger counts the shares that existed
on each date. Multiplying one by the other valued every day before a split at a
fraction of the truth, and the fraction was the ratio: a 6-for-1 printed an 83%
loss that never happened, and a per-class return turned that into -4300%.

Not derived from the ledger, though statements do report splits: a statement
gives the quantity credited to one broker's sleeve, so the same event arrives
split across brokers and sometimes classified as a purchase.

Revision ID: 0024_asset_splits
Revises: 0023_import_warnings
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_asset_splits"
down_revision = "0023_import_warnings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_splits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("ratio", sa.Numeric(18, 8), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", "date", name="uq_asset_split_day"),
    )
    op.create_index("ix_asset_splits_asset_id", "asset_splits", ["asset_id"])


def downgrade() -> None:
    op.drop_index("ix_asset_splits_asset_id", table_name="asset_splits")
    op.drop_table("asset_splits")
