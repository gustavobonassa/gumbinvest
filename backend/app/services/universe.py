"""Screening over the local universe, and how a portfolio compares to it.

Pure database work — no HTTP, no model calls. The ingest in
``app.market.universe`` fills the table; everything here only reads it.

Three consumers: the "Universo de ativos" page, the "como me comparo" view, and
the AI wallet's candidate step, which gets a short pre-screened list instead of
being asked to remember which papers exist.

Two decisions worth stating, because both are easy to get wrong invisibly:

**Ordering must be explicit about NULLs.** SQLite sorts NULLs first, Postgres
sorts them last. Ranking by dividend yield without saying which you want gives
the desktop build and the Docker build *different* top-30 lists from the same
data. Every ordering here goes through ``nullslast`` and tie-breaks on ticker,
so "deterministic screener" is true rather than aspirational.

**No composite quality score.** It would be one unfalsifiable number deciding
which companies a user sees, which is precisely the kind of hidden judgement
this codebase surfaces instead. Rank by liquidity — the least opinionated proxy
for "actually investable" — hand over the raw metrics, and let the reader (or
the model) argue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import Select, func, nullslast, or_, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Asset, AssetUniverse
from app.market.universe import state

# One-way import: this module knows the AI wallet's categories so a screen can
# honour them, and ``ai_wallet`` must never import this module back. The
# injection happens in the route, which already imports both.
from app.services.ai_wallet import CATEGORIES

logger = get_logger(__name__)

#: Screenable fields, named exactly as ``ai_wallet._FUNDAMENTAL_KEYS`` renames
#: them for the model. Sharing the vocabulary means a screener row and a
#: phase-B verified block use one name per number, so nothing has to reconcile
#: "pe" with "p_l" halfway through a prompt.
SCREEN_FIELDS: dict[str, object] = {
    "setor": AssetUniverse.sector,
    "valor_de_mercado": AssetUniverse.market_cap,
    "p_l": AssetUniverse.pe,
    "p_vp": AssetUniverse.pb,
    "roe_pct": AssetUniverse.roe_pct,
    "margem_liquida_pct": AssetUniverse.net_margin_pct,
    "margem_bruta_pct": AssetUniverse.gross_margin_pct,
    "crescimento_receita_pct": AssetUniverse.revenue_growth_pct,
    "crescimento_lucro_pct": AssetUniverse.earnings_growth_pct,
    "divida_sobre_patrimonio": AssetUniverse.debt_to_equity,
    "dividend_yield_pct": AssetUniverse.dividend_yield_pct,
    "payout_pct": AssetUniverse.payout_pct,
    "liquidez_media_diaria": AssetUniverse.avg_volume_21d,
    "variacao_12m_pct": AssetUniverse.price_change_12m_pct,
    "volatilidade_12m_pct": AssetUniverse.volatility_12m_pct,
    "preco": AssetUniverse.price,
    "valor_patrimonial_por_acao": AssetUniverse.book_value_per_share,
    "segmento_do_fundo": AssetUniverse.fund_segment,
    "segmento_b3": AssetUniverse.b3_segment,
    "patrimonio_do_fundo": AssetUniverse.fii_pl,
    "indices": AssetUniverse.indexes,
    "receita": AssetUniverse.revenue,
    "lucro_liquido": AssetUniverse.net_income,
}

#: Which fields a screen may sort on. Text columns are filterable but a
#: "top 30 by sector name" is not a ranking anyone wants.
SORTABLE = frozenset(SCREEN_FIELDS) - {"setor", "segmento_do_fundo", "segmento_b3", "indices"}

#: Instrument families that are tradable positions. Subscription rights expire
#: and are not something to screen for.
_SCREENABLE_KINDS = frozenset({"STOCK", "UNIT", "FII", "ETF", "ETF_INTL", "BDR", "REIT", "STOCK_INTL"})


class Filter:
    """One comparison against a screenable field."""

    __slots__ = ("field", "op", "value")

    def __init__(self, field: str, op: str, value: object) -> None:
        self.field = field
        self.op = op
        self.value = value


@dataclass(frozen=True, slots=True)
class ScreenRequest:
    category: str | None = None
    kinds: frozenset[str] | None = None
    currency: str | None = None
    market: str | None = None
    sector: str | None = None
    #: A B3 index code: only papers belonging to it. "IBOV", "SMLL", "IFIX"…
    index: str | None = None
    text: str | None = None
    filters: tuple[Filter, ...] = ()
    #: Fields that must be present; a row missing one is dropped and counted.
    require: tuple[str, ...] = ()
    order_by: str = "valor_de_mercado"
    descending: bool = True
    only_active: bool = True
    #: Ignore fundamentals older than this, when set.
    fresher_than_days: int | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class ScreenResult:
    rows: list[dict]
    total: int
    #: Matched the identity filters but lacked a metric the screen needed.
    #: Surfaced rather than hidden: a screener that quietly omits half the
    #: market is worse than one that says how much it omitted.
    dropped_for_missing_data: int
    stalest_fundamentals_at: datetime | None


def screenable_fields() -> list[dict]:
    """The whitelist, for the UI's filter and sort controls."""
    return [
        {"key": key, "sortable": key in SORTABLE}
        for key in SCREEN_FIELDS
    ]


