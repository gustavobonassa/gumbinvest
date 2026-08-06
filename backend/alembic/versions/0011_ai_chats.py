"""Saved AI-analyst conversations.

Revision ID: 0011_ai_chats
Revises: 0010_income_query_index
Create Date: 2026-08-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_ai_chats"
down_revision = "0010_income_query_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_chats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "portfolio_id",
            sa.Integer(),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ai_chats_portfolio_id", "ai_chats", ["portfolio_id"])
    op.create_index("ix_ai_chats_ticker", "ai_chats", ["ticker"])


def downgrade() -> None:
    op.drop_index("ix_ai_chats_ticker", table_name="ai_chats")
    op.drop_index("ix_ai_chats_portfolio_id", table_name="ai_chats")
    op.drop_table("ai_chats")
