"""Separate an import's warnings from its failures

An import's ``rows_failed`` was the length of its whole issue log, and for a
broker statement that log also holds the parser's *notes* — an ambiguity the
importer resolved and wants a human to know about. So a statement that imported
perfectly reported "1 com erro", which reads as lost data and is not.

Revision ID: 0023_import_warnings
Revises: 0022_notifications
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_import_warnings"
down_revision = "0022_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "import_batches",
        sa.Column("rows_warned", sa.Integer(), nullable=False, server_default="0"),
    )
    # Existing rows counted notes as failures. The log is still there, but it
    # predates the level tag, so the split cannot be recovered — leaving the old
    # counts alone is the honest option; re-importing a file rewrites them.


def downgrade() -> None:
    op.drop_column("import_batches", "rows_warned")