def _column(name: str):
    column = SCREEN_FIELDS.get(name)
    if column is None:
        raise ValueError(f"campo desconhecido para o screener: {name}")
    return column


def _apply_filters(stmt: Select, request: ScreenRequest) -> Select:
    if request.market:
        stmt = stmt.where(AssetUniverse.market == request.market.upper())
    if request.currency:
        stmt = stmt.where(AssetUniverse.currency == request.currency.upper())
    if request.kinds:
        stmt = stmt.where(AssetUniverse.kind.in_(sorted(request.kinds)))
    else:
        stmt = stmt.where(AssetUniverse.kind.in_(sorted(_SCREENABLE_KINDS)))
    if request.sector:
        stmt = stmt.where(AssetUniverse.sector == request.sector)
    if request.index:
        # The stored value is comma-bounded (",IBOV,IBRA,"), so this is an
        # exact token match rather than a substring that could catch IBOVX.
        stmt = stmt.where(AssetUniverse.indexes.like(f"%,{request.index.strip().upper()},%"))
    if request.only_active:
        # The registry's word. A cancelled registration is a fact about the
        # company, not an inference from missing data.
        stmt = stmt.where(AssetUniverse.status.notin_(("CANCELADA", "INATIVO")))
    if request.text:
        pattern = f"%{request.text.strip().upper()}%"
        stmt = stmt.where(
            or_(
                func.upper(AssetUniverse.ticker).like(pattern),
                func.upper(AssetUniverse.name).like(pattern),
            )
        )
    if request.fresher_than_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=request.fresher_than_days)
        stmt = stmt.where(AssetUniverse.fundamentals_fetched_at >= cutoff)

    for item in request.filters:
        column = _column(item.field)
        if item.op == "gte":
            stmt = stmt.where(column >= Decimal(str(item.value)))
        elif item.op == "lte":
            stmt = stmt.where(column <= Decimal(str(item.value)))
        elif item.op == "eq":
            stmt = stmt.where(column == item.value)
        elif item.op == "in":
            stmt = stmt.where(column.in_(list(item.value)))  # type: ignore[arg-type]
        elif item.op == "notnull":
            stmt = stmt.where(column.is_not(None))
        else:
            raise ValueError(f"operador desconhecido: {item.op}")
    return stmt


