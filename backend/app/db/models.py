"""SQLAlchemy ORM models.

Money is stored as ``Numeric`` (never float) so financial arithmetic stays
exact; quantities allow 8 decimals because B3 hands out fractional shares from
splits, bonuses and fraction auctions.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

QTY = Numeric(24, 8)
MONEY = Numeric(20, 6)
#: Dimensionless screening figures — P/L, P/VP, and percentages as published
#: (18.5 means 18,5 %). Never money: these are ratios and must not be summed.
RATIO = Numeric(18, 6)
#: Market caps, traded volume and share counts, quantized to whole units at
#: ingest. Six decimals on a R$ 3,5 trilhão cap would be 19 significant digits,
#: and SQLite stores Numeric through a float that quantizes past 15 (see
#: tests/test_sqlite_decimal.py); whole units keep it at 13 — exact on both.
BIGMONEY = Numeric(24, 2)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Portfolio(Base, TimestampMixin):
    """A logical portfolio. The app seeds a single default portfolio."""

    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    base_currency: Mapped[str] = mapped_column(String(8), default="BRL")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="portfolio")


class Broker(Base, TimestampMixin):
    """Custodian/broker. Raw CSV spellings are normalised to a canonical name."""

    __tablename__ = "brokers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    raw_names: Mapped[list[str]] = mapped_column(JSON, default=list)


class Asset(Base, TimestampMixin):
    """A tradable instrument, keyed by its B3 ticker (or a synthetic code)."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    kind: Mapped[str] = mapped_column(String(24), default="OTHER", index=True)
    #: Currency the asset trades and is booked in ("BRL", "USD").
    currency: Mapped[str] = mapped_column(String(8), default="BRL")
    #: US statements identify securities by CUSIP only, so the mapping learned
    #: from one statement is reused for every later one — see
    #: :mod:`app.importer.pdf.symbols`.
    cusip: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    #: Who the IRPF worksheet names as the payer, when the registry cannot say.
    #: ``asset_universe`` already carries the CNPJ B3 and the CVM publish and it
    #: covers every listed Brazilian asset; this answers for the ones no registry
    #: lists — a private CDB's issuer, a cash account's bank, the exchange
    #: holding the crypto. An override, so the registry stays the source.
    cnpj: Mapped[str | None] = mapped_column(String(14), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Ticker used when querying the market data provider (e.g. PETR4 -> PETR4.SA)
    market_symbol: Mapped[str | None] = mapped_column(String(60), nullable=True)
    #: When true the engine skips live quotes and values the position at cost.
    price_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    #: A balance the user keeps somewhere no export reaches — money sitting in a
    #: bank account, earning the CDI. It is fixed income in every way that
    #: matters (it accrues against an index and belongs in the net worth), but
    #: it is *entered by hand*, so the importer must never rewrite its kind and
    #: its balance accrues by a different rule than a paper's: see
    #: :func:`app.market.fixed_income.value_account`.
    is_cash_account: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Every dependent table declares ON DELETE CASCADE, so deleting an asset is
    # the database's job. Without `passive_deletes` the ORM instead tries to
    # orphan the children by nulling their FK — which cannot work for `quotes`,
    # where `asset_id` *is* the primary key, and would silently detach
    # transactions rather than remove them.
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="asset", passive_deletes=True
    )
    quote: Mapped["Quote | None"] = relationship(
        back_populates="asset", uselist=False, passive_deletes=True
    )


