"""The staged ingest that fills ``asset_universe``.

Five stages, run in order, each resumable at its own boundary: prices (which is
also the roster), registry, company fundamentals, fund informes, and the US
ticker list. The stage a run reached is recorded in the database, so a laptop
that closed mid-parse picks up at the next stage rather than starting over.

Two properties are worth stating plainly because the rest of the module is
shaped by them:

* **No per-ticker HTTP.** A whole run is roughly ten requests. That is what
  keeps Yahoo reserved for papers the portfolio actually holds, and what makes
  the whole thing finish in minutes instead of hours. There is a test that
  counts outbound calls, because this is the sort of invariant that erodes.
* **The ingest never creates an ``Asset`` row.** ``market.service.quotable_assets``
  treats an asset with no transactions as watch-only and refreshes its quote
  every half hour; two thousand of those would be a self-inflicted rate limit.

Sessions are opened per stage and committed in batches — never held across a
download, which on SQLite would park a writer lock for the length of an HTTP
request while the UI polls for progress.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.dates import local_today
from app.core.logging import get_logger
from app.db.models import AssetUniverse
from app.db.session import session_scope
from app.db.upsert import dialect_insert
from app.domain.enums import AssetKind
from app.market.universe import compute, state
from app.market.universe.sources import (
    SourceShapeError,
    cotahist,
    cvm_fii,
    cvm_statements,
    registry,
    sec,
    sec_financials,
)

logger = get_logger(__name__)

#: Rows per commit. Small enough that a cancel lands promptly and a SQLite
#: writer lock is never held long; large enough that 2 500 rows is 25 commits.
BATCH = 100

#: One worker: two concurrent ingests would duplicate every download for no
#: gain. The persisted run block is the real mutex — this only bounds threads.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="universe")
_LOCK = threading.Lock()

SOURCE_PRICES = cotahist.SOURCE


def _recover(db: Session) -> None:
    """Roll back after a failed stage so the remaining ones can still run.

    Postgres aborts the whole transaction on any statement error and refuses
    everything after it until a rollback — so without this, one stage tripping
    over a source's data killed every later stage *and* the run's own progress
    writes. SQLite is more forgiving, which is exactly why this was invisible
    until the first real Postgres run.
    """
    try:
        db.rollback()
    except Exception:  # noqa: BLE001 — nothing useful to do if rollback fails
        logger.exception("universe: rollback after a failed stage failed")


def _short(exc: Exception, limit: int = 300) -> str:
    """A message fit for the UI. A driver's full SQL echo is not one."""
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _upsert(db: Session, rows: list[dict], keys: list[str]) -> None:
    """Upsert a batch, updating only ``keys``.

    Listing the updatable columns explicitly is load-bearing: passing the whole
    row would let the price stage blank the fundamentals the later stages
    wrote, and each stage owns a disjoint set of columns for exactly that
    reason.
    """
    if not rows:
        return
    statement = dialect_insert(db)(AssetUniverse).values(rows)
    db.execute(
        statement.on_conflict_do_update(
            index_elements=[AssetUniverse.ticker],
            set_={key: getattr(statement.excluded, key) for key in keys},
        )
    )


# ---------------------------------------------------------------------------
# Stage 1 — prices and the roster (COTAHIST)


def _cotahist_urls(years: int, today: date) -> list[str]:
    """Which COTAHIST files to fold, oldest first.

    Annual files for the completed years, then monthly ones for the current
    year. The mix matters: the current year's annual file is republished daily
    and grows to 67 MB, while its months are ~10 MB each and only the last one
    changes — so a nightly refresh re-reads a fraction of the data.
    """
    urls = [cotahist.annual_url(year) for year in range(today.year - years + 1, today.year)]
    urls += [cotahist.monthly_url(today.year, month) for month in range(1, today.month + 1)]
    return urls


PRICE_COLUMNS = [
    "name",
    "kind",
    "isin",
    "market_symbol",
    "price",
    "price_date",
    "avg_volume_21d",
    "price_change_12m_pct",
    "high_52w",
    "low_52w",
    "volatility_12m_pct",
    "traded_days_12m",
    "price_source",
    "price_fetched_at",
    "identity_source",
    "identity_fetched_at",
]