def _row_dict(row: AssetUniverse, where: "Membership") -> dict:
    """One screener row. Decimals stay Decimal; FastAPI serialises them."""
    return {
        "ticker": row.ticker,
        "name": row.name,
        "market": row.market,
        "kind": row.kind,
        "currency": row.currency,
        "sector": row.sector,
        "segmento_b3": row.b3_segment,
        "segmento_do_fundo": row.fund_segment,
        "status": row.status,
        #: Trimmed of the bounding commas the LIKE match needs.
        "indices": [code for code in (row.indexes or "").split(",") if code],
        "preco": row.price,
        "preco_em": row.price_date,
        "valor_de_mercado": row.market_cap,
        "liquidez_media_diaria": row.avg_volume_21d,
        "variacao_12m_pct": row.price_change_12m_pct,
        "volatilidade_12m_pct": row.volatility_12m_pct,
        "maxima_52s": row.high_52w,
        "minima_52s": row.low_52w,
        "dias_negociados_12m": row.traded_days_12m,
        "p_l": row.pe,
        "p_vp": row.pb,
        "roe_pct": row.roe_pct,
        "margem_liquida_pct": row.net_margin_pct,
        "margem_bruta_pct": row.gross_margin_pct,
        "crescimento_receita_pct": row.revenue_growth_pct,
        "crescimento_lucro_pct": row.earnings_growth_pct,
        "divida_sobre_patrimonio": row.debt_to_equity,
        "dividend_yield_pct": row.dividend_yield_pct,
        "payout_pct": row.payout_pct,
        "valor_patrimonial_por_acao": row.book_value_per_share,
        "patrimonio_do_fundo": row.fii_pl,
        "receita": row.revenue,
        "lucro_liquido": row.net_income,
        # Three vintages, never one. The price is yesterday's close, the
        # fundamentals are whatever period the company last filed, and the two
        # can be eight months apart — showing a single "updated at" would
        # imply they move together.
        "exercicio_dos_fundamentos": row.fundamentals_period,
        "origem_dos_precos": row.price_source,
        "origem_dos_fundamentos": row.fundamentals_source,
        "fundamentos_em": row.fundamentals_fetched_at,
        "observacao": row.notes,
        # Three separate facts, never conflated: owning a paper, an AI wallet
        # holding it, and following it are different things.
        "na_carteira": row.ticker in where.portfolio,
        "na_carteira_ia": row.ticker in where.ai_wallets,
        "na_watchlist": row.ticker in where.watchlist,
    }


@dataclass(frozen=True, slots=True)
class Membership:
    """Which of the app's own lists a ticker already appears in.

    Three separate answers, because they mean three different things and only
    one of them is "you own this". An ``Asset`` row existing says none of them:
    the AI wallets mint rows for their virtual positions, the watchlist has its
    own, and merely opening a ticker's page creates one too. Reading "na
    carteira" off the presence of that row told the user they held papers they
    had never bought.
    """

    portfolio: frozenset[str]
    ai_wallets: frozenset[str]
    watchlist: frozenset[str]


def _membership(db: Session) -> Membership:
    """Where each ticker already appears, by the only test that means it."""
    from app.db.models import AiWalletPosition, Transaction, WatchlistItem
    from app.portfolio.service import PortfolioService

    portfolio: set[str] = set()
    portfolio_id = db.scalar(select(Transaction.portfolio_id).limit(1))
    if portfolio_id is not None:
        # An *open* position: a paper bought and since sold is not held, and
        # badging it as owned would be as wrong as badging a virtual one.
        open_ids = {
            position.asset_id
            for position in PortfolioService(db, portfolio_id).positions().values()
            if position.is_open
        }
        if open_ids:
            portfolio = set(
                db.scalars(select(Asset.ticker).where(Asset.id.in_(open_ids))).all()
            )

    ai_wallets = set(
        db.scalars(select(AiWalletPosition.ticker).where(AiWalletPosition.quantity > 0)).all()
    )
    watchlist = set(db.scalars(select(WatchlistItem.ticker)).all())
    return Membership(
        portfolio=frozenset(portfolio),
        ai_wallets=frozenset(ai_wallets),
        watchlist=frozenset(watchlist),
    )