class ImportBatch(Base, TimestampMixin):
    """One uploaded file. Keeps a full audit trail of what happened.

    Broker statements add the columns below. They are what turns a pile of PDFs
    into a coverage map: which broker, which account, which month, and the
    balances the statement opened and closed on — enough to tell a missing month
    from a quiet one. See :mod:`app.importer.coverage`.
    """

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    rows_failed: Mapped[int] = mapped_column(Integer, default=0)
    #: Rows that imported, with something the user should know about them. A
    #: statement note is not a failure: the movement is on file, the importer
    #: just could not do one part of the job (split a tax it was told about but
    #: cannot see) and says so rather than guessing.
    rows_warned: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: Structured per-row issues, newest schema first:
    #: [{"level": "error"|"warning", "line": 12, "error": "...", "raw": {...}}]
    #: Entries written before the level existed are read as errors.
    issues: Mapped[list] = mapped_column(JSON, default=list)
    #: Counts by operation type, unknown movements, date range, etc.
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: "CSV" (B3 export) or "PDF" (broker statement).
    source_kind: Mapped[str] = mapped_column(String(8), default="CSV", index=True)
    #: Parser that read the file, e.g. "apex-en" — see app.importer.pdf.registry.
    source_format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    broker_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    account_ref: Mapped[str | None] = mapped_column(String(60), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    #: Total account equity the statement opened and closed the period with.
    opening_balance: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    closing_balance: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="import_batch")


