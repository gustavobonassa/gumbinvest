"""B3 index membership on each universe row.

Which indices a paper belongs to is the sharpest filter B3 publishes — "ações
do IBOV", "small caps do SMLL", "FIIs do IFIX" — and the whole market's
memberships arrive in a single request, so it costs nothing to keep current.

Stored comma-delimited and comma-bounded (",IBOV,IBRA,") so a token match is a
portable ``LIKE '%,IBOV,%'`` rather than a dialect-specific array or a JSON
path that casts differently on SQLite and Postgres.

Revision ID: 0017_universe_indexes
Revises: 0016_asset_universe
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_universe_indexes"
down_revision = "0016_asset_universe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("asset_universe", sa.Column("indexes", sa.String(length=400), nullable=True))


def downgrade() -> None:
    op.drop_column("asset_universe", "indexes")
