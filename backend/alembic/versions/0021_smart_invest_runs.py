"""History of aporte-inteligente analyses.

The background job's registry forgets a result after an hour; this table is
the durable copy, one row per finished run with the full rendered payload.

Revision ID: 0021_smart_invest_runs
Revises: 0020_succession_ai_suggestions
Create Date: 2026-08-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_smart_invest_runs"
down_revision = "0020_succession_ai_suggestions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "smart_invest_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "portfolio_id",
            sa.Integer(),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_smart_invest_runs_portfolio_id", "smart_invest_runs", ["portfolio_id"])


def downgrade() -> None:
    op.drop_table("smart_invest_runs")