class Transaction(Base):
    """A single normalised movement.

    ``dedup_key`` is a deterministic hash of the meaningful CSV fields plus an
    occurrence counter, which makes re-importing the same file a no-op while
    still allowing genuinely repeated movements (same asset, day, price and
    quantity) to coexist.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "dedup_key", name="uq_transaction_dedup"),
        Index("ix_transactions_asset_date", "asset_id", "trade_date"),
        Index("ix_transactions_portfolio_date", "portfolio_id", "trade_date"),
        Index("ix_transactions_op_type", "op_type"),
        Index("ix_transactions_portfolio_op_date", "portfolio_id", "op_type", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    broker_id: Mapped[int | None] = mapped_column(ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True)
    import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )

    trade_date: Mapped[date] = mapped_column(Date, index=True)
    direction: Mapped[str] = mapped_column(String(8))
    op_type: Mapped[str] = mapped_column(String(24))
    effect: Mapped[str] = mapped_column(String(20))

    quantity: Mapped[Decimal] = mapped_column(QTY, default=Decimal(0))
    unit_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    gross_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    taxes: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    #: gross ± fees/taxes — the amount that actually moved in the cash account.
    net_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))

    #: Currency the amounts above are recorded in — never converted on the way
    #: in, so the asset page can show a US holding in the dollars it was bought
    #: with. Conversion to the portfolio's base currency happens on read.
    currency: Mapped[str] = mapped_column(String(8), default="BRL")
    #: Units of the portfolio's base currency per unit of ``currency`` on the
    #: trade date (PTAX). Stored rather than looked up later so a historical
    #: cost basis in reais stays reproducible even as rates move. ``None`` when
    #: the movement is already in the base currency.
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)

    raw_movement: Mapped[str] = mapped_column(String(120))
    raw_product: Mapped[str] = mapped_column(String(255))
    raw_institution: Mapped[str] = mapped_column(String(255), default="")
    source_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dedup_key: Mapped[str] = mapped_column(String(80), index=True)
    occurrence: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    portfolio: Mapped[Portfolio] = relationship(back_populates="transactions")
    asset: Mapped[Asset] = relationship(back_populates="transactions")
    broker: Mapped[Broker | None] = relationship()
    import_batch: Mapped[ImportBatch | None] = relationship(back_populates="transactions")


class Quote(Base):
    """Latest known price for an asset (one row per asset)."""

    __tablename__ = "quotes"

    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    price: Mapped[Decimal] = mapped_column(MONEY)
    previous_close: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    change: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    change_percent: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="BRL")
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    long_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    asset: Mapped[Asset] = relationship(back_populates="quote")


class AssetSplit(Base):
    """A share split as the exchange declared it, from the data provider.

    Stored because :class:`PriceHistory` is written in *today's* shares — a
    provider divides the whole pre-split series by the ratio — while the ledger
    counts the shares that existed on each date. Valuing a past holding needs
    both, and without this row the two series are indistinguishable from one
    that never split: a 6-for-1 makes every earlier day look like an 83% loss
    that never happened.

    Not derived from the ledger, though the statements do report splits: a
    statement gives the quantity credited to *one broker's* sleeve, so the same
    6-for-1 arrives as 13.7 shares from one broker and 38.7 from another — and
    sometimes classified as a purchase. The exchange's own ratio is the fact.
    """

    __tablename__ = "asset_splits"
    __table_args__ = (UniqueConstraint("asset_id", "date", name="uq_asset_split_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)
    #: Shares after, per share before: 6 for a 6-for-1, 0.1 for a 1-for-10.
    ratio: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    source: Mapped[str] = mapped_column(String(32), default="unknown")


class QuoteAttempt(Base):
    """A quote fetch that failed transiently and is owed another try.

    Exists so a timeout is never presented to the user as "this asset has no
    price". A row appears when the provider reports a transient failure and is
    deleted the moment a price arrives, so the table is empty in the normal
    case and its contents are exactly the retry queue.

    Persisted rather than kept in memory because the queue has to be visible to
    whichever runtime happens to be draining it — Celery beat under Docker, the
    APScheduler inside the desktop server — and to the API that shows the user
    what is still pending.
    """

    __tablename__ = "quote_attempts"

    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    #: When the queue may pick this row up again. Compared in Python rather
    #: than in SQL — SQLite drops the offset of an aware datetime on the way in
    #: — which the rest of this codebase does for ``fetched_at`` too, and which
    #: costs nothing on a table that is empty whenever nothing is wrong.
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    asset: Mapped[Asset] = relationship()


class AssetFundamentals(Base):
    """Cached company data for an asset (one row per asset).

    Fundamentals move quarterly at best, and the upstream APIs are rate limited,
    so the payload is stored whole and re-fetched on a TTL rather than on every
    page view. It is a cache: dropping the table costs one refresh.
    """

    __tablename__ = "asset_fundamentals"

    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(32), default="yahoo")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssetUniverse(Base, TimestampMixin):
    """Every instrument the market lists — whether or not this portfolio owns it.

    Built by an opt-in background ingest from official bulk files (B3 COTAHIST,
    CVM open data, SEC), never from per-ticker API calls. It is a rebuildable
    index of public data, not user history: it is excluded from the
    ``.gumbinvest`` export for that reason, and dropping it costs one ingest.

    Deliberately **not** joined to ``assets`` by a foreign key, and the ingest
    never creates an ``Asset`` row. ``app.market.service.quotable_assets``
    treats an asset with no transactions as watch-only and refreshes its quote
    every half hour, so minting two thousand of them would turn the quote
    refresh into a rate-limit incident. The join is by ticker at query time,
    exactly like ``watchlist``.

    Every metric is nullable and ``NULL`` means "not published", never zero —
    a screener that reads a missing P/L as 0 ranks the company as the cheapest
    in the market. ``notes`` carries why a figure is absent when it is known.
    """

    __tablename__ = "asset_universe"
    __table_args__ = (
        UniqueConstraint("ticker", name="uq_asset_universe_ticker"),
        Index("ix_asset_universe_kind_currency", "kind", "currency"),
        Index("ix_asset_universe_market_status", "market", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # -- identity ---------------------------------------------------------
    ticker: Mapped[str] = mapped_column(String(40), index=True)
    #: B3 | US. Which bulk pipeline owns the row.
    market: Mapped[str] = mapped_column(String(8), default="B3", index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    #: An AssetKind value. From COTAHIST's CODBDI where available (which knows
    #: HGLG11 is a FII and BOVA11 an ETF), else classify_asset_kind's heuristic.
    kind: Mapped[str] = mapped_column(String(24), default="OTHER", index=True)
    currency: Mapped[str] = mapped_column(String(8), default="BRL")
    market_symbol: Mapped[str | None] = mapped_column(String(60), nullable=True)
    isin: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    #: Digits only — B3 publishes it bare, CVM punctuated; this is the join key.
    cnpj: Mapped[str | None] = mapped_column(String(14), nullable=True, index=True)
    cvm_code: Mapped[str | None] = mapped_column(String(12), nullable=True)
    cik: Mapped[str | None] = mapped_column(String(10), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    b3_segment: Mapped[str | None] = mapped_column(String(60), nullable=True)
    #: FII only: Segmento_Atuacao ("Shoppings", "Lajes Corporativas", "Papel").
    fund_segment: Mapped[str | None] = mapped_column(String(60), nullable=True)
    fii_management: Mapped[str | None] = mapped_column(String(24), nullable=True)
    #: B3 index membership, comma-delimited *and* comma-bounded:
    #: ",IBOV,IBRA,IDIV,". The bounding commas make ``LIKE '%,IBOV,%'`` an
    #: exact-token match on both dialects, where a bare ``%IBOV%`` would also
    #: match a hypothetical IBOVX. One HTTP call fills this for the whole
    #: market, and it is the most useful screening axis B3 publishes: "ações do
    #: IBOV", "small caps do SMLL", "FIIs do IFIX".
    indexes: Mapped[str | None] = mapped_column(String(400), nullable=True)
    #: From the registry (CVM SIT / B3 status) — declared, never inferred.
    #: Wide enough for the registry's own wording: CVM files whole phrases
    #: here, e.g. "SUSPENSO(A) - DECISÃO ADM" at 25 characters.
    status: Mapped[str] = mapped_column(String(40), default="ATIVO", index=True)

    # -- market metrics (COTAHIST) ----------------------------------------
    price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    price_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    market_cap: Mapped[Decimal | None] = mapped_column(BIGMONEY, nullable=True)
    avg_volume_21d: Mapped[Decimal | None] = mapped_column(BIGMONEY, nullable=True)
    price_change_12m_pct: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    high_52w: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    low_52w: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    volatility_12m_pct: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    #: How many sessions the paper actually traded in the window. A 12-month
    #: return computed over 9 sessions is noise, and this is what says so.
    traded_days_12m: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # -- fundamentals (CVM filings for B3, SEC XBRL for the US) -----------
    #: Trailing revenue. Also the size ranking for US rows, which have no
    #: market capitalisation: no bulk source publishes US prices, so nothing
    #: can multiply their share count by one.
    revenue: Mapped[Decimal | None] = mapped_column(BIGMONEY, nullable=True)
    net_income: Mapped[Decimal | None] = mapped_column(BIGMONEY, nullable=True)
    shares_outstanding: Mapped[Decimal | None] = mapped_column(BIGMONEY, nullable=True)
    book_value_per_share: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    pe: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    pb: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    roe_pct: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    net_margin_pct: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    gross_margin_pct: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    revenue_growth_pct: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    earnings_growth_pct: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    debt_to_equity: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    dividend_yield_pct: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    payout_pct: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    #: FII patrimônio líquido, straight from the monthly informe.
    fii_pl: Mapped[Decimal | None] = mapped_column(BIGMONEY, nullable=True)

    # -- provenance -------------------------------------------------------
    identity_source: Mapped[str] = mapped_column(String(32), default="unknown")
    identity_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    price_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    fundamentals_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fundamentals_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Which filing the ratios came from ("2026T1"), so a stale number is
    #: visibly stale rather than quietly presented as current.
    fundamentals_period: Mapped[str | None] = mapped_column(String(12), nullable=True)
    #: Why a metric is missing, in pt-BR. Ambiguity is surfaced, not hidden.
    notes: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Display-only leftovers. Never filtered or sorted on — a JSON path in a
    #: WHERE casts differently on SQLite and Postgres, and on bad data one
    #: silently yields 0.0 while the other raises.
    extras: Mapped[dict] = mapped_column(JSON, default=dict)


class PriceHistory(Base):
    """Daily closes used to reconstruct historical portfolio value."""

    __tablename__ = "price_history"
    __table_args__ = (UniqueConstraint("asset_id", "date", name="uq_price_history_asset_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    close: Mapped[Decimal] = mapped_column(MONEY)
    source: Mapped[str] = mapped_column(String(32), default="unknown")


class AssetSuccession(Base, TimestampMixin):
    """Records that one asset *became* another — merger, ticker change, delisting.

    B3's export credits the new asset but never debits the old one, so both sit
    in the portfolio: the successor holding free shares and the predecessor
    holding a phantom position with all of the cost. This table is what closes
    that loop, and it is user data — the export contains no link between the two
    sides, and inferring one silently would produce confident, wrong numbers.

    ``to_asset_id`` is nullable: a null successor means the asset was an
    artifact (an intermediate holding vehicle, a cancelled line) and every
    movement of it is dropped from the replay.

    ``cash_amount`` is cash received in the event. It reduces the cost carried
    over — the common retail treatment of a cash-plus-shares merger — rather
    than being booked as a gain.
    """

    __tablename__ = "asset_successions"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "from_asset_id", name="uq_succession_portfolio_from"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    from_asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    to_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=True
    )
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    cash_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: "manual" when the user created it, "detected" when accepted from a suggestion.
    source: Mapped[str] = mapped_column(String(16), default="manual")


class SuccessionAiSuggestion(Base):
    """A corporate event the AI scan found, awaiting the user's decision.

    Stored — not applied: the user accepts or declines each one, now or later.
    Accepting writes the real :class:`AssetSuccession`; declined rows stay so
    a re-scan does not resurface the same proposal.
    """

    __tablename__ = "succession_ai_suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    from_ticker: Mapped[str] = mapped_column(String(40))
    #: Null successor: the asset was delisted/bought out — position written off.
    to_ticker: Mapped[str | None] = mapped_column(String(40), nullable=True)
    effective_date: Mapped[date] = mapped_column(Date)
    cash_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    #: rename | merger | delisting | spinoff | other
    event_type: Mapped[str] = mapped_column(String(24), default="other")
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Where the model says it saw the event (site/publication).
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: pending | accepted | declined
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    provider: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SmartInvestRun(Base):
    """One finished aporte-inteligente analysis, kept so the advice survives.

    The job registry forgets results after an hour; this is the durable copy.
    ``payload`` is stored whole, exactly as the page renders it (money as
    strings): the value of a run is the advice as it was given, with the
    prices of that moment — it is never recomputed later.
    """

    __tablename__ = "smart_invest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(8), default="BRL")
    provider: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TreasuryPrice(Base):
    """Daily two-sided price of a Tesouro Direto title (Tesouro Transparente).

    The Tesouro quotes a *spread*: the investor buys at ``buy_price`` and can
    only sell back at ``sell_price``. Both are kept because they answer
    different questions — what the paper cost and what an early redemption
    would pay — and so are the yields, since a Tesouro position is tracked by
    its contracted rate ("IPCA + 7,12 %") as much as by its price.
    """

    __tablename__ = "treasury_prices"
    __table_args__ = (UniqueConstraint("asset_id", "date", name="uq_treasury_price_asset_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    buy_price: Mapped[Decimal] = mapped_column(MONEY)
    sell_price: Mapped[Decimal] = mapped_column(MONEY)
    buy_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    sell_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="tesouro-transparente")


class FxRate(Base):
    """Daily exchange rate between two currencies.

    Filled from Banco Central's PTAX series (see :mod:`app.market.fx`), which is
    the rate Brazilian tax reporting expects. Rates are stored rather than
    computed on the fly because a position's cost in reais depends on the rate
    that applied on the day it was bought, not today's.
    """

    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint("base", "quote", "date", name="uq_fx_rate_pair_date"),
        Index("ix_fx_rates_pair_date", "base", "quote", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Currency being priced, e.g. "USD".
    base: Mapped[str] = mapped_column(String(8), index=True)
    #: Currency it is priced in, e.g. "BRL".
    quote: Mapped[str] = mapped_column(String(8), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    #: How many units of ``quote`` one unit of ``base`` buys.
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    source: Mapped[str] = mapped_column(String(24), default="bcb-ptax")


class PortfolioSnapshot(Base):
    """Materialised end-of-day portfolio state, used by the history charts."""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (UniqueConstraint("portfolio_id", "date", name="uq_snapshot_portfolio_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    market_value: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    cost_basis: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    net_invested: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    cash_flow: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    dividends_cumulative: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    realized_cumulative: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    unrealized: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    #: Cumulative time-weighted return factor since the first movement (1 = flat).
    #: Stored per day so any window can be rebased by dividing two of them,
    #: which is what lets the rentabilidade chart answer every range from one
    #: pass instead of replaying six years of prices on each request.
    return_factor: Mapped[Decimal] = mapped_column(Numeric(28, 16), default=Decimal(1))
    #: How much of *this day's* market value carried a real price (a close, or
    #: an accrual). A holding nothing can value is marked at cost, which is
    #: outside the percentage — and the page says by how much rather than
    #: quietly diluting the figure towards zero.
    priced_value: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    #: Net capital put in since the first movement, and the same flows weighted
    #: by their date (amount × day ordinal). Two running sums are all a
    #: money-weighted return needs: subtract them at the window's ends and the
    #: capital each contribution actually had at work falls out.
    flow: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    flow_time: Mapped[Decimal] = mapped_column(Numeric(30, 6), default=Decimal(0))
    #: Per asset class: factor, result, value and the same two flow sums. JSON
    #: rather than columns because the set of classes is data, not schema.
    kind_state: Mapped[dict] = mapped_column(JSON, default=dict)
    #: What the ledger looked like when this row was built. A snapshot whose
    #: fingerprint no longer matches is stale — an import, a reclassification or
    #: a declared succession all change the replay — and is recomputed instead
    #: of served.
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IndexRate(Base):
    """Daily (or monthly) values of a market index, keyed by code.

    ``value`` means whatever the code says it means, which is why the code that
    reads it always goes through the module that owns the series:

    * CDI / Selic — the rate **per business day** (``0.052531`` = 0,052531 %
      a.d.), from Banco Central's SGS API. See :mod:`app.market.indices`.
    * IPCA — the monthly variation, same source.
    * IBOV — the index **level** (Ibovespa points) at the daily close, from the
      quote provider. See :mod:`app.market.benchmarks`.
    """

    __tablename__ = "index_rates"
    __table_args__ = (UniqueConstraint("code", "date", name="uq_index_rate_code_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    source: Mapped[str] = mapped_column(String(24), default="bcb-sgs")


class FixedIncomeTerms(Base, TimestampMixin):
    """Yield terms for a fixed income asset.

    B3's export carries no rate, so these are supplied by the user (defaulting to
    100 % of CDI). They drive the accrual that values CDB/LCI/LCA/RDB positions
    instead of leaving them frozen at cost.
    """

    __tablename__ = "fixed_income_terms"

    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    #: CDI | SELIC | IPCA | PRE
    index_code: Mapped[str] = mapped_column(String(16), default="CDI")
    #: Percentage of the index, e.g. 100 for "100 % do CDI", 119.6 for "119,6 %".
    percent_of_index: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal(100))
    #: Annual spread added on top of the index ("CDI + 2 %" -> 2).
    spread_annual: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal(0))
    #: Annual rate for prefixed papers (index_code = "PRE").
    fixed_rate_annual: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal(0))
    maturity_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Interest paid periodically instead of compounding into the principal.
    pays_periodic_interest: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    asset: Mapped[Asset] = relationship()


class AppSetting(Base):
    """Key/value application settings editable from the Settings page."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WatchlistItem(Base, TimestampMixin):
    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)


