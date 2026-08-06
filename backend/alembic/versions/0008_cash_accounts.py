"""Mark assets that are hand-kept bank balances rather than traded instruments.

Money sitting in a bank account is fixed income — it accrues against the CDI and
belongs in the net worth — but no export reaches it. Modelling it as an asset
with deposits and withdrawals means every screen already knows what to do with
it; the flag is what keeps the importer from rewriting a balance's kind and what
selects the right accrual rule.

Revision ID: 0008_cash_accounts
Revises: 0007_snapshot_flows
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_cash_accounts"
down_revision = "0007_snapshot_flows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assets",
        sa.Column("is_cash_account", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("assets", "is_cash_account")
