"""AI-found corporate events awaiting the user's approval.

The AI scan proposes successions (renames, mergers, delistings) for the
portfolio's own tickers; each proposal is stored here so the user can accept
or decline it — now or later. Accepting writes the real ``asset_successions``
row; nothing touches the history before that.

Revision ID: 0020_succession_ai_suggestions
Revises: 0019_quote_attempts
Create Date: 2026-08-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_succession_ai_suggestions"
down_revision = "0019_quote_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "succession_ai_suggestions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "portfolio_id",
            sa.Integer(),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_ticker", sa.String(length=40), nullable=False),
        sa.Column("to_ticker", sa.String(length=40), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("cash_amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_succession_ai_suggestions_portfolio_id", "succession_ai_suggestions", ["portfolio_id"]
    )
    op.create_index(
        "ix_succession_ai_suggestions_status", "succession_ai_suggestions", ["status"]
    )


def downgrade() -> None:
    op.drop_table("succession_ai_suggestions")