def screen(db: Session, request: ScreenRequest) -> ScreenResult:
    """One page of screened rows, plus what the filters dropped."""
    if request.category:
        spec = CATEGORIES.get(request.category)
        if spec is None:
            raise ValueError(f"categoria desconhecida: {request.category}")
        request = ScreenRequest(
            **{
                **{f.name: getattr(request, f.name) for f in request.__dataclass_fields__.values()},
                "kinds": frozenset(spec["kinds"]) if spec["kinds"] else request.kinds,
                "currency": spec["currency"] or request.currency,
                "category": None,
            }
        )

    if request.order_by not in SORTABLE:
        raise ValueError(f"campo não ordenável: {request.order_by}")

    base = _apply_filters(select(AssetUniverse), request)
    matched = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    order_column = _column(request.order_by)
    required = set(request.require) | {request.order_by}
    stmt = base
    for name in sorted(required):
        stmt = stmt.where(_column(name).is_not(None))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    direction = order_column.desc() if request.descending else order_column.asc()
    stmt = stmt.order_by(
        nullslast(direction),
        # Without a tie-break, two rows with equal values can swap order
        # between dialects — and a screener that is not reproducible is not
        # deterministic, whatever the ordering clause says.
        AssetUniverse.ticker,
    ).limit(request.limit).offset(request.offset)

    rows = list(db.scalars(stmt).all())
    where = _membership(db)
    stalest = db.scalar(
        select(func.min(AssetUniverse.fundamentals_fetched_at)).where(
            AssetUniverse.ticker.in_([row.ticker for row in rows] or [""])
        )
    )
    return ScreenResult(
        rows=[_row_dict(row, where) for row in rows],
        total=total,
        dropped_for_missing_data=max(matched - total, 0),
        stalest_fundamentals_at=stalest,
    )


def coverage(db: Session) -> dict:
    """What the universe currently holds — shown above the screener table."""
    total = db.scalar(select(func.count()).select_from(AssetUniverse)) or 0
    if not total:
        return {"total": 0, "by_market": {}, "by_kind": {}, "with_fundamentals": 0}
    by_market = dict(
        db.execute(select(AssetUniverse.market, func.count()).group_by(AssetUniverse.market)).all()
    )
    by_kind = dict(
        db.execute(select(AssetUniverse.kind, func.count()).group_by(AssetUniverse.kind)).all()
    )
    with_fundamentals = db.scalar(
        select(func.count())
        .select_from(AssetUniverse)
        .where(AssetUniverse.fundamentals_fetched_at.is_not(None))
    )
    return {
        "total": total,
        "by_market": by_market,
        "by_kind": by_kind,
        "with_fundamentals": with_fundamentals or 0,
        "prices_updated_at": db.scalar(select(func.max(AssetUniverse.price_fetched_at))),
        "fundamentals_updated_at": db.scalar(
            select(func.max(AssetUniverse.fundamentals_fetched_at))
        ),
    }


def is_enabled(db: Session) -> bool:
    return state.is_enabled(db)


# ---------------------------------------------------------------------------
# Pre-screened candidates for the AI wallet

#: How each category is ranked before the model sees it, and which metrics
#: travel with the row. Liquidity leads for equities because it is the least
#: opinionated proxy for "investable" — it makes no claim about quality, which
#: is the model's job to argue.
_CATEGORY_RANK: dict[str, str] = {
    "ACOES": "liquidez_media_diaria",
    "FII": "liquidez_media_diaria",
    "ETF": "liquidez_media_diaria",
    # US rows have no market capitalisation — no free bulk source publishes
    # US prices — so revenue is the size ranking there.
    "STOCKS": "receita",
    "REIT": "receita",
}