class Goal(Base, TimestampMixin):
    """Simple goal tracking (target equity by a date)."""

    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    target_amount: Mapped[Decimal] = mapped_column(MONEY)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class AiChat(Base):
    """A saved AI-analyst conversation about one asset or the whole portfolio.

    The whole conversation lives in one JSON column: chats are small and are
    always read and rewritten whole (the client resends the full history each
    turn), so a per-message table would buy nothing.
    """

    __tablename__ = "ai_chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    #: Null means the conversation is about the portfolio as a whole.
    ticker: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    #: First question, truncated — what the list screen shows.
    title: Mapped[str] = mapped_column(String(160))
    #: ``[{"role": "user"|"assistant", "content": str}, ...]``
    messages: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    """Append-only trail of state-changing operations."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Notification(Base):
    """One thing that happened, kept for the header bell's history.

    The counterpart to the *derived* entries in ``app/services/notifications.py``:
    those describe a condition that holds right now and resolve themselves, so
    persisting them would only create rows to reap. These describe a moment —
    last night's backup, this morning's import — which is exactly what does not
    resolve itself and what a scrollable history is made of.

    Distinct from :class:`AuditLog`, which records the same events for a
    different reader: the audit trail is written for forensics and keeps every
    routine refresh, while a row here is a sentence addressed to the user.

    Rows are only ever appended as things happen, so ``id`` descending *is*
    reverse chronological order — the feed pages on the primary key rather than
    a (timestamp, id) pair, and the cursor cannot skip or repeat a row when two
    events land in the same second.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    #: Groups entries by producer (``cloud_backup``, ``backup``, ``import``).
    kind: Mapped[str] = mapped_column(String(40), index=True)
    #: ``info`` | ``success`` | ``warning`` — picks the icon and its colour.
    level: Mapped[str] = mapped_column(String(12), default="info")
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text, default="")
    #: Subjects the entry is about (tickers, file names) — rendered as chips.
    items: Mapped[list] = mapped_column(JSON, default=list)
    #: NULL for events that concern the whole installation (backups); set for
    #: those that belong to one portfolio (imports).
    portfolio_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: Optional idempotency handle. A producer that may run twice for the same
    #: event (a retried task, a scheduler firing after a restart) passes one and
    #: gets a single row — the same contract the importer uses for movements.
    dedup_key: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Archived rows stay for the audit trail but leave the panel for good.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PipelineRun(Base):
    """One execution of an automated collector (see :mod:`app.pipelines`).

    A row is the *whole* channel between the worker thread driving a browser
    and the UI watching it: the thread appends to ``log`` and flips ``status``,
    the API polls the row, and when the broker asks for a 2FA code the thread
    parks on ``waiting_input`` until the user's answer lands in
    ``input_response``. A database row rather than an in-process registry
    because under Docker the scheduled run lives in the worker container and
    the poll in the backend container — memory they do not share.

    Runs are history the user scrolls back through ("did last Monday's
    collection work?"), which is why this is a table and not a settings blob.
    """

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Registry key of the pipeline that ran (``b3``, later ``avenue``…).
    pipeline: Mapped[str] = mapped_column(String(40), index=True)
    #: ``manual`` (button) or ``scheduled`` (weekly slot).
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    #: ``running`` | ``waiting_input`` | ``success`` | ``failed`` | ``cancelled``.
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Touched on every log line and every wait tick. A "running" row whose
    #: heartbeat is minutes old is a crashed process, not a live run — the
    #: claim check treats it as such, so no phantom lock survives a restart.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: ``[{"at": iso, "level": "info"|"warning"|"error", "message": str}]`` —
    #: the narration the UI shows live and keeps afterwards.
    log: Mapped[list] = mapped_column(JSON, default=list)
    #: Set while parked: ``{"prompt": str, "kind": "code", "requested_at": iso}``.
    input_request: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: The user's answer, written by the API for the worker to pick up.
    input_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    #: Per-run knobs the trigger chose, e.g. ``{"full_history": true}`` for a
    #: first-time backfill. Null for a plain incremental run.
    options: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: What the run produced (import counts, downloaded file) — the sentence
    #: the history table shows.
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    portfolio_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=True, index=True
    )


