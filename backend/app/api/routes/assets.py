"""Per-asset endpoints: detail page, price overrides, notes, history."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.deps import CurrentPortfolio, DbSession, PortfolioSvc
from app.db.models import Asset, AssetFundamentals, AuditLog, Broker, PriceHistory, Quote, Transaction
from app.domain.enums import INCOME_TYPES, AssetKind, Direction, OperationType
from app.importer.classifier import classify
from app.importer.dedup import exact_fingerprint
from app.market import fundamentals, lookup
from app.market.service import ensure_market_data, refresh_if_stale, resolve_market_symbol
from app.portfolio.engine import QTY_EPSILON

router = APIRouter(prefix="/assets", tags=["assets"])
INCOME_VALUES = [t.value for t in INCOME_TYPES]
#: Withholding taken out of income. It is not income itself, which is why it is
#: excluded above, but it decides what actually reached the account — so the
#: income series has to see it.
#:
#: Commissions are deliberately *not* here, matching the Proventos page: a
#: brokerage charge is a cost of buying, not a deduction from a dividend.
#: Counting them made a month of two US$2.50 purchases and no dividend show a
#: dividend of −US$5.00.
INCOME_COST_VALUES = [OperationType.TAX.value]


def _income_by_month(transactions: list[Transaction]) -> list[dict]:
    """Monthly income for one asset, gross and net, in the asset's own currency.

    This mirrors what the Proventos page does portfolio-wide, and it has to:
    the two screens showing different numbers for the same month is a bug in
    itself. Withholding is signed, so a refund — Apex returns them years later,
    and CIO's April 2026 is *nothing but* three of them — raises the net rather
    than lowering it, and a month with no dividend can still pay.

    A month can still come out negative, and legitimately: withholding is often
    dated a few days after the payment it was taken from, so an end-of-month
    dividend and its tax can land either side of the boundary.
    """
    months: dict[str, dict] = {}

    def bucket(day) -> dict:
        return months.setdefault(
            f"{day.year}-{day.month:02d}",
            {"period": f"{day.year}-{day.month:02d}", "gross": Decimal(0), "tax": Decimal(0), "payments": 0},
        )

    for t in transactions:
        if t.op_type in INCOME_VALUES:
            row = bucket(t.trade_date)
            row["gross"] += t.net_amount
            row["payments"] += 1
        elif t.op_type in INCOME_COST_VALUES:
            # `net_amount` is negative when cash left, so the sign is flipped to
            # read as "amount withheld".
            bucket(t.trade_date)["tax"] -= t.net_amount

    return [
        {**row, "net": row["gross"] - row["tax"]}
        for row in sorted(months.values(), key=lambda r: r["period"])
    ]


class AssetUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    sector: str | None = None
    market_symbol: str | None = None
    price_manual: bool | None = None
    manual_price: Decimal | None = Field(default=None, ge=0)
    notes: str | None = None
    #: Kept as digits only, whatever the user typed: the IRPF worksheet formats
    #: it for reading, and a CNPJ stored once with dots and once without is two
    #: different payers to every lookup that follows.
    cnpj: str | None = None

    @field_validator("cnpj")
    @classmethod
    def _digits_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        digits = "".join(character for character in value if character.isdigit())
        if not digits:
            return None
        if len(digits) != 14:
            raise ValueError("um CNPJ tem 14 dígitos")
        return digits


def _get_asset(db: DbSession, ticker: str) -> Asset:
    asset = db.scalar(select(Asset).where(Asset.ticker == ticker.upper()))
    if asset is None:
        raise HTTPException(status_code=404, detail=f"asset {ticker} not found")
    return asset


@router.get("", response_model=None, summary="All assets with their current position")
def list_assets(service: PortfolioSvc, include_closed: bool = True) -> list[dict]:
    items = service.asset_positions(include_closed=include_closed)
    total = sum((item.market_value_base for item in items), Decimal(0))
    return [item.to_dict(total) for item in items]


def _from_universe(db: DbSession, ticker: str) -> Asset | None:
    """Build the row from the local universe, when it already knows the ticker.

    Saves a market search per page open: the universe was downloaded from B3
    and the CVM precisely so questions like "what is this ticker" can be
    answered without asking a provider. Returns an unsaved ``Asset``; the
    caller persists it.
    """
    from app.db.models import AssetUniverse

    row = db.scalar(select(AssetUniverse).where(AssetUniverse.ticker == ticker.upper()))
    if row is None:
        return None
    return Asset(
        ticker=row.ticker,
        name=(row.name or row.ticker)[:255],
        kind=row.kind,
        currency=row.currency or "BRL",
        market_symbol=row.market_symbol,
        sector=row.sector,
    )


def _create_watch_only(db: DbSession, ticker: str) -> Asset:
    """Register a paper the portfolio never traded, so its page can exist.

    The ticker is validated before anything is written — a typo'd URL must
    404, never mint a junk row. The local universe answers first when it knows
    the ticker, which saves a market search per page open; otherwise the
    provider's search decides.

    The row has no transactions and therefore no position; it only gives
    quotes, history and fundamentals somewhere to hang. It is deliberately
    *not* enrolled in the scheduled refresh — see
    :func:`app.market.service.tracked_asset_ids` — so browsing the universe
    cannot grow that job one click at a time. If the ticker is ever actually
    bought, the importer finds this row by ticker and it becomes a held one
    with no migration.
    """
    source = "universo"
    asset = _from_universe(db, ticker)
    if asset is None:
        hit = lookup.resolve(ticker)
        if hit is None:
            raise HTTPException(status_code=404, detail=f"asset {ticker} not found")
        source = "mercado"
        asset = Asset(
            ticker=hit.ticker,
            name=hit.name[:255],
            kind=hit.kind,
            currency=hit.currency,
            market_symbol=hit.market_symbol,
        )
    db.add(asset)
    db.add(
        AuditLog(
            action="asset.watch",
            detail={"ticker": asset.ticker, "name": asset.name, "source": source},
        )
    )
    db.commit()
    db.refresh(asset)
    # Synchronous on purpose: the page being opened right now needs a price
    # and a chart, not a promise of tomorrow's backfill.
    ensure_market_data(db, asset)
    return asset


def _watch_only_payload(db: DbSession, asset: Asset) -> dict:
    """The detail response for an asset with no transactions.

    Same shape as the held response with the position zeroed out, so the
    frontend reads one type; ``held: False`` is what tells it to drop the
    wallet tabs.
    """
    quote = db.get(Quote, asset.id)
    price = asset.manual_price if asset.price_manual else (quote.price if quote else None)
    zero = Decimal(0)
    return {
        "held": False,
        "asset_id": asset.id,
        "ticker": asset.ticker,
        "name": asset.name or asset.ticker,
        "kind": asset.kind,
        "currency": asset.currency or "BRL",
        "is_foreign": (asset.currency or "BRL").upper() != "BRL",
        "is_open": False,
        "current_price": price,
        "has_market_price": price is not None,
        "price_source": "manual" if asset.price_manual else (quote.source if quote else None),
        "day_change": quote.change if quote else None,
        "day_change_pct": quote.change_percent if quote else None,
        "fx_rate": None,
        "quantity": zero,
        "average_price": zero,
        "cost_basis": zero,
        "cost_basis_base": zero,
        "market_value": zero,
        "market_value_base": zero,
        "unrealized_pnl": zero,
        "unrealized_pct": zero,
        "unrealized_pnl_base": zero,
        "realized_pnl": zero,
        "realized_pnl_base": zero,
        "income": zero,
        "income_base": zero,
        "income_by_type": {},
        "income_tax": zero,
        "returned_capital": zero,
        "uncosted_proceeds": zero,
        "uncosted_quantity": zero,
        "staked_quantity": zero,
        "total_return": zero,
        "total_return_pct": zero,
        "total_return_base": zero,
        "allocation_pct": zero,
        "day_change_base": zero,
        "first_trade": None,
        "last_trade": None,
        "is_convertible": False,
        "warnings": [],
        "notes": [],
        "sector": asset.sector,
        "market_symbol": asset.market_symbol,
        "price_manual": asset.price_manual,
        "manual_price": asset.manual_price,
        "user_notes": asset.notes,
        "transactions": [],
        "transactions_count": 0,
        "dividends": [],
        "income_months": [],
    }


@router.get("/{ticker}", response_model=None, summary="Full detail for a single asset")
def asset_detail(ticker: str, db: DbSession, service: PortfolioSvc) -> dict:
    asset = db.scalar(select(Asset).where(Asset.ticker == ticker.upper()))
    if asset is None:
        # Unknown ticker: not a 404 yet — if the market knows it, the page
        # becomes a watch-only view (search and the asset universe hand out
        # these URLs).
        asset = _create_watch_only(db, ticker)
    else:
        # A paper nobody asked to track is not on the refresh schedule, so its
        # price is brought up to date here — when someone is actually looking.
        refresh_if_stale(db, asset, service.portfolio_id)
    items = service.asset_positions(include_closed=True)
    total = sum((item.market_value_base for item in items), Decimal(0))
    match = next((item for item in items if item.asset.id == asset.id), None)
    if match is None:
        return _watch_only_payload(db, asset)

    brokers = {b.id: b.canonical_name for b in db.scalars(select(Broker)).all()}
    transactions = db.scalars(
        select(Transaction)
        .where(Transaction.asset_id == asset.id, Transaction.portfolio_id == service.portfolio_id)
        .order_by(Transaction.trade_date.desc(), Transaction.id.desc())
    ).all()

    return {
        **match.to_dict(total),
        "held": True,
        # `transactions` carries the ledger here, so the count from to_dict()
        # is re-exposed under an explicit name.
        "transactions_count": match.position.transactions,
        "sector": asset.sector,
        "market_symbol": asset.market_symbol,
        "price_manual": asset.price_manual,
        "manual_price": asset.manual_price,
        # `notes` from to_dict() holds the engine's interpretation notes; the
        # user's own free text travels separately.
        "user_notes": asset.notes,
        "transactions": [
            {
                "id": t.id,
                "date": t.trade_date,
                "op_type": t.op_type,
                "effect": t.effect,
                "movement": t.raw_movement,
                "direction": t.direction,
                "quantity": t.quantity,
                "unit_price": t.unit_price,
                "gross_amount": t.gross_amount,
                "net_amount": t.net_amount,
                "broker": brokers.get(t.broker_id),
                "notes": t.notes,
            }
            for t in transactions
        ],
        "dividends": [
            {
                "date": t.trade_date,
                "op_type": t.op_type,
                "amount": t.net_amount,
                "unit_price": t.unit_price,
                "quantity": t.quantity,
            }
            for t in transactions
            if t.op_type in INCOME_VALUES
        ],
        # Pre-aggregated so the chart does not have to re-derive withholding.
        "income_months": _income_by_month(list(transactions)),
        "income_tax": sum(
            (-t.net_amount for t in transactions if t.op_type in INCOME_COST_VALUES),
            Decimal(0),
        ),
    }


@router.get("/{ticker}/price-history", response_model=None, summary="Stored daily closes for the asset")
def price_history(ticker: str, db: DbSession, limit: int = Query(default=2000, le=10000)) -> list[dict]:
    asset = _get_asset(db, ticker)
    rows = db.execute(
        select(PriceHistory.date, PriceHistory.close)
        .where(PriceHistory.asset_id == asset.id)
        .order_by(PriceHistory.date.desc())
        .limit(limit)
    ).all()
    return [{"date": day, "close": close} for day, close in reversed(rows)]


#: Families with no company behind them — a CDB has no revenue and Bitcoin has
#: no dividend policy. Asking about them wastes a request and returns nothing.
_NO_FUNDAMENTALS = {
    AssetKind.FIXED_INCOME.value,
    AssetKind.TREASURY.value,
    AssetKind.CRYPTO.value,
    AssetKind.STABLECOIN.value,
    AssetKind.SUBSCRIPTION.value,
    AssetKind.FUTURE.value,
    AssetKind.OPTION.value,
    AssetKind.OTHER.value,
}

#: Fundamentals move quarterly; a day-old copy is a current copy.
_FUNDAMENTALS_TTL = timedelta(hours=12)


@router.get("/{ticker}/fundamentals", response_model=None, summary="Company data behind the asset")
def asset_fundamentals(ticker: str, db: DbSession, refresh: bool = False) -> dict:
    """Valuation, results and the dividend schedule, cached per asset.

    Served from the cache unless it is stale or ``refresh`` is set, so opening
    an asset page never waits on an upstream API. A failed fetch keeps whatever
    was cached rather than replacing it with an empty answer.
    """
    asset = _get_asset(db, ticker)
    if asset.kind in _NO_FUNDAMENTALS or asset.price_manual:
        return {"ticker": asset.ticker, "supported": False, "data": None}

    cached = db.get(AssetFundamentals, asset.id)
    fresh = (
        cached is not None
        and cached.fetched_at is not None
        and cached.fetched_at.replace(tzinfo=cached.fetched_at.tzinfo or UTC)
        > datetime.now(UTC) - _FUNDAMENTALS_TTL
    )
    if cached is not None and fresh and not refresh:
        return {"ticker": asset.ticker, "supported": True, "data": cached.data, "fetched_at": cached.fetched_at}

    symbol = resolve_market_symbol(asset)
    data = fundamentals.fetch(symbol, asset.ticker, (asset.currency or "BRL").upper() == "BRL")
    if not data.get("has_data"):
        # Nothing came back: keep the last good copy rather than blanking it.
        if cached is not None:
            return {
                "ticker": asset.ticker,
                "supported": True,
                "data": cached.data,
                "fetched_at": cached.fetched_at,
                "stale": True,
            }
        return {"ticker": asset.ticker, "supported": True, "data": None}

    now = datetime.now(UTC)
    db.merge(AssetFundamentals(asset_id=asset.id, data=data, source="yahoo", fetched_at=now))
    # The sector is the one field worth keeping on the asset itself: every
    # screen that groups by sector reads it from there.
    if data.get("sector") and not asset.sector:
        asset.sector = str(data["sector"])[:120]
    db.commit()
    return {"ticker": asset.ticker, "supported": True, "data": data, "fetched_at": now}


class BalanceAdjustment(BaseModel):
    """The balance the venue actually reports for an asset."""

    quantity: Decimal = Field(ge=0, description="Real balance, in the asset's own units")
    date: date_type | None = Field(default=None, description="Defaults to today")
    note: str | None = Field(default=None, max_length=200)


@router.post(
    "/{ticker}/reconcile",
    response_model=None,
    summary="Record the balance the venue actually reports",
)
def reconcile_balance(
    ticker: str,
    payload: BalanceAdjustment,
    db: DbSession,
    portfolio: CurrentPortfolio,
    service: PortfolioSvc,
) -> dict:
    """Correct a position to the balance you can see, without editing history.

    Some of what a portfolio holds cannot be derived from an export. Interest
    that compounds *inside* a staking product is paid into the position rather
    than itemised as a movement, so the balance creeps up with nothing on file
    to explain it — a computed stablecoin balance ends up a few units short of
    what the exchange itself shows.

    Positions stay derived: nothing is overwritten. The difference is appended
    as one more movement, so the audit trail says exactly where it came from and
    the replay keeps producing the number. Posting the same balance twice is a
    no-op, because the second call computes a difference of zero.
    """
    asset = _get_asset(db, ticker)
    position = service.positions().get(asset.id)
    current = position.quantity if position else Decimal(0)
    difference = payload.quantity - current
    when = payload.date or date_type.today()

    if abs(difference) <= QTY_EPSILON:
        return {
            "ticker": asset.ticker,
            "previous": current,
            "quantity": payload.quantity,
            "difference": Decimal(0),
            "applied": False,
            "detail": "a posição já corresponde ao saldo informado",
        }

    direction = Direction.CREDIT if difference > 0 else Direction.DEBIT
    movement = "Balance adjustment"
    classification = classify(movement, direction, Decimal(0))
    note = payload.note or f"saldo informado pelo usuário em {when.isoformat()}"
    # Deterministic, so re-posting the same correction cannot stack up.
    fingerprint = exact_fingerprint(
        "balance-adjustment",
        when.isoformat(),
        asset.ticker,
        format(payload.quantity.normalize(), "f"),
    )

    db.add(
        Transaction(
            portfolio_id=portfolio.id,
            asset_id=asset.id,
            broker_id=None,
            import_batch_id=None,
            trade_date=when,
            direction=direction.value,
            op_type=classification.op_type.value,
            effect=classification.effect.value,
            quantity=abs(difference),
            unit_price=Decimal(0),
            gross_amount=Decimal(0),
            fees=Decimal(0),
            taxes=Decimal(0),
            net_amount=Decimal(0),
            currency=asset.currency or portfolio.base_currency,
            fx_rate=None,
            raw_movement=movement,
            raw_product=asset.name or asset.ticker,
            raw_institution="",
            source_line=None,
            dedup_key=f"{fingerprint}:0",
            occurrence=0,
            notes=note,
        )
    )
    db.add(
        AuditLog(
            action="asset.reconcile",
            detail={
                "ticker": asset.ticker,
                "previous": str(current),
                "quantity": str(payload.quantity),
                "difference": str(difference),
            },
        )
    )
    db.commit()
    return {
        "ticker": asset.ticker,
        "previous": current,
        "quantity": payload.quantity,
        "difference": difference,
        "applied": True,
        "date": when,
    }


@router.patch("/{ticker}", response_model=None, summary="Update asset metadata, notes or manual price")
def update_asset(ticker: str, payload: AssetUpdate, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    asset = _get_asset(db, ticker)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(asset, field, value)
    db.commit()
    db.refresh(asset)
    return {
        "ticker": asset.ticker,
        "name": asset.name,
        "kind": asset.kind,
        "sector": asset.sector,
        "market_symbol": asset.market_symbol,
        "price_manual": asset.price_manual,
        "manual_price": asset.manual_price,
        "cnpj": asset.cnpj,
        "user_notes": asset.notes,
    }