def _kind_for(reduction: cotahist.Reduction) -> str:
    """B3's own instrument family, falling back to the ticker classifier.

    CODBDI is authoritative where the suffix heuristic guesses — it separates
    HGLG11 (FII) from BOVA11 (ETF), which no ticker shape can.
    """
    if reduction.kind:
        return reduction.kind
    from app.importer.parser import classify_asset_kind  # local: avoids a cycle

    hint = f"{reduction.name} {reduction.especi}".strip()
    return classify_asset_kind(reduction.ticker, hint).value


def stage_prices(db: Session, block: dict, years: int) -> int:
    """Download and reduce COTAHIST; write the roster and its price metrics."""
    today = local_today()
    urls = _cotahist_urls(years, today)
    cancelled = False

    def on_file(index: int, total: int, url: str) -> bool:
        nonlocal cancelled
        name = url.rsplit("/", 1)[-1]
        stop = state.heartbeat(
            db,
            block,
            message=f"Baixando {name} ({index + 1} de {total})",
            processed=index,
            total=total,
        )
        cancelled = bool(stop)
        return not stop

    reductions = cotahist.fetch_and_reduce(urls, on_file=on_file)
    if cancelled and not reductions:
        return 0

    now = datetime.now(UTC)
    rows: list[dict] = []
    written = 0
    total = len(reductions)
    for index, reduction in enumerate(reductions.values()):
        rows.append(
            {
                "ticker": reduction.ticker,
                "market": "B3",
                "currency": "BRL",
                "name": (reduction.name or reduction.ticker)[:255],
                "kind": _kind_for(reduction),
                "isin": reduction.isin,
                "market_symbol": f"{reduction.ticker}.SA",
                "price": compute.quantize_money(reduction.last_close),
                "price_date": reduction.last_date,
                "avg_volume_21d": compute.quantize_big(reduction.avg_volume_21d),
                "price_change_12m_pct": compute.quantize_ratio(reduction.change_12m_pct),
                "high_52w": compute.quantize_money(reduction.high),
                "low_52w": compute.quantize_money(reduction.low),
                "volatility_12m_pct": compute.quantize_ratio(reduction.volatility_pct),
                "traded_days_12m": reduction.traded_days,
                "price_source": SOURCE_PRICES,
                "price_fetched_at": now,
                "identity_source": SOURCE_PRICES,
                "identity_fetched_at": now,
            }
        )
        if len(rows) >= BATCH:
            _upsert(db, rows, PRICE_COLUMNS)
            written += len(rows)
            rows = []
            db.commit()
            if state.heartbeat(
                db, block, message=f"Gravando ativos ({index + 1} de {total})",
                processed=index + 1, total=total,
            ):
                return written
    _upsert(db, rows, PRICE_COLUMNS)
    written += len(rows)
    db.commit()
    return written


# ---------------------------------------------------------------------------
# Stage 2 — registry (B3 companies + CVM cadastro)

REGISTRY_COLUMNS = ["name", "cnpj", "cvm_code", "sector", "b3_segment", "status", "indexes"]
#: Papers with no issuer to attach — only their index memberships are written.
INDEX_ONLY_COLUMNS = ["indexes"]