class AiWallet(Base, TimestampMixin):
    """A virtual portfolio managed entirely by an AI model.

    Not related to the user's real portfolios: positions live in their own
    tables, never as ``Transaction`` rows. The provider/model are pinned at
    creation so a wallet stays loyal to one AI — that keeps a "competition
    between models" meaningful even if Configurações changes later.
    """

    __tablename__ = "ai_wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    provider: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(120))


class AiWalletCategory(Base):
    """One activated category of a wallet, with its virtual cash pool.

    A row exists only after the category's first generation: the R$10.000
    budget is credited at that moment, so a wallet's invested capital is the
    sum of the budgets it actually put to work — not seven idle piles.
    """

    __tablename__ = "ai_wallet_categories"
    __table_args__ = (UniqueConstraint("wallet_id", "category", name="uq_ai_wallet_category"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("ai_wallets.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(24))
    budget: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(10000))
    cash: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The model's own strategy for the category, written at generation and
    #: fed back on every suggestion run so it remembers its goal.
    thesis: Mapped[str | None] = mapped_column(Text, nullable=True)


class AiWalletPosition(Base, TimestampMixin):
    """A virtual holding inside an AI wallet.

    Listed assets point at a (possibly watch-only) ``Asset`` row so the quote
    refresh keeps pricing them; SET NULL on asset deletion degrades the row to
    "valued at cost" instead of corrupting the wallet's cash accounting.
    Renda fixa rows have no asset: the paper is synthetic (indexer + rate) and
    is valued by accruing ``cost_brl`` from ``fi_start_date``.
    """

    __tablename__ = "ai_wallet_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("ai_wallets.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(24), index=True)
    asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ticker: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(255), default="")
    currency: Mapped[str] = mapped_column(String(8), default="BRL")
    quantity: Mapped[Decimal] = mapped_column(QTY, default=Decimal(0))
    #: Weighted average entry price, in ``currency``.
    avg_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    #: Weighted average entry FX (USD→BRL) for foreign rows.
    avg_fx: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    #: Invested capital in BRL — the source of truth for P&L and cash math.
    cost_brl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    #: Money reserved for this ticker while it has no quote (deferred buy).
    #: Settled into ``quantity`` automatically once a price arrives.
    pending_brl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    is_fixed_income: Mapped[bool] = mapped_column(Boolean, default=False)
    fi_index_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fi_percent_of_index: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    fi_spread_annual: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    fi_fixed_rate_annual: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    fi_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: The AI's justification when the position was opened.
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)

    asset: Mapped[Asset | None] = relationship()