#: What a candidate must have published before it is worth offering.
#:
#: Class-dependent, and getting it wrong is silent: an ETF has no company
#: behind it filing a balance sheet, so demanding a market capitalisation of
#: one rejects **every** ETF and the category quietly receives no candidates at
#: all. Ações and FIIs are held to the stricter bar on purpose — a share with
#: no fundamentals is not something to hand a model as a considered pick.
_CATEGORY_REQUIRE: dict[str, tuple[str, ...]] = {
    "ACOES": ("valor_de_mercado",),
    "FII": ("valor_de_mercado",),
    "ETF": (),
    # Price-independent on purpose: requiring a market cap would reject every
    # US row, the same way it rejected every ETF.
    "STOCKS": ("receita", "roe_pct"),
    "REIT": ("receita",),
}

#: Fundamentals staleness only bounds classes that *have* fundamentals.
#: An ETF's row is priced daily and never carries a filing date, so applying
#: the cutoff to it would reject the whole class for being "stale".
_CATEGORY_NEEDS_FRESH_FUNDAMENTALS = frozenset({"ACOES", "FII", "STOCKS", "REIT"})

#: Categories whose screening set is narrower than what the wallet will accept.
#:
#: ``CATEGORIES["REIT"]`` admits plain USD equities on purpose — the comment
#: there explains why: the market search reports US REITs as ordinary equities,
#: so the wallet cannot tell them apart and leaves the judgement to the model.
#: The universe *can* tell them apart, because a REIT declares itself to the
#: SEC as SIC 6798. Offering the model only real REITs is strictly better than
#: offering it the whole US market and hoping; the wallet's own acceptance rule
#: stays as loose as it was, so nothing the model picks is newly rejected.
_CATEGORY_SCREEN_KINDS: dict[str, frozenset[str]] = {
    "REIT": frozenset({"REIT"}),
}

#: Fewer than this is not worth a prompt block.
MIN_CANDIDATES = 8

#: Fundamentals older than this are not offered to the model as current.
CANDIDATE_MAX_AGE_DAYS = 120

#: Fields handed over per candidate — the same names phase B will see.
_CANDIDATE_FIELDS = (
    "ticker",
    "name",
    "setor",
    "segmento_do_fundo",
    "preco",
    "valor_de_mercado",
    "p_l",
    "p_vp",
    "roe_pct",
    "margem_liquida_pct",
    "crescimento_receita_pct",
    "crescimento_lucro_pct",
    "divida_sobre_patrimonio",
    "dividend_yield_pct",
    "payout_pct",
    "variacao_12m_pct",
    "liquidez_media_diaria",
    "receita",
    "lucro_liquido",
    "exercicio_dos_fundamentos",
)


def _round(value: object) -> object:
    return round(float(value), 2) if isinstance(value, Decimal) else value


def _diversify(rows: list[dict], key: str, limit: int) -> list[dict]:
    """Interleave by ``key`` so one sector cannot fill the whole list.

    Round-robin in Python rather than ``DISTINCT ON`` or a window function:
    those differ between SQLite and Postgres, and this has to give the desktop
    build and the Docker build the same answer.
    """
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(str(row.get(key) or "—"), []).append(row)
    ordered = sorted(buckets.items())
    out: list[dict] = []
    index = 0
    while len(out) < limit and any(len(items) > index for _, items in ordered):
        for _, items in ordered:
            if index < len(items):
                out.append(items[index])
                if len(out) >= limit:
                    break
        index += 1
    return out


