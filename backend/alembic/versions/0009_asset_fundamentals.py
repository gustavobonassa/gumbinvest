"""Cache company fundamentals per asset.

The upstream APIs are rate limited and the data moves quarterly, so the payload
is stored whole and refreshed on a TTL. Purely a cache — dropping the table
costs one refresh per asset.

Revision ID: 0009_asset_fundamentals
Revises: 0008_cash_accounts
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_asset_fundamentals"
down_revision = "0008_cash_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_fundamentals",
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="yahoo"),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("asset_id"),
    )


def downgrade() -> None:
    op.drop_table("asset_fundamentals")