def stage_registry(db: Session, block: dict) -> int:
    """Attach each ticker to the company behind it, and that company's sector."""
    companies = registry.fetch_b3_companies()
    if not companies:
        state.warn(db, block, "A B3 não respondeu à lista de companhias; setores podem faltar.")
    by_root = {company.root: company for company in companies}

    state.heartbeat(db, block, message="Lendo o cadastro da CVM")
    try:
        cvm = registry.fetch_cvm_registry()
    except SourceShapeError as exc:
        state.warn(db, block, f"Cadastro da CVM indisponível: {exc}")
        cvm = {}

    state.heartbeat(db, block, message="Lendo a composição dos índices da B3")
    memberships = registry.fetch_index_membership()
    if not memberships:
        state.warn(db, block, "Índices da B3 indisponíveis; o filtro por índice ficará vazio.")

    tickers = db.scalars(
        select(AssetUniverse.ticker).where(AssetUniverse.market == "B3")
    ).all()
    rows: list[dict] = []
    #: Papers B3 indexes whose issuer did not resolve — every ETF and BDR, and
    #: every FII. They get their memberships through a narrower upsert: an
    #: insert missing the company columns would carry their defaults into
    #: ``excluded`` and blank out a name the price stage set correctly.
    index_only: list[dict] = []
    written = 0
    total = len(tickers)

    def flush(force: bool = False) -> bool:
        """Write both batches; True when the run has been asked to stop."""
        nonlocal rows, index_only, written
        if force or len(rows) >= BATCH:
            _upsert(db, rows, REGISTRY_COLUMNS)
            written += len(rows)
            rows = []
        if force or len(index_only) >= BATCH:
            _upsert(db, index_only, INDEX_ONLY_COLUMNS)
            written += len(index_only)
            index_only = []
        db.commit()
        return False

    for index, ticker in enumerate(tickers):
        company = by_root.get(registry.ticker_root(ticker))
        membership = memberships.get(ticker)
        if company is None:
            if membership:
                index_only.append({"ticker": ticker, "indexes": membership})
        else:
            cvm_row = cvm.get(company.cnpj) if company.cnpj else None
            rows.append(
                {
                    "ticker": ticker,
                    "indexes": membership,
                    "name": (cvm_row.name if cvm_row and cvm_row.name else company.name)[:255],
                    "cnpj": company.cnpj,
                    "cvm_code": ((cvm_row.cvm_code if cvm_row else company.cvm_code) or "")[:12] or None,
                    "sector": ((cvm_row.sector if cvm_row else None) or "")[:120] or None,
                    "b3_segment": (company.segment or "")[:60] or None,
                    # The registry's word, not ours: a cancelled registration is
                    # a fact about the company, and the screener filters on it.
                    # Truncated defensively — CVM writes free-ish phrases here,
                    # and a longer one must cost a clipped label, not the stage.
                    "status": (cvm_row.status if cvm_row else company.status)[:40],
                }
            )
        if len(rows) >= BATCH or len(index_only) >= BATCH:
            flush()
            if state.heartbeat(db, block, processed=index + 1, total=total):
                return written
    flush(force=True)
    return written


# ---------------------------------------------------------------------------
# Stage 3 — company fundamentals (CVM DFP)

FUNDAMENTAL_COLUMNS = [
    "revenue",
    "net_income",
    "market_cap",
    "shares_outstanding",
    "book_value_per_share",
    "pe",
    "pb",
    "roe_pct",
    "net_margin_pct",
    "gross_margin_pct",
    "revenue_growth_pct",
    "earnings_growth_pct",
    "debt_to_equity",
    "dividend_yield_pct",
    "payout_pct",
    "fundamentals_source",
    "fundamentals_fetched_at",
    "fundamentals_period",
    "notes",
]

#: Families whose numbers come from company statements. A BDR is a receipt over
#: a foreign share, so a Brazilian filing would describe the wrong entity.
_COMPANY_KINDS = {AssetKind.STOCK.value, AssetKind.UNIT.value}


