"""ai_chats.ticker becomes nullable: null = a conversation about the whole portfolio.

Revision ID: 0012_ai_chat_ticker_nullable
Revises: 0011_ai_chats
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_ai_chat_ticker_nullable"
down_revision = "0011_ai_chats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table emits the same ALTER COLUMN on Postgres and a table
    # rebuild on SQLite, where ALTER COLUMN does not exist.
    with op.batch_alter_table("ai_chats") as batch:
        batch.alter_column("ticker", existing_type=sa.String(40), nullable=True)


def downgrade() -> None:
    # Portfolio-wide chats have no ticker; give them a sentinel so NOT NULL holds.
    op.execute("UPDATE ai_chats SET ticker = 'CARTEIRA' WHERE ticker IS NULL")
    with op.batch_alter_table("ai_chats") as batch:
        batch.alter_column("ticker", existing_type=sa.String(40), nullable=False)
