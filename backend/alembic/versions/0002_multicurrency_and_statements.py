"""Multi-currency transactions, broker statement metadata and FX rates.

Adds what the offshore brokers need:

* ``transactions.currency`` / ``fx_rate`` — amounts stay in the currency they
  happened in, with the trade-date PTAX rate stored alongside so a cost basis in
  reais is reproducible;
* ``assets.cusip`` — US statements identify securities by CUSIP only;
* statement columns on ``import_batches`` — broker, account, period and the
  opening/closing balances that drive gap detection;
* ``fx_rates`` — daily PTAX series from Banco Central.

Existing rows are backfilled to BRL, which is what they all are: the only
importer before this migration read the B3 export.

Revision ID: 0002_multicurrency
Revises: 0001_initial
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_multicurrency"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="BRL"),
    )
    op.add_column(
        "transactions", sa.Column("fx_rate", sa.Numeric(precision=18, scale=8), nullable=True)
    )
    op.add_column("assets", sa.Column("cusip", sa.String(length=12), nullable=True))
    op.create_index(op.f("ix_assets_cusip"), "assets", ["cusip"], unique=False)

    op.add_column(
        "import_batches",
        sa.Column("source_kind", sa.String(length=8), nullable=False, server_default="CSV"),
    )
    op.add_column("import_batches", sa.Column("source_format", sa.String(length=32), nullable=True))
    op.add_column("import_batches", sa.Column("broker_name", sa.String(length=160), nullable=True))
    op.add_column("import_batches", sa.Column("account_ref", sa.String(length=60), nullable=True))
    op.add_column("import_batches", sa.Column("currency", sa.String(length=8), nullable=True))
    op.add_column("import_batches", sa.Column("period_start", sa.Date(), nullable=True))
    op.add_column("import_batches", sa.Column("period_end", sa.Date(), nullable=True))
    op.add_column(
        "import_batches",
        sa.Column("opening_balance", sa.Numeric(precision=20, scale=6), nullable=True),
    )
    op.add_column(
        "import_batches",
        sa.Column("closing_balance", sa.Numeric(precision=20, scale=6), nullable=True),
    )
    op.create_index(
        op.f("ix_import_batches_source_kind"), "import_batches", ["source_kind"], unique=False
    )
    op.create_index(
        op.f("ix_import_batches_broker_name"), "import_batches", ["broker_name"], unique=False
    )
    op.create_index(
        op.f("ix_import_batches_period_start"), "import_batches", ["period_start"], unique=False
    )
    op.create_index(
        op.f("ix_import_batches_period_end"), "import_batches", ["period_end"], unique=False
    )

    op.create_table(
        "fx_rates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("base", sa.String(length=8), nullable=False),
        sa.Column("quote", sa.String(length=8), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("rate", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False, server_default="bcb-ptax"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("base", "quote", "date", name="uq_fx_rate_pair_date"),
    )
    op.create_index(op.f("ix_fx_rates_base"), "fx_rates", ["base"], unique=False)
    op.create_index(op.f("ix_fx_rates_quote"), "fx_rates", ["quote"], unique=False)
    op.create_index(op.f("ix_fx_rates_date"), "fx_rates", ["date"], unique=False)
    op.create_index("ix_fx_rates_pair_date", "fx_rates", ["base", "quote", "date"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_fx_rates_pair_date", table_name="fx_rates")
    op.drop_index(op.f("ix_fx_rates_date"), table_name="fx_rates")
    op.drop_index(op.f("ix_fx_rates_quote"), table_name="fx_rates")
    op.drop_index(op.f("ix_fx_rates_base"), table_name="fx_rates")
    op.drop_table("fx_rates")

    op.drop_index(op.f("ix_import_batches_period_end"), table_name="import_batches")
    op.drop_index(op.f("ix_import_batches_period_start"), table_name="import_batches")
    op.drop_index(op.f("ix_import_batches_broker_name"), table_name="import_batches")
    op.drop_index(op.f("ix_import_batches_source_kind"), table_name="import_batches")
    for column in (
        "closing_balance",
        "opening_balance",
        "period_end",
        "period_start",
        "currency",
        "account_ref",
        "broker_name",
        "source_format",
        "source_kind",
    ):
        op.drop_column("import_batches", column)

    op.drop_index(op.f("ix_assets_cusip"), table_name="assets")
    op.drop_column("assets", "cusip")
    op.drop_column("transactions", "fx_rate")
    op.drop_column("transactions", "currency")
