"""Who pays, in the only form the Receita accepts.

Every line of the IRPF worksheet names a payer by CNPJ: the company behind a
dividend, the administrator behind a fund's yield, the bank behind a CDB. Most
of them are already known — ``asset_universe`` carries the registry B3 and the
CVM publish, and it covers every Brazilian asset in this ledger that paid
anything. What it cannot cover is the rest: a private CDB's issuer, a cash
account's bank, the exchange holding the crypto. None of those are listed
anywhere the universe reaches, so the number has to be able to come from a
person.

An override rather than a copy: the universe stays the source, and this column
only answers where the source is silent or wrong.

Revision ID: 0025_asset_cnpj
Revises: 0024_asset_splits
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_asset_cnpj"
down_revision = "0024_asset_splits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("cnpj", sa.String(length=14), nullable=True))


def downgrade() -> None:
    op.drop_column("assets", "cnpj")