def category_screen(db: Session, category: str, limit: int = 40) -> list[dict]:
    """Pre-screened candidates for one AI wallet category.

    Returns ``[]`` — never raises — when the universe is off, empty, or too
    thin to be useful. A generation that works today must keep working whether
    or not the user has ever run the ingest.
    """
    try:
        if not state.is_enabled(db) or category not in CATEGORIES:
            return []
        rank = _CATEGORY_RANK.get(category)
        if rank is None:
            # CRIPTO and RENDA_FIXA have no rows here by construction.
            return []
        spec = CATEGORIES[category]
        request = ScreenRequest(
            kinds=_CATEGORY_SCREEN_KINDS.get(category) or frozenset(spec["kinds"]),
            currency=spec["currency"],
            order_by=rank,
            descending=True,
            only_active=True,
            require=_CATEGORY_REQUIRE.get(category, ()),
            fresher_than_days=(
                CANDIDATE_MAX_AGE_DAYS
                if category in _CATEGORY_NEEDS_FRESH_FUNDAMENTALS
                else None
            ),
            limit=max(limit * 3, 120),
        )
        result = screen(db, request)
        if len(result.rows) < MIN_CANDIDATES:
            return []
        group = "segmento_do_fundo" if category == "FII" else "setor"
        chosen = _diversify(result.rows, group, limit)
        return [
            {
                key: _round(row.get(key))
                for key in _CANDIDATE_FIELDS
                if row.get(key) is not None
            }
            for row in chosen
        ]
    except Exception:  # noqa: BLE001 — a screener fault must never block a run
        logger.exception("category_screen failed for %s", category)
        return []


# ---------------------------------------------------------------------------
# "Como me comparo": the real portfolio against its market


def _percentile(value: Decimal, population: list[Decimal], higher_is_better: bool) -> int | None:
    """Where ``value`` sits within ``population``, as a 0-100 percentile."""
    if not population:
        return None
    below = sum(1 for other in population if other < value)
    percentile = round(below / len(population) * 100)
    return percentile if higher_is_better else 100 - percentile


#: Families the sector comparison covers. It must match the denominator the
#: market weights are computed over, or the two sides measure different things.
_SECTOR_KINDS = frozenset({"STOCK", "UNIT"})

#: Metrics compared against sector peers, and which direction is favourable.
_FIT_METRICS: tuple[tuple[str, str, bool], ...] = (
    ("p_l", "pe", False),
    ("p_vp", "pb", False),
    ("roe_pct", "roe_pct", True),
    ("dividend_yield_pct", "dividend_yield_pct", True),
)