def stage_fundamentals(db: Session, block: dict) -> int:
    """Compute per-ticker ratios from the filings and the stored price."""
    records, warnings = cvm_statements.fetch()
    for warning in warnings:
        state.warn(db, block, warning)

    rows_in = db.execute(
        select(
            AssetUniverse.ticker,
            AssetUniverse.cnpj,
            AssetUniverse.price,
        ).where(
            AssetUniverse.market == "B3",
            AssetUniverse.cnpj.is_not(None),
            AssetUniverse.kind.in_(_COMPANY_KINDS),
        )
    ).all()

    now = datetime.now(UTC)
    rows: list[dict] = []
    written = 0
    total = len(rows_in)
    for index, (ticker, cnpj, price) in enumerate(rows_in):
        record = records.get(cnpj or "")
        if record is None:
            continue
        price = Decimal(price) if price is not None else None
        shares, scale = compute.resolve_share_scale(
            record.shares_outstanding, record.equity, price
        )
        note = None
        if record.shares_outstanding and shares is None:
            note = (
                "quantidade de ações não pôde ser escalada com segurança "
                "(a CVM não publica a escala do campo)"
            )
        cap = compute.market_cap(price, shares)
        rows.append(
            {
                "ticker": ticker,
                "revenue": compute.quantize_big(record.revenue),
                "net_income": compute.quantize_big(record.net_income),
                "market_cap": compute.quantize_big(cap),
                "shares_outstanding": compute.quantize_big(shares),
                "book_value_per_share": compute.quantize_money(
                    compute.book_value_per_share(record.equity, shares)
                ),
                "pe": compute.quantize_ratio(
                    compute.price_earnings(price, record.net_income, shares)
                ),
                "pb": compute.quantize_ratio(
                    compute.price_book(price, record.equity, shares)
                ),
                "roe_pct": compute.quantize_ratio(
                    compute.return_on_equity_pct(record.net_income, record.equity)
                ),
                "net_margin_pct": compute.quantize_ratio(
                    compute.margin_pct(record.net_income, record.revenue)
                ),
                "gross_margin_pct": compute.quantize_ratio(
                    compute.margin_pct(record.gross_profit, record.revenue)
                ),
                # On the trailing basis the source already compared matching
                # spans; only the annual basis derives growth from totals here.
                "revenue_growth_pct": compute.quantize_ratio(
                    record.revenue_growth_pct
                    if record.revenue_growth_pct is not None
                    else compute.growth_pct(record.revenue, record.prior_revenue)
                ),
                "earnings_growth_pct": compute.quantize_ratio(
                    record.earnings_growth_pct
                    if record.earnings_growth_pct is not None
                    else compute.growth_pct(record.net_income, record.prior_net_income)
                ),
                "debt_to_equity": compute.quantize_ratio(
                    compute.debt_to_equity(record.debt, record.equity)
                ),
                "dividend_yield_pct": compute.quantize_ratio(
                    compute.dividend_yield_pct(record.dividends_paid, cap)
                ),
                "payout_pct": compute.quantize_ratio(
                    compute.payout_pct(record.dividends_paid, record.net_income)
                ),
                "fundamentals_source": cvm_statements.SOURCE,
                "fundamentals_fetched_at": now,
                "fundamentals_period": record.period,
                "notes": note,
            }
        )
        if len(rows) >= BATCH:
            _upsert(db, rows, FUNDAMENTAL_COLUMNS)
            written += len(rows)
            rows = []
            db.commit()
            if state.heartbeat(db, block, processed=index + 1, total=total):
                return written
    _upsert(db, rows, FUNDAMENTAL_COLUMNS)
    written += len(rows)
    db.commit()
    return written


# ---------------------------------------------------------------------------
# Stage 4 — FII informes

FUND_COLUMNS = [
    "name",
    "fund_segment",
    "fii_management",
    "fii_pl",
    "cnpj",
    "shares_outstanding",
    "book_value_per_share",
    "pb",
    "market_cap",
    "dividend_yield_pct",
    "fundamentals_source",
    "fundamentals_fetched_at",
    "fundamentals_period",
]


