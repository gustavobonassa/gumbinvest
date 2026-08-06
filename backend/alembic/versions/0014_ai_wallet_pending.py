"""Deferred AI-wallet buys: money reserved for a ticker with no quote yet.

A resolvable asset whose quote fetch failed (Yahoo rate limits burst lookups)
used to be skipped outright. Now the allocation is reserved in ``pending_brl``
and settles into shares automatically once a price arrives.

Revision ID: 0014_ai_wallet_pending
Revises: 0013_ai_wallets
Create Date: 2026-08-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_ai_wallet_pending"
down_revision = "0013_ai_wallets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_wallet_positions",
        sa.Column("pending_brl", sa.Numeric(20, 6), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("ai_wallet_positions", "pending_brl")
