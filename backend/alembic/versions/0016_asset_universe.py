"""The local asset universe: every listed instrument, from official bulk files.

The app knows a great deal about the assets a portfolio holds and nothing at all
about the ones it does not, so no question of the form "which papers look like
this" can be answered locally. This table is that missing index, built from B3
COTAHIST, CVM open data and the SEC ticker registry.

It holds only public, reproducible data — no user history — which is why it is
excluded from the ``.gumbinvest`` export and why dropping it is safe: the cost
is one ingest. There is deliberately no foreign key to ``assets``; the ingest
must never create Asset rows, because an asset without transactions is treated
as watch-only and pulled into the half-hourly quote refresh.

Revision ID: 0016_asset_universe
Revises: 0015_ai_wallet_thesis
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_asset_universe"
down_revision = "0015_ai_wallet_thesis"
branch_labels = None
depends_on = None

#: Dimensionless ratios and published percentages.
RATIO = sa.Numeric(18, 6)
#: Caps, volumes and share counts — whole units, so a trillion-real figure
#: still round-trips exactly through SQLite's float-backed Numeric storage.
BIGMONEY = sa.Numeric(24, 2)
MONEY = sa.Numeric(20, 6)


def upgrade() -> None:
    op.create_table(
        "asset_universe",
        sa.Column("id", sa.Integer(), nullable=False),
        # identity
        sa.Column("ticker", sa.String(length=40), nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False, server_default="B3"),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=24), nullable=False, server_default="OTHER"),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="BRL"),
        sa.Column("market_symbol", sa.String(length=60), nullable=True),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("cnpj", sa.String(length=14), nullable=True),
        sa.Column("cvm_code", sa.String(length=12), nullable=True),
        sa.Column("cik", sa.String(length=10), nullable=True),
        sa.Column("sector", sa.String(length=120), nullable=True),
        sa.Column("b3_segment", sa.String(length=60), nullable=True),
        sa.Column("fund_segment", sa.String(length=60), nullable=True),
        sa.Column("fii_management", sa.String(length=24), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="ATIVO"),
        # market metrics — all nullable: NULL is "not published", never zero
        sa.Column("price", MONEY, nullable=True),
        sa.Column("price_date", sa.Date(), nullable=True),
        sa.Column("market_cap", BIGMONEY, nullable=True),
        sa.Column("avg_volume_21d", BIGMONEY, nullable=True),
        sa.Column("price_change_12m_pct", RATIO, nullable=True),
        sa.Column("high_52w", MONEY, nullable=True),
        sa.Column("low_52w", MONEY, nullable=True),
        sa.Column("volatility_12m_pct", RATIO, nullable=True),
        sa.Column("traded_days_12m", sa.Integer(), nullable=True),
        # fundamentals
        sa.Column("shares_outstanding", BIGMONEY, nullable=True),
        sa.Column("book_value_per_share", MONEY, nullable=True),
        sa.Column("pe", RATIO, nullable=True),
        sa.Column("pb", RATIO, nullable=True),
        sa.Column("roe_pct", RATIO, nullable=True),
        sa.Column("net_margin_pct", RATIO, nullable=True),
        sa.Column("gross_margin_pct", RATIO, nullable=True),
        sa.Column("revenue_growth_pct", RATIO, nullable=True),
        sa.Column("earnings_growth_pct", RATIO, nullable=True),
        sa.Column("debt_to_equity", RATIO, nullable=True),
        sa.Column("dividend_yield_pct", RATIO, nullable=True),
        sa.Column("payout_pct", RATIO, nullable=True),
        sa.Column("fii_pl", BIGMONEY, nullable=True),
        # provenance
        sa.Column("identity_source", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("identity_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("price_source", sa.String(length=32), nullable=True),
        sa.Column("price_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fundamentals_source", sa.String(length=32), nullable=True),
        sa.Column("fundamentals_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fundamentals_period", sa.String(length=12), nullable=True),
        sa.Column("notes", sa.String(length=200), nullable=True),
        sa.Column("extras", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticker", name="uq_asset_universe_ticker"),
    )
    op.create_index("ix_asset_universe_ticker", "asset_universe", ["ticker"])
    op.create_index("ix_asset_universe_market", "asset_universe", ["market"])
    op.create_index("ix_asset_universe_kind", "asset_universe", ["kind"])
    op.create_index("ix_asset_universe_sector", "asset_universe", ["sector"])
    op.create_index("ix_asset_universe_status", "asset_universe", ["status"])
    op.create_index("ix_asset_universe_isin", "asset_universe", ["isin"])
    op.create_index("ix_asset_universe_cnpj", "asset_universe", ["cnpj"])
    op.create_index("ix_asset_universe_price_fetched_at", "asset_universe", ["price_fetched_at"])
    op.create_index("ix_asset_universe_kind_currency", "asset_universe", ["kind", "currency"])
    op.create_index("ix_asset_universe_market_status", "asset_universe", ["market", "status"])


def downgrade() -> None:
    op.drop_table("asset_universe")