def stage_funds(db: Session, block: dict) -> int:
    """Fill the FII rows from the monthly informe, joined on ISIN."""
    funds, warnings = cvm_fii.fetch()
    for warning in warnings:
        state.warn(db, block, warning)
    by_isin = cvm_fii.by_isin(funds)

    rows_in = db.execute(
        select(AssetUniverse.ticker, AssetUniverse.isin, AssetUniverse.price).where(
            AssetUniverse.kind == AssetKind.FII.value, AssetUniverse.isin.is_not(None)
        )
    ).all()

    now = datetime.now(UTC)
    rows: list[dict] = []
    written = 0
    total = len(rows_in)
    for index, (ticker, isin, price) in enumerate(rows_in):
        record = by_isin.get((isin or "").upper())
        if record is None:
            continue
        price = Decimal(price) if price is not None else None
        book = record.book_value_per_quota
        rows.append(
            {
                "ticker": ticker,
                "name": (record.name or ticker)[:255],
                "fund_segment": (record.segment or None) and record.segment[:60],
                "fii_management": (record.management or None) and record.management[:24],
                "fii_pl": compute.quantize_big(record.net_assets),
                "cnpj": record.cnpj,
                "shares_outstanding": compute.quantize_big(record.quotas),
                "book_value_per_share": compute.quantize_money(book),
                # The fund publishes its own book value per quota, so this is
                # the filed figure divided into the price — not a reconstruction.
                "pb": compute.quantize_ratio(
                    None if not book or book <= 0 or price is None else price / book
                ),
                "market_cap": compute.quantize_big(compute.market_cap(price, record.quotas)),
                "dividend_yield_pct": compute.quantize_ratio(record.dividend_yield_pct),
                "fundamentals_source": cvm_fii.SOURCE,
                "fundamentals_fetched_at": now,
                "fundamentals_period": record.period,
            }
        )
        if len(rows) >= BATCH:
            _upsert(db, rows, FUND_COLUMNS)
            written += len(rows)
            rows = []
            db.commit()
            if state.heartbeat(db, block, processed=index + 1, total=total):
                return written
    _upsert(db, rows, FUND_COLUMNS)
    written += len(rows)
    db.commit()
    return written


# ---------------------------------------------------------------------------
# Stage 5 — US identity

US_COLUMNS = ["name", "cik", "identity_source", "identity_fetched_at"]

#: What the SEC's bulk XBRL fills in for a US row. No price columns: no free
#: bulk source publishes US closes, so market cap, P/L and P/VP stay absent
#: and the screener ranks these by revenue instead.
US_FUNDAMENTAL_COLUMNS = [
    "name",
    "kind",
    "sector",
    "revenue",
    "net_income",
    "shares_outstanding",
    "roe_pct",
    "net_margin_pct",
    "gross_margin_pct",
    "revenue_growth_pct",
    "earnings_growth_pct",
    "debt_to_equity",
    "fundamentals_source",
    "fundamentals_fetched_at",
    "fundamentals_period",
    "notes",
]


def stage_us(db: Session, block: dict) -> int:
    """The SEC ticker registry. Identity only — see the module docstring."""
    try:
        sec.check_user_agent(db)
    except sec.UserAgentNotConfigured as exc:
        state.warn(db, block, str(exc))
        return 0

    tickers = sec.fetch(db)
    # A US ticker equal to a B3 one would collide on the unique key. B3 tickers
    # carry a digit so this is near-impossible, but "near" is not "never" and a
    # silent overwrite would attribute one market's prices to the other.
    taken = set(
        db.scalars(select(AssetUniverse.ticker).where(AssetUniverse.market == "B3")).all()
    )
    now = datetime.now(UTC)
    rows: list[dict] = []
    written = skipped = 0
    total = len(tickers)
    for index, item in enumerate(tickers):
        if item.ticker in taken:
            skipped += 1
            continue
        rows.append(
            {
                "ticker": item.ticker,
                "market": "US",
                "currency": sec.DEFAULT_CURRENCY,
                "name": item.name,
                "kind": sec.DEFAULT_KIND,
                "market_symbol": item.ticker,
                "cik": item.cik,
                "identity_source": sec.SOURCE,
                "identity_fetched_at": now,
            }
        )
        if len(rows) >= BATCH:
            _upsert(db, rows, US_COLUMNS)
            written += len(rows)
            rows = []
            db.commit()
            if state.heartbeat(db, block, processed=index + 1, total=total):
                return written
    _upsert(db, rows, US_COLUMNS)
    written += len(rows)
    db.commit()
    if skipped:
        state.warn(db, block, f"{skipped} tickers dos EUA ignorados por colidirem com códigos da B3.")

    written += _stage_us_fundamentals(db, block, tickers)
    return written