def portfolio_fit(db: Session, portfolio_id: int) -> dict:
    """How the held portfolio sits against the universe it was picked from.

    Answers three questions the screener alone cannot: which sectors this
    portfolio over- and under-weights against the market, where each holding
    ranks among its own sector peers, and which sectors it has no exposure to
    at all.

    Holdings the universe does not cover — fixed income, Tesouro, crypto,
    foreign brokers — are listed separately rather than dropped, so the
    percentages below always say what they are a percentage *of*.
    """
    from app.portfolio.service import PortfolioService

    if not state.is_enabled(db):
        return {"enabled": False}

    service = PortfolioService(db, portfolio_id)
    positions = [
        position
        for position in service.asset_positions()
        if position.market_value and position.market_value > 0
    ]
    if not positions:
        return {"enabled": True, "holdings": [], "sectors": [], "gaps": [], "outside": []}

    universe = {
        row.ticker: row
        for row in db.scalars(
            select(AssetUniverse).where(
                AssetUniverse.ticker.in_([p.asset.ticker for p in positions])
            )
        ).all()
    }

    # Sector populations, for the peer percentiles below.
    peers: dict[str, dict[str, list[Decimal]]] = {}
    for row in db.scalars(
        select(AssetUniverse).where(
            AssetUniverse.sector.is_not(None),
            AssetUniverse.status.notin_(("CANCELADA", "INATIVO")),
        )
    ).all():
        bucket = peers.setdefault(row.sector or "", {})
        for label, attribute, _ in _FIT_METRICS:
            value = getattr(row, attribute, None)
            if value is not None:
                bucket.setdefault(label, []).append(Decimal(value))

    total_value = sum((Decimal(p.market_value) for p in positions), Decimal(0))
    holdings: list[dict] = []
    outside: list[str] = []
    unclassified: list[str] = []
    mine_by_sector: dict[str, Decimal] = {}

    for position in positions:
        ticker = position.asset.ticker
        row = universe.get(ticker)
        value = Decimal(position.market_value)
        if row is None:
            outside.append(ticker)
            continue
        sector = row.sector
        if sector and row.kind in _SECTOR_KINDS:
            mine_by_sector[sector] = mine_by_sector.get(sector, Decimal(0)) + value
        else:
            # FIIs, ETFs and BDRs carry no CVM sector, and the market weights
            # below are a company-cap denominator. Bucketing them under "—"
            # would report a large overweight in a sector that does not exist;
            # they are named instead, and still appear in the holdings table.
            unclassified.append(ticker)
        ranks: dict[str, int | None] = {}
        for label, attribute, higher_is_better in _FIT_METRICS:
            mine = getattr(row, attribute, None)
            ranks[label] = (
                _percentile(Decimal(mine), peers.get(sector, {}).get(label, []), higher_is_better)
                if mine is not None
                else None
            )
        holdings.append(
            {
                "ticker": ticker,
                "name": row.name,
                "setor": row.sector,
                "peso_pct": _round(value / total_value * 100) if total_value else None,
                "p_l": _round(row.pe),
                "p_vp": _round(row.pb),
                "roe_pct": _round(row.roe_pct),
                "dividend_yield_pct": _round(row.dividend_yield_pct),
                "percentis_no_setor": ranks,
                "receita": row.revenue,
        "lucro_liquido": row.net_income,
        # Three vintages, never one. The price is yesterday's close, the
        # fundamentals are whatever period the company last filed, and the two
        # can be eight months apart — showing a single "updated at" would
        # imply they move together.
        "exercicio_dos_fundamentos": row.fundamentals_period,
        "origem_dos_precos": row.price_source,
        "origem_dos_fundamentos": row.fundamentals_source,
            }
        )

    # Market weights are cap-weighted over the same active, screenable rows.
    market_by_sector = dict(
        db.execute(
            select(AssetUniverse.sector, func.sum(AssetUniverse.market_cap))
            .where(
                AssetUniverse.sector.is_not(None),
                AssetUniverse.market_cap.is_not(None),
                AssetUniverse.status.notin_(("CANCELADA", "INATIVO")),
                AssetUniverse.kind.in_(("STOCK", "UNIT")),
            )
            .group_by(AssetUniverse.sector)
        ).all()
    )
    market_total = sum((Decimal(v) for v in market_by_sector.values()), Decimal(0))
    covered = sum(mine_by_sector.values(), Decimal(0))

    sectors = []
    for sector in sorted(set(mine_by_sector) | set(market_by_sector)):
        mine_pct = (
            mine_by_sector.get(sector, Decimal(0)) / covered * 100 if covered else Decimal(0)
        )
        market_pct = (
            Decimal(market_by_sector.get(sector, 0)) / market_total * 100
            if market_total
            else Decimal(0)
        )
        if mine_pct == 0 and market_pct < 1:
            continue  # a sector neither held nor material is just noise
        sectors.append(
            {
                "setor": sector,
                "meu_peso_pct": _round(mine_pct),
                "peso_de_mercado_pct": _round(market_pct),
                "diferenca_pp": _round(mine_pct - market_pct),
            }
        )
    sectors.sort(key=lambda item: abs(item["diferenca_pp"] or 0), reverse=True)

    gaps = [
        item["setor"]
        for item in sectors
        if (item["meu_peso_pct"] or 0) == 0 and (item["peso_de_mercado_pct"] or 0) >= 2
    ]

    return {
        "enabled": True,
        "holdings": sorted(holdings, key=lambda item: item["peso_pct"] or 0, reverse=True),
        "sectors": sectors,
        "gaps": gaps,
        # Named, not silently excluded: these are real money the comparison
        # cannot speak to, and the weights above are shares of what it can.
        "outside": sorted(outside),
        #: In the universe, but with no sector to compare against — FIIs, ETFs
        #: and BDRs. They have their own metrics in the holdings table.
        "sem_setor": sorted(unclassified),
        "coberto_pct": _round(covered / total_value * 100) if total_value else None,
    }
