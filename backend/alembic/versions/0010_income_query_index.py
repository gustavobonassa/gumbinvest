"""Composite index for income queries filtering portfolio + op_type + date.

Revision ID: 0010_income_query_index
Revises: 0009_asset_fundamentals
Create Date: 2026-08-02
"""
from __future__ import annotations

from alembic import op

revision = "0010_income_query_index"
down_revision = "0009_asset_fundamentals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_transactions_portfolio_op_date",
        "transactions",
        ["portfolio_id", "op_type", "trade_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_portfolio_op_date", table_name="transactions")
