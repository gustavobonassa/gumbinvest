"""AI-managed virtual wallets: positions, suggestions, events, snapshots.

Revision ID: 0013_ai_wallets
Revises: 0012_ai_chat_ticker_nullable
Create Date: 2026-08-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_ai_wallets"
down_revision = "0012_ai_chat_ticker_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_wallets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "ai_wallet_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "wallet_id",
            sa.Integer(),
            sa.ForeignKey("ai_wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column("budget", sa.Numeric(20, 6), nullable=False),
        sa.Column("cash", sa.Numeric(20, 6), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("wallet_id", "category", name="uq_ai_wallet_category"),
    )
    op.create_index("ix_ai_wallet_categories_wallet_id", "ai_wallet_categories", ["wallet_id"])

    op.create_table(
        "ai_wallet_positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "wallet_id",
            sa.Integer(),
            sa.ForeignKey("ai_wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column(
            "asset_id",
            sa.Integer(),
            sa.ForeignKey("assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ticker", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("avg_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("avg_fx", sa.Numeric(18, 8), nullable=True),
        sa.Column("cost_brl", sa.Numeric(20, 6), nullable=False),
        sa.Column("is_fixed_income", sa.Boolean(), nullable=False),
        sa.Column("fi_index_code", sa.String(length=16), nullable=True),
        sa.Column("fi_percent_of_index", sa.Numeric(10, 4), nullable=True),
        sa.Column("fi_spread_annual", sa.Numeric(10, 4), nullable=True),
        sa.Column("fi_fixed_rate_annual", sa.Numeric(10, 4), nullable=True),
        sa.Column("fi_start_date", sa.Date(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ai_wallet_positions_wallet_id", "ai_wallet_positions", ["wallet_id"])
    op.create_index("ix_ai_wallet_positions_category", "ai_wallet_positions", ["category"])
    op.create_index("ix_ai_wallet_positions_asset_id", "ai_wallet_positions", ["asset_id"])

    op.create_table(
        "ai_wallet_suggestions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "wallet_id",
            sa.Integer(),
            sa.ForeignKey("ai_wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("ticker", sa.String(length=40), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("amount_brl", sa.Numeric(20, 6), nullable=True),
        sa.Column("to_ticker", sa.String(length=40), nullable=True),
        sa.Column("to_category", sa.String(length=24), nullable=True),
        sa.Column(
            "to_position_id",
            sa.Integer(),
            sa.ForeignKey("ai_wallet_positions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "position_id",
            sa.Integer(),
            sa.ForeignKey("ai_wallet_positions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ai_wallet_suggestions_wallet_id", "ai_wallet_suggestions", ["wallet_id"])
    op.create_index("ix_ai_wallet_suggestions_category", "ai_wallet_suggestions", ["category"])
    op.create_index("ix_ai_wallet_suggestions_batch_id", "ai_wallet_suggestions", ["batch_id"])
    op.create_index("ix_ai_wallet_suggestions_status", "ai_wallet_suggestions", ["status"])

    op.create_table(
        "ai_wallet_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "wallet_id",
            sa.Integer(),
            sa.ForeignKey("ai_wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(length=24), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
    )
    op.create_index("ix_ai_wallet_events_wallet_id", "ai_wallet_events", ["wallet_id"])
    op.create_index("ix_ai_wallet_events_at", "ai_wallet_events", ["at"])
    op.create_index("ix_ai_wallet_events_action", "ai_wallet_events", ["action"])

    op.create_table(
        "ai_wallet_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "wallet_id",
            sa.Integer(),
            sa.ForeignKey("ai_wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(20, 6), nullable=False),
        sa.Column("invested", sa.Numeric(20, 6), nullable=False),
        sa.Column("cash", sa.Numeric(20, 6), nullable=False),
        sa.Column("return_factor", sa.Numeric(28, 16), nullable=False),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.UniqueConstraint("wallet_id", "date", name="uq_ai_wallet_snapshot_date"),
    )
    op.create_index("ix_ai_wallet_snapshots_wallet_id", "ai_wallet_snapshots", ["wallet_id"])
    op.create_index("ix_ai_wallet_snapshots_date", "ai_wallet_snapshots", ["date"])


def downgrade() -> None:
    for table in (
        "ai_wallet_snapshots",
        "ai_wallet_events",
        "ai_wallet_suggestions",
        "ai_wallet_positions",
        "ai_wallet_categories",
        "ai_wallets",
    ):
        op.drop_table(table)
