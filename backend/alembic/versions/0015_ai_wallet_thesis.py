"""AI wallet category thesis: the model's own strategy, remembered.

The generation turn now returns a ``strategy`` alongside the allocation; it is
stored here and fed back to the model on every suggestion run, so the wallet
keeps pursuing the goal it set for itself.

Revision ID: 0015_ai_wallet_thesis
Revises: 0014_ai_wallet_pending
Create Date: 2026-08-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_ai_wallet_thesis"
down_revision = "0014_ai_wallet_pending"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_wallet_categories", sa.Column("thesis", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_wallet_categories", "thesis")