class AiWalletSuggestion(Base):
    """One AI-proposed change, accepted or declined individually by the user."""

    __tablename__ = "ai_wallet_suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("ai_wallets.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(24), index=True)
    #: One uuid per "Sugerir mudanças" run; a new batch supersedes older pending rows.
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    #: buy_new | increase | reduce | sell_all | rebalance
    action: Mapped[str] = mapped_column(String(16))
    ticker: Mapped[str | None] = mapped_column(String(40), nullable=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    amount_brl: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    #: Rebalance target: an asset (to_ticker) or another category's cash pool.
    to_ticker: Mapped[str | None] = mapped_column(String(40), nullable=True)
    to_category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_position_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_wallet_positions.id", ondelete="SET NULL"), nullable=True
    )
    #: Raw AI item as returned (Decimals serialised as strings).
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: pending | accepted | declined | superseded | failed
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    #: Why it failed, or what was actually applied (e.g. buy capped by cash).
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_wallet_positions.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AiWalletEvent(Base):
    """Append-only log of everything that happened to an AI wallet.

    Mirrors ``AuditLog`` but scoped to a wallet and carrying which AI acted.
    Wallet deletion cascades these away, so the deletion itself is recorded in
    the global ``audit_logs`` instead.
    """

    __tablename__ = "ai_wallet_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("ai_wallets.id", ondelete="CASCADE"), index=True)
    category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    #: wallet.created | category.generated | position.buy/increase/reduce/sell/rebalance
    #: | suggestion.batch/accepted/declined
    action: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str | None] = mapped_column(String(24), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class AiWalletSnapshot(Base):
    """Daily value of an AI wallet, for the profitability/competition chart.

    ``return_factor`` chains time-weighted daily returns exactly like
    ``PortfolioSnapshot``; activating a category adds equal value and flow, so
    the factor — and the competition — stays fair.
    """

    __tablename__ = "ai_wallet_snapshots"
    __table_args__ = (UniqueConstraint("wallet_id", "date", name="uq_ai_wallet_snapshot_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("ai_wallets.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    #: Positions market value (BRL) + cash.
    value: Mapped[Decimal] = mapped_column(MONEY)
    #: Sum of activated category budgets.
    invested: Mapped[Decimal] = mapped_column(MONEY)
    cash: Mapped[Decimal] = mapped_column(MONEY)
    return_factor: Mapped[Decimal] = mapped_column(Numeric(28, 16), default=Decimal(1))
    #: ``{"FII": {"value": "10234.10", "cash": "210.50"}, ...}``
    categories: Mapped[dict] = mapped_column(JSON, default=dict)
