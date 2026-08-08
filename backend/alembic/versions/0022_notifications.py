"""Stored history behind the header bell.

The bell used to render only derived state, which capped it at a handful of
entries and left nothing to scroll. This table is where events that *happened*
land, so the panel can page back through them and the user can mark them read
or archive them.

Revision ID: 0022_notifications
Revises: 0021_smart_invest_runs
Create Date: 2026-08-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_notifications"
down_revision = "0021_smart_invest_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("level", sa.String(length=12), nullable=False, server_default="info"),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column(
            "portfolio_id",
            sa.Integer(),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            nullable=True,
        ),
        # Named explicitly: SQLite rewrites a table to alter it, and an
        # auto-named constraint is one the batch migration cannot find again.
        sa.Column("dedup_key", sa.String(length=160), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("dedup_key", name="uq_notifications_dedup_key"),
    )
    op.create_index("ix_notifications_at", "notifications", ["at"])
    op.create_index("ix_notifications_kind", "notifications", ["kind"])
    op.create_index("ix_notifications_portfolio_id", "notifications", ["portfolio_id"])


def downgrade() -> None:
    op.drop_table("notifications")
