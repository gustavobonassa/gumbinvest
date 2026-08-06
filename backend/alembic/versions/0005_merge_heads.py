"""Merge the multi-currency and corporate-action branches.

Both descend from ``0001_initial`` and touch disjoint tables, so there is
nothing to reconcile — this only gives Alembic a single head to upgrade to.

Revision ID: 0005_merge
Revises: 0004_successions, 0002_multicurrency
"""
from __future__ import annotations

revision = '0005_merge'
down_revision = ('0004_successions', '0002_multicurrency')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
