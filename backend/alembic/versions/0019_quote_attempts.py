"""The retry queue for quote fetches that failed transiently.

A symbol missing from a provider's answer used to mean two different things —
"no public price exists" and "the request timed out" — and the second was shown
to the user as the first. A row here is the second case, and only that: it is
created when the provider reports a transient failure and deleted the moment a
price arrives, so the table is empty whenever nothing is wrong.

Revision ID: 0019_quote_attempts
Revises: 0018_universe_revenue
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_quote_attempts"
down_revision = "0018_universe_revenue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quote_attempts",
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column(
            "first_failed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("asset_id"),
    )


def downgrade() -> None:
    op.drop_table("quote_attempts")