def _stage_us_fundamentals(db: Session, block: dict, tickers: list) -> int:
    """Fill the US rows from the SEC's bulk XBRL datasets.

    Everything derivable without a price, which is everything except valuation:
    ROE, margins, growth and leverage are ratios between figures the filing
    itself contains. Market cap, P/L and P/VP need a share price, and no free
    bulk source publishes US closes — so they stay absent rather than being
    filled from somewhere this feature has ruled out.
    """
    state.heartbeat(db, block, message="Lendo os balanços das empresas dos EUA (SEC)")
    try:
        filings, warnings = sec_financials.fetch(db=db)
    except sec.UserAgentNotConfigured as exc:
        state.warn(db, block, str(exc))
        return 0
    for warning in warnings:
        state.warn(db, block, warning)

    by_cik = {item.cik.lstrip("0"): item.ticker for item in tickers}
    now = datetime.now(UTC)
    rows: list[dict] = []
    written = 0
    total = len(filings)
    for index, (cik, record) in enumerate(filings.items()):
        ticker = by_cik.get(cik.lstrip("0"))
        if ticker is None:
            continue  # a filer with no listed ticker is not screenable
        equity = record.equity
        rows.append(
            {
                "ticker": ticker,
                "name": (record.name or ticker)[:255],
                "kind": sec_financials.kind_for(record.sic),
                "sector": (sec_financials.sector_for(record.sic) or "")[:120] or None,
                "revenue": compute.quantize_big(record.revenue),
                "net_income": compute.quantize_big(record.net_income),
                "shares_outstanding": compute.quantize_big(record.shares_outstanding),
                "roe_pct": compute.quantize_ratio(
                    compute.return_on_equity_pct(record.net_income, equity)
                ),
                "net_margin_pct": compute.quantize_ratio(
                    compute.margin_pct(record.net_income, record.revenue)
                ),
                "gross_margin_pct": compute.quantize_ratio(
                    compute.margin_pct(record.gross_profit, record.revenue)
                ),
                "revenue_growth_pct": compute.quantize_ratio(
                    compute.growth_pct(record.revenue, record.prior_revenue)
                ),
                "earnings_growth_pct": compute.quantize_ratio(
                    compute.growth_pct(record.net_income, record.prior_net_income)
                ),
                # Total liabilities over equity: US GAAP files no single
                # borrowings line, so this is a coarser gearing measure than the
                # B3 rows carry. Same column, deliberately — a screener that
                # split it in two would ask the reader to know which is which.
                "debt_to_equity": compute.quantize_ratio(
                    compute.debt_to_equity(record.debt, equity)
                ),
                "fundamentals_source": sec_financials.SOURCE,
                "fundamentals_fetched_at": now,
                "fundamentals_period": (record.period or "")[:12] or None,
                "notes": "sem preço em fonte pública em massa — valuation indisponível",
            }
        )
        if len(rows) >= BATCH:
            _upsert(db, rows, US_FUNDAMENTAL_COLUMNS)
            written += len(rows)
            rows = []
            db.commit()
            if state.heartbeat(db, block, processed=index + 1, total=total):
                return written
    _upsert(db, rows, US_FUNDAMENTAL_COLUMNS)
    written += len(rows)
    db.commit()
    return written


# ---------------------------------------------------------------------------
# The driver

_STAGE_FUNCTIONS = {
    "prices": lambda db, block, cfg: stage_prices(db, block, cfg["history_years"]),
    "registry": lambda db, block, cfg: stage_registry(db, block),
    "fundamentals": lambda db, block, cfg: stage_fundamentals(db, block),
    "funds": lambda db, block, cfg: stage_funds(db, block),
    "us": lambda db, block, cfg: stage_us(db, block),
}

#: Stages that only make sense for a market the user asked for.
_STAGE_MARKETS = {
    "prices": "B3",
    "registry": "B3",
    "fundamentals": "B3",
    "funds": "B3",
    "us": "US",
}


def ingest_slice(db: Session, block: dict, *, budget_seconds: float = 900.0) -> bool:
    """Run stages until the budget runs out. True when everything is done.

    The budget is half of Celery's 30-minute hard limit, so a scheduled run
    always returns before the task is killed. A run that stops early leaves its
    completed stages recorded and resumes at the next one.
    """
    config = {
        "history_years": state.history_years(db),
        "markets": [market.upper() for market in (block.get("markets") or ["B3"])],
    }
    deadline = datetime.now(UTC) + timedelta(seconds=budget_seconds)
    done = set(block.get("stages_done") or [])

    for name, _label in state.STAGES:
        if name in done:
            continue
        if _STAGE_MARKETS[name] not in config["markets"]:
            state.stage_started(db, block, name)
            state.stage_done(db, block, name, 0, state="skipped")
            continue
        if datetime.now(UTC) >= deadline:
            return False
        state.stage_started(db, block, name)
        try:
            rows = _STAGE_FUNCTIONS[name](db, block, config)
        except SourceShapeError as exc:
            # The published format changed. Skip the stage with the reason
            # recorded; the rows written by earlier runs stay exactly as they
            # were rather than being overwritten with nothing.
            _recover(db)
            state.warn(db, block, f"Etapa '{name}' ignorada: {_short(exc)}")
            state.stage_done(db, block, name, 0, state="skipped")
            continue
        except Exception as exc:  # noqa: BLE001 — one bad source is not a dead run
            logger.exception("universe stage %s failed", name)
            _recover(db)
            state.warn(db, block, f"Etapa '{name}' falhou: {_short(exc)}")
            state.stage_done(db, block, name, 0, state="failed")
            continue
        state.stage_done(db, block, name, rows)
        if state.cancel_requested(db, block):
            return False

    return True


def run_ingest(markets: list[str] | None = None, *, budget_seconds: float = 900.0) -> dict:
    """One complete ingest, opening its own session. Safe to call from a thread.

    Used by both the scheduled task and the button in Configurações. The run
    block is claimed before any work starts, so a second caller gets a refusal
    rather than a duplicate set of downloads.
    """
    with session_scope() as db:
        block = state.start(db, markets or state.markets(db))
    try:
        with session_scope() as db:
            finished = ingest_slice(db, block, budget_seconds=budget_seconds)
            cancelled = state.cancel_requested(db, block)
            if cancelled:
                state.finish(db, block, "cancelled", "Atualização cancelada.")
            elif finished:
                total = db.scalar(select(func.count()).select_from(AssetUniverse)) or 0
                state.finish(db, block, "done", f"{total} ativos no universo.")
            else:
                state.finish(db, block, "paused", "Pausada — retome para continuar.")
            return state.read(db)
    except Exception as exc:  # noqa: BLE001 — the run block must always close
        logger.exception("universe ingest failed")
        with session_scope() as db:
            state.finish(db, block, "error", f"Falha na atualização: {exc}")
            return state.read(db)


def start_background(db: Session, markets: list[str] | None = None) -> dict:
    """Claim the run and hand it to the worker thread; returns immediately.

    The claim happens here, on the request's session, so a second click gets a
    409 straight away instead of racing the thread pool.
    """
    wanted = markets or state.markets(db)
    block = state.start(db, wanted)
    with _LOCK:
        _EXECUTOR.submit(_run_claimed, block, wanted)
    return block


def _run_claimed(block: dict, markets: list[str]) -> None:
    """Continue a run whose block has already been claimed by the caller."""
    try:
        with session_scope() as db:
            finished = ingest_slice(db, block)
            if state.cancel_requested(db, block):
                state.finish(db, block, "cancelled", "Atualização cancelada.")
            elif finished:
                total = db.scalar(select(func.count()).select_from(AssetUniverse)) or 0
                state.finish(db, block, "done", f"{total} ativos no universo.")
            else:
                state.finish(db, block, "paused", "Pausada — retome para continuar.")
    except Exception as exc:  # noqa: BLE001 — never leave the block "running"
        logger.exception("universe ingest failed")
        with session_scope() as db:
            state.finish(db, block, "error", f"Falha na atualização: {exc}")
