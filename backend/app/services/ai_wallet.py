"""AI wallets: virtual portfolios managed entirely by an AI model.

A wallet holds positions in its own tables — never ``Transaction`` rows — so
nothing here can touch the user's real portfolio, and the AI never sees it.
Listed picks are backed by (possibly watch-only) ``Asset`` rows, which keeps
them inside the ordinary quote-refresh loop; renda fixa picks are synthetic
papers (indexer + rate) accrued with the same engine that values real CDBs.

Every mutation is Decimal-exact, capped by the category's virtual cash, and
recorded in ``ai_wallet_events`` together with the provider/model that decided
it. The functions here are deliberately free of HTTP/SSE concerns so the
schedulers and the tests can call them directly.
"""
from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import delete as sa_delete
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.dates import local_today
from app.core.logging import get_logger
from app.db.models import (
    AiWallet,
    AiWalletCategory,
    AiWalletEvent,
    AiWalletPosition,
    AiWalletSnapshot,
    AiWalletSuggestion,
    Asset,
    AssetFundamentals,
    AuditLog,
    IndexRate,
    PriceHistory,
    Quote,
    Transaction,
    WatchlistItem,
)
from app.db.upsert import dialect_insert
from app.domain.enums import AssetKind
from app.importer.crypto import symbols as coins
from app.market import lookup
from app.market.fixed_income import accrual_factor
from app.market.fx import FxTable, load_table
from app.market.indices import load_series
from app.market.service import ensure_market_data, resolve_market_symbol

logger = get_logger(__name__)

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)
CENT = Decimal("0.01")

#: Virtual money each category receives on its first generation.
DEFAULT_BUDGET = Decimal(10000)

#: The tabs of the feature. ``currency`` restricts what a category may hold
#: (None = both); ``kinds`` are the AssetKind values accepted from lookup or
#: from an existing Asset row. REIT accepts plain USD equities because Yahoo's
#: search reports US REITs as EQUITY — membership there is the model's call.
CATEGORIES: dict[str, dict] = {
    "ACOES": {
        "label": "Ações",
        "currency": "BRL",
        "kinds": {AssetKind.STOCK.value, AssetKind.BDR.value, AssetKind.UNIT.value},
    },
    "FII": {"label": "FIIs", "currency": "BRL", "kinds": {AssetKind.FII.value}},
    "ETF": {
        "label": "ETFs",
        "currency": None,
        "kinds": {AssetKind.ETF.value, AssetKind.ETF_INTL.value},
    },
    "STOCKS": {
        "label": "Stocks",
        "currency": "USD",
        "kinds": {AssetKind.STOCK.value, AssetKind.STOCK_INTL.value},
    },
    "REIT": {
        "label": "REITs",
        "currency": "USD",
        "kinds": {AssetKind.STOCK.value, AssetKind.STOCK_INTL.value, AssetKind.REIT.value},
    },
    "CRIPTO": {"label": "Cripto", "currency": "USD", "kinds": set(coins.CRYPTO_KINDS)},
    "RENDA_FIXA": {"label": "Renda fixa", "currency": "BRL", "kinds": set()},
}

FI_INDEX_CODES = {"CDI", "SELIC", "IPCA", "PRE"}

#: Yahoo rate-limits burst lookups; one paced retry rescues most legitimate
#: tickers before they are declared unknown. Tests set this to 0.
RESOLVE_RETRY_DELAY = 1.5


def _jsonable(value):
    """Decimals as strings, dates as ISO — for JSON columns and SSE results."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def log_event(
    db: Session,
    wallet_id: int,
    action: str,
    *,
    category: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    detail: dict | None = None,
) -> None:
    db.add(
        AiWalletEvent(
            wallet_id=wallet_id,
            category=category,
            action=action,
            provider=provider,
            model=model,
            detail=_jsonable(detail or {}),
        )
    )


# ---------------------------------------------------------------------------
# Asset resolution


def _fits_category(kind: str, currency: str, category: str) -> bool:
    spec = CATEGORIES[category]
    if spec["currency"] and (currency or "BRL").upper() != spec["currency"]:
        return False
    return kind in spec["kinds"]


def _create_asset(db: Session, **fields) -> Asset:
    """A watch-only Asset row + first quote/history (assets._create_watch_only)."""
    asset = Asset(**fields)
    db.add(asset)
    db.add(AuditLog(action="asset.watch", detail={"ticker": asset.ticker, "name": asset.name}))
    db.commit()
    db.refresh(asset)
    try:
        ensure_market_data(db, asset)
    except Exception:  # noqa: BLE001 — priceless is recoverable, a failed page is not
        logger.exception("ai wallet: market data failed for %s", asset.ticker)
    return asset


def _crypto_asset(db: Session, symbol: str, created: list[int] | None = None) -> Asset | None:
    """Resolve/create a coin. Lookup drops crypto, so the pair is checked directly."""
    symbol = coins.asset_symbol(symbol)
    if not symbol or coins.is_fiat(symbol):
        return None
    asset = db.scalar(select(Asset).where(Asset.ticker == symbol))
    if asset is not None and asset.kind in coins.CRYPTO_KINDS:
        return asset
    suffixed = db.scalar(select(Asset).where(Asset.ticker == symbol + coins.TICKER_SUFFIX))
    if suffixed is not None:
        return suffixed

    market_symbol = coins.market_symbol_for(symbol)
    from app.market.providers import get_provider  # local: avoids a cycle

    provider = get_provider()
    if provider.name == "none":
        return None
    quotes = provider.get_quotes([market_symbol])
    if market_symbol not in quotes and RESOLVE_RETRY_DELAY:
        time.sleep(RESOLVE_RETRY_DELAY)
        quotes = provider.get_quotes([market_symbol])
    if market_symbol not in quotes:
        return None  # a coin the provider cannot price is as good as nonexistent
    ticker = symbol if asset is None else symbol + coins.TICKER_SUFFIX
    minted = _create_asset(
        db,
        ticker=ticker,
        name=coins.coin_name(symbol),
        kind=coins.coin_kind(symbol).value,
        currency="USD",
        market_symbol=market_symbol,
    )
    if created is not None:
        created.append(minted.id)
    return minted


def get_or_create_wallet_asset(
    db: Session, category: str, ticker: str, created: list[int] | None = None
) -> Asset | None:
    """The Asset behind an AI pick, or None when the market disowns it.

    Never raises and never mints a junk row: an unresolvable or out-of-category
    ticker is simply skipped by the caller (and surfaced to the user).
    ``created`` collects the ids of rows this call minted (as opposed to
    found), so a run can delete its unused verification scratch afterwards.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None
    if category == "CRIPTO":
        return _crypto_asset(db, ticker, created)

    asset = db.scalar(select(Asset).where(Asset.ticker == ticker))
    if asset is not None:
        return asset if _fits_category(asset.kind, asset.currency, category) else None

    hit = lookup.resolve(ticker)
    if hit is None and RESOLVE_RETRY_DELAY:
        time.sleep(RESOLVE_RETRY_DELAY)
        hit = lookup.resolve(ticker)
    if hit is None or not _fits_category(hit.kind, hit.currency, category):
        return None
    # resolve() normalises (PETR4.SA -> PETR4); re-check under the final ticker.
    existing = db.scalar(select(Asset).where(Asset.ticker == hit.ticker))
    if existing is not None:
        return existing if _fits_category(existing.kind, existing.currency, category) else None
    minted = _create_asset(
        db,
        ticker=hit.ticker,
        name=hit.name[:255],
        kind=hit.kind,
        currency=hit.currency,
        market_symbol=hit.market_symbol,
    )
    if created is not None:
        created.append(minted.id)
    return minted


# ---------------------------------------------------------------------------
# Pricing and trade math


def quantity_step(category: str, currency: str) -> Decimal:
    """B3 trades whole shares; US brokers sell fractions; coins split to 8 dp."""
    if category == "CRIPTO":
        return Decimal("0.00000001")
    if (currency or "BRL").upper() == "BRL":
        return ONE
    return Decimal("0.0001")


def _live_price(db: Session, asset_id: int | None) -> tuple[Decimal, str] | None:
    if asset_id is None:
        return None
    quote = db.get(Quote, asset_id)
    if quote is None or quote.price is None:
        return None
    return Decimal(quote.price), (quote.currency or "BRL").upper()


def _last_close(db: Session, asset_id: int | None) -> Decimal | None:
    if asset_id is None:
        return None
    close = db.scalar(
        select(PriceHistory.close)
        .where(PriceHistory.asset_id == asset_id)
        .order_by(PriceHistory.date.desc())
        .limit(1)
    )
    return Decimal(close) if close is not None else None


def _trade_price(db: Session, asset: Asset) -> Decimal | None:
    """Latest price to trade at: the Quote row, else the last stored close."""
    live = _live_price(db, asset.id)
    if live is not None:
        return live[0]
    return _last_close(db, asset.id)


def _price_brl(price: Decimal, currency: str, fx: FxTable) -> Decimal | None:
    """Never rate 1 for a foreign asset: better no trade than a wrong one."""
    if (currency or "BRL").upper() == "BRL":
        return price
    rate = fx.latest
    if rate is None:
        return None
    return price * Decimal(rate)


def _find_position(
    db: Session, wallet_id: int, category: str, ticker: str
) -> AiWalletPosition | None:
    return db.scalar(
        select(AiWalletPosition).where(
            AiWalletPosition.wallet_id == wallet_id,
            AiWalletPosition.category == category,
            AiWalletPosition.ticker == ticker,
        )
    )


def _blank_position(cat_row: AiWalletCategory, asset: Asset, rationale: str | None) -> AiWalletPosition:
    return AiWalletPosition(
        wallet_id=cat_row.wallet_id,
        category=cat_row.category,
        asset_id=asset.id,
        ticker=asset.ticker,
        name=asset.name or asset.ticker,
        currency=(asset.currency or "BRL").upper(),
        quantity=ZERO,
        avg_price=ZERO,
        cost_brl=ZERO,
        pending_brl=ZERO,
        is_fixed_income=False,
        rationale=rationale,
    )


def _apply_buy_to_position(
    position: AiWalletPosition, quantity: Decimal, price: Decimal, rate: Decimal | None, cost: Decimal
) -> None:
    """Fold one fill into the row with weighted averages."""
    old_qty = Decimal(position.quantity)
    total = old_qty + quantity
    if old_qty > ZERO:
        position.avg_price = (Decimal(position.avg_price) * old_qty + price * quantity) / total
        if rate is not None:
            old_fx = Decimal(position.avg_fx) if position.avg_fx is not None else rate
            position.avg_fx = (old_fx * old_qty + rate * quantity) / total
    else:
        position.avg_price = price
        position.avg_fx = rate
    position.quantity = total
    position.cost_brl = Decimal(position.cost_brl) + cost


def buy_into_category(
    db: Session,
    cat_row: AiWalletCategory,
    asset: Asset,
    amount_brl: Decimal,
    fx: FxTable,
    rationale: str | None = None,
) -> tuple[AiWalletPosition, Decimal] | None:
    """Spend up to ``amount_brl`` of the category's cash on ``asset``.

    Returns ``(position, cost)`` or None when the buy cannot be priced or the
    amount does not cover one tradable unit. Merges into an existing row with
    weighted averages — the wallet has one row per (category, ticker).
    """
    amount = min(Decimal(amount_brl), Decimal(cat_row.cash)).quantize(CENT, ROUND_DOWN)
    if amount <= ZERO:
        return None
    currency = (asset.currency or "BRL").upper()
    price = _trade_price(db, asset)
    if price is None:
        return None
    price_brl = _price_brl(price, currency, fx)
    if price_brl is None or price_brl <= ZERO:
        return None
    step = quantity_step(cat_row.category, currency)
    quantity = (amount / price_brl).quantize(step, ROUND_DOWN)
    if quantity <= ZERO:
        return None
    cost = (quantity * price_brl).quantize(CENT, ROUND_DOWN)
    rate = (price_brl / price) if currency != "BRL" else None

    position = _find_position(db, cat_row.wallet_id, cat_row.category, asset.ticker)
    if position is None:
        position = _blank_position(cat_row, asset, rationale)
        db.add(position)
    _apply_buy_to_position(position, quantity, price, rate, cost)
    if rationale:
        position.rationale = rationale
    cat_row.cash = Decimal(cat_row.cash) - cost
    db.flush()
    return position, cost


def defer_buy(
    db: Session,
    cat_row: AiWalletCategory,
    asset: Asset,
    amount_brl: Decimal,
    rationale: str | None = None,
) -> tuple[AiWalletPosition, Decimal] | None:
    """Reserve cash for a resolvable asset that has no usable price yet.

    The market gate already passed — only the quote (or FX) is missing, which
    is usually a transient rate limit. The allocation must not silently shrink
    to cash, so the money is parked on the position and
    :func:`settle_pending_positions` completes the buy once a price shows up.
    """
    amount = min(Decimal(amount_brl), Decimal(cat_row.cash)).quantize(CENT, ROUND_DOWN)
    if amount <= ZERO:
        return None
    position = _find_position(db, cat_row.wallet_id, cat_row.category, asset.ticker)
    if position is None:
        position = _blank_position(cat_row, asset, rationale)
        db.add(position)
    position.pending_brl = Decimal(position.pending_brl) + amount
    if rationale:
        position.rationale = rationale
    cat_row.cash = Decimal(cat_row.cash) - amount
    db.flush()
    return position, amount


def settle_pending_positions(db: Session, wallet: AiWallet) -> int:
    """Complete deferred buys whose price has since arrived.

    Cheap no-op when nothing is pending. Called from the wallet detail read
    and the snapshot job, so the ordinary quote-refresh cycle turns
    reservations into shares within the day. A reservation that still cannot
    buy one tradable unit at the discovered price is refunded to the
    category's cash — visibly, through a ``position.settled`` event.
    """
    rows = db.scalars(
        select(AiWalletPosition).where(
            AiWalletPosition.wallet_id == wallet.id, AiWalletPosition.pending_brl > 0
        )
    ).all()
    if not rows:
        return 0
    fx = load_table(db)
    cat_rows = {
        row.category: row
        for row in db.scalars(
            select(AiWalletCategory).where(AiWalletCategory.wallet_id == wallet.id)
        ).all()
    }
    changed = 0
    for position in rows:
        cat_row = cat_rows.get(position.category)
        if cat_row is None:
            continue
        pending = Decimal(position.pending_brl)

        def refund(reason: str) -> None:
            position.pending_brl = ZERO
            cat_row.cash = Decimal(cat_row.cash) + pending
            if Decimal(position.quantity) <= ZERO:
                db.delete(position)
            log_event(
                db,
                wallet.id,
                "position.settled",
                category=position.category,
                detail={"ticker": position.ticker, "refunded_brl": pending, "reason": reason},
            )

        asset = db.get(Asset, position.asset_id) if position.asset_id else None
        if asset is None:
            refund("ativo indisponível")
            changed += 1
            continue
        currency = (asset.currency or "BRL").upper()
        price = _trade_price(db, asset)
        price_brl = _price_brl(price, currency, fx) if price is not None else None
        if price_brl is None or price_brl <= ZERO:
            continue  # still waiting for a quote / FX
        step = quantity_step(position.category, currency)
        quantity = (pending / price_brl).quantize(step, ROUND_DOWN)
        if quantity <= ZERO:
            refund("valor reservado abaixo de uma unidade")
            changed += 1
            continue
        cost = (quantity * price_brl).quantize(CENT, ROUND_DOWN)
        rate = (price_brl / price) if currency != "BRL" else None
        _apply_buy_to_position(position, quantity, price, rate, cost)
        remainder = pending - cost
        position.pending_brl = ZERO
        cat_row.cash = Decimal(cat_row.cash) + remainder
        log_event(
            db,
            wallet.id,
            "position.settled",
            category=position.category,
            detail={
                "ticker": position.ticker,
                "quantity": quantity,
                "price": price,
                "amount_brl": cost,
                "returned_to_cash": remainder,
            },
        )
        changed += 1
    if changed:
        db.flush()
        snapshot_wallet(db, wallet)
    return changed


def open_fixed_income(
    db: Session,
    cat_row: AiWalletCategory,
    item: dict,
    amount_brl: Decimal,
    start: date,
) -> AiWalletPosition | None:
    """A synthetic renda fixa paper: the principal accrues from ``start``."""
    index_code = str(item.get("index_code") or "CDI").upper()
    if index_code not in FI_INDEX_CODES:
        return None
    principal = min(Decimal(amount_brl), Decimal(cat_row.cash)).quantize(CENT, ROUND_DOWN)
    if principal <= ZERO:
        return None
    position = AiWalletPosition(
        wallet_id=cat_row.wallet_id,
        category=cat_row.category,
        asset_id=None,
        ticker=str(item.get("name") or "Renda fixa")[:40],
        name=str(item.get("name") or "Renda fixa")[:255],
        currency="BRL",
        quantity=ZERO,
        avg_price=ZERO,
        cost_brl=principal,
        is_fixed_income=True,
        fi_index_code=index_code,
        fi_percent_of_index=_decimal_or_none(item.get("percent_of_index")),
        fi_spread_annual=_decimal_or_none(item.get("spread_annual")),
        fi_fixed_rate_annual=_decimal_or_none(item.get("fixed_rate_annual")),
        fi_start_date=start,
        rationale=item.get("rationale"),
    )
    db.add(position)
    cat_row.cash = Decimal(cat_row.cash) - principal
    db.flush()
    return position


def _decimal_or_none(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _fi_probe(position: AiWalletPosition):
    """Transient terms for the accrual engine — never added to the session."""
    from app.db.models import FixedIncomeTerms

    return FixedIncomeTerms(
        index_code=position.fi_index_code or "CDI",
        percent_of_index=position.fi_percent_of_index
        if position.fi_percent_of_index is not None
        else Decimal(100),
        spread_annual=position.fi_spread_annual or ZERO,
        fixed_rate_annual=position.fi_fixed_rate_annual or ZERO,
    )


def position_value_brl(
    db: Session,
    position: AiWalletPosition,
    fx: FxTable,
    on: date | None = None,
    closes: dict[int, list[tuple[date, Decimal]]] | None = None,
) -> tuple[Decimal, bool]:
    """(market value in BRL, priced) — falls back to cost, never to zero.

    A pending reservation is valued at face: it is cash earmarked for the
    ticker, not a position with a price yet.
    """
    cost = Decimal(position.cost_brl)
    if position.is_fixed_income:
        start = position.fi_start_date or local_today()
        factor, _, _ = accrual_factor(db, _fi_probe(position), start, on)
        return (cost * factor).quantize(CENT), True

    pending = Decimal(position.pending_brl or 0)
    quantity = Decimal(position.quantity)
    if quantity <= ZERO:
        return (pending if pending > ZERO else cost).quantize(CENT), True

    price: Decimal | None = None
    if on is None:
        live = _live_price(db, position.asset_id)
        if live is not None:
            price = live[0]
        else:
            # A missing Quote row does not have to mean "unpriced": yesterday's
            # stored close is a far better mark than frozen cost.
            price = _last_close(db, position.asset_id)
        rate = fx.latest
    else:
        series = (closes or {}).get(position.asset_id or -1) or []
        for day, close in reversed(series):
            if day <= on:
                price = Decimal(close)
                break
        rate = fx.rate_on(on)
    if price is None:
        return (cost + pending).quantize(CENT), False
    if (position.currency or "BRL").upper() != "BRL":
        if rate is None:
            return (cost + pending).quantize(CENT), False
        price = price * Decimal(rate)
    return ((quantity * price) + pending).quantize(CENT), True


def sell_from_position(
    db: Session,
    position: AiWalletPosition,
    amount_brl: Decimal | None,
    fx: FxTable,
) -> tuple[Decimal, Decimal, bool]:
    """Sell up to ``amount_brl`` (None = everything) at the current market value.

    Returns ``(proceeds, released_cost, closed)``. The caller decides which
    cash pool receives the proceeds — that is what makes rebalance a transfer.
    An unpriced position sells at cost: honest, and never a fabricated loss.
    A pending reservation is drained first — it is cash in costume, price-free.
    """
    value, _ = position_value_brl(db, position, fx)
    if value <= ZERO:
        value = Decimal(position.cost_brl)
    wanted = value if amount_brl is None else min(Decimal(amount_brl), value)
    wanted = wanted.quantize(CENT, ROUND_DOWN)
    if wanted <= ZERO:
        return ZERO, ZERO, False

    cost = Decimal(position.cost_brl)
    if position.is_fixed_income:
        if wanted >= value:
            db.delete(position)
            db.flush()
            return value, cost, True
        released = (cost * wanted / value).quantize(CENT)
        position.cost_brl = cost - released
        db.flush()
        return wanted, released, False

    pending = Decimal(position.pending_brl or 0)
    proceeds = ZERO
    released = ZERO
    if pending > ZERO and wanted > ZERO:
        take = min(wanted, pending)
        position.pending_brl = pending - take
        proceeds += take
        wanted -= take

    quantity = Decimal(position.quantity)
    if wanted > ZERO and quantity > ZERO:
        share_value = value - pending  # what the shares alone are worth
        unit_brl = share_value / quantity
        if unit_brl > ZERO:
            step = quantity_step(position.category, position.currency)
            qty_sold = (wanted / unit_brl).quantize(step, ROUND_DOWN)
            if qty_sold >= quantity or (quantity - qty_sold) * unit_brl < ONE:
                # A remainder below R$1 is dust, not a position.
                proceeds += share_value
                released = cost
                position.quantity = ZERO
                position.cost_brl = ZERO
            elif qty_sold > ZERO:
                proceeds += (qty_sold * unit_brl).quantize(CENT, ROUND_DOWN)
                released = (cost * qty_sold / quantity).quantize(CENT)
                position.quantity = quantity - qty_sold
                position.cost_brl = cost - released

    if proceeds <= ZERO:
        return ZERO, ZERO, False
    closed = Decimal(position.quantity) <= ZERO and Decimal(position.pending_brl) <= ZERO
    if closed:
        db.delete(position)
    db.flush()
    return proceeds.quantize(CENT), released, closed


# ---------------------------------------------------------------------------
# Valuation


def _trim(value) -> str:
    """A Decimal without trailing zeros: 110.0000 → "110", 6.50 → "6.5"."""
    return format(Decimal(value).normalize(), "f")


def fi_label(position: AiWalletPosition) -> str | None:
    if not position.is_fixed_income:
        return None
    code = (position.fi_index_code or "CDI").upper()
    if code == "PRE":
        return f"Pré {_trim(position.fi_fixed_rate_annual or 0)}% a.a."
    parts = [code]
    if position.fi_percent_of_index is not None and Decimal(position.fi_percent_of_index) != HUNDRED:
        parts[0] = f"{_trim(position.fi_percent_of_index)}% {code}"
    if position.fi_spread_annual and Decimal(position.fi_spread_annual) != ZERO:
        parts.append(f"+ {_trim(position.fi_spread_annual)}% a.a.")
    return " ".join(parts)


def value_wallet(db: Session, wallet: AiWallet, on: date | None = None) -> dict:
    """Everything the wallet is worth right now (or on a past date).

    Positions without a price (quote missing, asset deleted) are valued at
    cost and listed under ``unpriced`` — the repo-wide rule: cost, never zero,
    never a fake loss.
    """
    fx = load_table(db)
    cat_rows = db.scalars(
        select(AiWalletCategory).where(AiWalletCategory.wallet_id == wallet.id)
    ).all()
    positions = db.scalars(
        select(AiWalletPosition)
        .where(AiWalletPosition.wallet_id == wallet.id)
        .order_by(AiWalletPosition.cost_brl.desc())
    ).all()

    closes: dict[int, list[tuple[date, Decimal]]] = {}
    if on is not None:
        asset_ids = [p.asset_id for p in positions if p.asset_id is not None]
        if asset_ids:
            rows = db.execute(
                select(PriceHistory.asset_id, PriceHistory.date, PriceHistory.close)
                .where(PriceHistory.asset_id.in_(asset_ids), PriceHistory.date <= on)
                .order_by(PriceHistory.date)
            ).all()
            for asset_id, day, close in rows:
                closes.setdefault(asset_id, []).append((day, close))

    unpriced: list[str] = []
    by_category: dict[str, dict] = {}
    for row in cat_rows:
        by_category[row.category] = {
            "category": row.category,
            "label": CATEGORIES.get(row.category, {}).get("label", row.category),
            "budget": Decimal(row.budget),
            "cash": Decimal(row.cash),
            "generated_at": row.generated_at,
            "thesis": row.thesis,
            "positions": [],
            "value": Decimal(row.cash),
        }

    for position in positions:
        value, priced = position_value_brl(db, position, fx, on=on, closes=closes)
        if not priced:
            unpriced.append(position.ticker)
        pending = Decimal(position.pending_brl or 0)
        # A reservation is invested capital too — hiding it would read as
        # money vanishing between the budget and the table.
        invested_here = Decimal(position.cost_brl) + pending
        pnl = value - invested_here
        block = by_category.get(position.category)
        if block is None:  # orphan safety: a position without its category row
            continue
        block["value"] += value
        block["positions"].append(
            {
                "id": position.id,
                "ticker": position.ticker,
                "name": position.name,
                "category": position.category,
                "currency": position.currency,
                "quantity": Decimal(position.quantity),
                "avg_price": Decimal(position.avg_price),
                "avg_fx": Decimal(position.avg_fx) if position.avg_fx is not None else None,
                "cost_brl": invested_here,
                "pending_brl": pending,
                "market_value_brl": value,
                "pnl_brl": pnl,
                "pnl_pct": (pnl / invested_here * HUNDRED) if invested_here > ZERO else ZERO,
                "priced": priced,
                "is_fixed_income": position.is_fixed_income,
                "fi_label": fi_label(position),
                "rationale": position.rationale,
            }
        )

    for block in by_category.values():
        total = block["value"]
        for item in block["positions"]:
            item["weight_pct"] = (
                (item["market_value_brl"] / total * HUNDRED) if total > ZERO else ZERO
            )

    value = sum((block["value"] for block in by_category.values()), ZERO)
    invested = sum((block["budget"] for block in by_category.values()), ZERO)
    cash = sum((block["cash"] for block in by_category.values()), ZERO)
    return {
        "value": value.quantize(CENT),
        "invested": invested.quantize(CENT),
        "cash": cash.quantize(CENT),
        "return_pct": ((value / invested - ONE) * HUNDRED) if invested > ZERO else None,
        "unpriced": unpriced,
        "categories": by_category,
    }


# ---------------------------------------------------------------------------
# Snapshots (daily value + chained time-weighted return)


def snapshot_wallet(db: Session, wallet: AiWallet, on: date | None = None) -> None:
    """Upsert the wallet's snapshot for ``on`` and chain the TWR factor.

    Budget activation enters value and flow at the same instant, so the factor
    — the competition's scoreboard — never jumps on a generation.
    """
    on = on or local_today()
    valuation = value_wallet(db, wallet, on=None if on == local_today() else on)
    if not valuation["categories"]:
        return  # nothing generated yet: no capital, nothing to score
    value, invested, cash = valuation["value"], valuation["invested"], valuation["cash"]

    previous = db.scalars(
        select(AiWalletSnapshot)
        .where(AiWalletSnapshot.wallet_id == wallet.id, AiWalletSnapshot.date < on)
        .order_by(AiWalletSnapshot.date.desc())
        .limit(1)
    ).first()
    prev_factor = Decimal(previous.return_factor) if previous else ONE
    flow = invested - (Decimal(previous.invested) if previous else ZERO)
    base = (Decimal(previous.value) if previous else ZERO) + flow
    factor = (prev_factor * value / base) if base > ZERO else prev_factor
    factor = factor.quantize(Decimal("1.0000000000000000"))

    categories = {
        code: {"value": block["value"], "cash": block["cash"]}
        for code, block in valuation["categories"].items()
    }
    db.execute(
        dialect_insert(db)(AiWalletSnapshot)
        .values(
            wallet_id=wallet.id,
            date=on,
            value=value,
            invested=invested,
            cash=cash,
            return_factor=factor,
            categories=_jsonable(categories),
        )
        .on_conflict_do_update(
            index_elements=[AiWalletSnapshot.wallet_id, AiWalletSnapshot.date],
            set_={
                "value": value,
                "invested": invested,
                "cash": cash,
                "return_factor": factor,
                "categories": _jsonable(categories),
            },
        )
    )


def snapshot_ai_wallets(db: Session, on: date | None = None) -> dict:
    """The scheduled entry point: one snapshot per wallet, one commit."""
    wallets = db.scalars(select(AiWallet)).all()
    settled = 0
    for wallet in wallets:
        # Deferred buys settle on the daily pass too, not only on page views.
        settled += settle_pending_positions(db, wallet)
        snapshot_wallet(db, wallet, on=on)
    db.commit()
    return {"wallets": len(wallets), "settled": settled, "date": (on or local_today()).isoformat()}


# ---------------------------------------------------------------------------
# Prompt context builders (pure data — no model calls here)


def _round2(value: Decimal | None) -> float | None:
    return None if value is None else round(float(value), 2)


def wallet_summary(db: Session, wallet: AiWallet) -> dict:
    """The whole wallet, compact, for the model's context. Never user data."""
    valuation = value_wallet(db, wallet)
    categories = {}
    for code, block in valuation["categories"].items():
        categories[code] = {
            "caixa_brl": _round2(block["cash"]),
            "valor_brl": _round2(block["value"]),
            "posicoes": [
                {
                    "ticker": item["ticker"],
                    "peso_pct": _round2(item["weight_pct"]),
                    "retorno_pct": _round2(item["pnl_pct"]),
                }
                for item in block["positions"]
            ],
        }
    return {
        "valor_total_brl": _round2(valuation["value"]),
        "investido_brl": _round2(valuation["invested"]),
        "por_categoria": categories,
    }


def macro_context(db: Session) -> dict:
    """Today's rate environment from local data — search-less models get it too."""
    today = local_today()
    out: dict = {"data": today.isoformat()}

    for code, key in (("CDI", "cdi_ano_pct"), ("SELIC", "selic_ano_pct")):
        series = load_series(db, code)
        if series:
            daily = Decimal(series[-1][1]) / HUNDRED
            out[key] = _round2(((ONE + daily) ** 252 - ONE) * HUNDRED)

    ipca = load_series(db, "IPCA")
    if ipca:
        factor = ONE
        for _, value in ipca[-12:]:
            factor *= ONE + Decimal(value) / HUNDRED
        out["ipca_12m_pct"] = _round2((factor - ONE) * HUNDRED)

    dollar = load_table(db).latest
    if dollar is not None:
        out["dolar_brl"] = _round2(Decimal(dollar))

    ibov = db.execute(
        select(IndexRate.date, IndexRate.value)
        .where(IndexRate.code == "IBOV")
        .order_by(IndexRate.date)
    ).all()
    if ibov:
        last_day, last_value = ibov[-1]
        out["ibov_pontos"] = _round2(Decimal(last_value))
        year_ago = today - timedelta(days=365)
        base = next((value for day, value in reversed(ibov) if day <= year_ago), None)
        if base and Decimal(base) > ZERO:
            out["ibov_12m_pct"] = _round2((Decimal(last_value) / Decimal(base) - ONE) * HUNDRED)
    return out


#: Fundamentals fields worth the model's attention, renamed to pt-BR.
_FUNDAMENTAL_KEYS = {
    "sector": "setor",
    "market_cap": "valor_de_mercado",
    "pe_trailing": "p_l",
    "price_to_book": "p_vp",
    "return_on_equity": "roe_pct",
    "profit_margin": "margem_liquida_pct",
    "revenue_growth": "crescimento_receita_pct",
    "earnings_growth": "crescimento_lucro_pct",
    "debt_to_equity": "divida_sobre_patrimonio",
    "dividend_yield": "dividend_yield_pct",
    "payout_ratio": "payout_pct",
    "target_mean_price": "preco_alvo_medio",
    "recommendation": "recomendacao_analistas",
}

_FUNDAMENTALS_TTL = timedelta(hours=12)


def _fundamentals_for(db: Session, asset: Asset) -> dict:
    """Cached (12 h) fundamentals subset; stored so the rest of the app benefits."""
    if asset.kind in coins.CRYPTO_KINDS:
        return {}
    now = datetime.now(UTC)
    cached = db.get(AssetFundamentals, asset.id)
    data = None
    if (
        cached is not None
        and cached.fetched_at is not None
        and cached.fetched_at.replace(tzinfo=cached.fetched_at.tzinfo or UTC)
        > now - _FUNDAMENTALS_TTL
    ):
        data = cached.data
    if data is None:
        from app.market import fundamentals  # local: heavy module, cycle-safe

        try:
            data = fundamentals.fetch(
                resolve_market_symbol(asset),
                asset.ticker,
                (asset.currency or "BRL").upper() == "BRL",
            )
        except Exception:  # noqa: BLE001 — a data gap must not sink the generation
            logger.exception("ai wallet: fundamentals failed for %s", asset.ticker)
            return {}
        if data.get("has_data"):
            db.merge(
                AssetFundamentals(
                    asset_id=asset.id, data=data, source="yahoo", fetched_at=now
                )
            )
            db.commit()
    subset = {}
    for source_key, target_key in _FUNDAMENTAL_KEYS.items():
        value = (data or {}).get(source_key)
        if value is not None:
            subset[target_key] = value
    dates = (data or {}).get("earnings_dates") or []
    if dates:
        subset["proximo_resultado"] = dates[0]
    return subset


def candidate_context(
    db: Session, category: str, ticker: str, created: list[int] | None = None
) -> tuple[dict, bool]:
    """Verified reality for one AI-proposed candidate.

    Returns ``(context, ok)``. ``ok=False`` means the ticker failed the market
    gate and must not survive to the final allocation. ``created`` collects
    minted Asset ids for the caller's post-run cleanup.
    """
    asset = get_or_create_wallet_asset(db, category, ticker, created)
    if asset is None:
        return {"ticker": (ticker or "").strip().upper(), "erro": "não encontrado no mercado ou fora da categoria"}, False

    context: dict = {"ticker": asset.ticker, "nome": asset.name, "moeda": asset.currency}
    price = _trade_price(db, asset)
    if price is None:
        # The asset resolved — only the quote is missing (usually a transient
        # rate limit). It stays eligible: the buy defers until priced.
        context["preco_atual"] = None
        context["observacao"] = "sem cotação neste momento — a compra será concluída quando houver preço"
    else:
        context["preco_atual"] = _round2(price)

    today = local_today()
    rows = db.execute(
        select(PriceHistory.date, PriceHistory.close)
        .where(PriceHistory.asset_id == asset.id, PriceHistory.date >= today - timedelta(days=400))
        .order_by(PriceHistory.date)
    ).all()
    window = [(day, Decimal(close)) for day, close in rows if day >= today - timedelta(days=365)]
    if window:
        base = window[0][1]
        if base > ZERO:
            context["variacao_12m_pct"] = _round2((window[-1][1] / base - ONE) * HUNDRED)
        closes = [close for _, close in window]
        context["maxima_52s"] = _round2(max(closes))
        context["minima_52s"] = _round2(min(closes))

    fundamentals = _fundamentals_for(db, asset)
    if fundamentals:
        context.update(fundamentals)
    elif asset.kind not in coins.CRYPTO_KINDS:
        # Missing data must read as missing, not as "nothing remarkable".
        context["observacao_fundamentos"] = "fundamentos indisponíveis neste momento"
    return context, True


def cleanup_unused_assets(db: Session, asset_ids: list[int]) -> int:
    """Delete Asset rows from ``asset_ids`` that nothing references anymore.

    This is what keeps the scheduled quote refresh scoped to assets someone
    actually holds or watches: verification scratch and orphaned wallet
    tickers would otherwise accumulate in the refresh set forever. Each id is
    re-checked against every live claim — wallet positions (any wallet), real
    transactions, the watchlist, still-pending suggestions — and survivors are
    left alone. Deleting is safe: a watch-only row is recreated by one visit
    to its asset page. The caller commits.
    """
    removed = 0
    for asset_id in set(asset_ids):
        asset = db.get(Asset, asset_id)
        if asset is None:
            continue
        referenced = (
            db.scalar(
                select(AiWalletPosition.id).where(AiWalletPosition.asset_id == asset_id).limit(1)
            )
            or db.scalar(select(Transaction.id).where(Transaction.asset_id == asset_id).limit(1))
            or db.scalar(
                select(WatchlistItem.id).where(WatchlistItem.ticker == asset.ticker).limit(1)
            )
            or db.scalar(
                select(AiWalletSuggestion.id)
                .where(
                    AiWalletSuggestion.status == "pending",
                    or_(
                        AiWalletSuggestion.ticker == asset.ticker,
                        AiWalletSuggestion.to_ticker == asset.ticker,
                    ),
                )
                .limit(1)
            )
        )
        if referenced:
            continue
        db.add(
            AuditLog(
                action="asset.unwatch",
                detail={"ticker": asset.ticker, "reason": "candidato IA não utilizado"},
            )
        )
        db.delete(asset)
        removed += 1
    return removed


def delete_wallet(db: Session, wallet: AiWallet) -> None:
    """Remove a wallet and everything it owns.

    Children are deleted explicitly rather than trusting ON DELETE CASCADE:
    SQLite only honours it behind a pragma, and a half-deleted wallet is worse
    than a slow delete. The wallet's own event log dies here, so the deletion
    itself is recorded in the global audit trail. Watch-only Assets survive
    only while something else references them — tickers exclusive to this
    wallet leave the quote-refresh set together with it.
    """
    asset_ids = [
        asset_id
        for (asset_id,) in db.execute(
            select(AiWalletPosition.asset_id).where(
                AiWalletPosition.wallet_id == wallet.id,
                AiWalletPosition.asset_id.is_not(None),
            )
        ).all()
    ]
    for model in (
        AiWalletSnapshot,
        AiWalletEvent,
        AiWalletSuggestion,
        AiWalletPosition,
        AiWalletCategory,
    ):
        db.execute(sa_delete(model).where(model.wallet_id == wallet.id))
    db.add(
        AuditLog(
            action="ai_wallet.deleted",
            detail={"name": wallet.name, "provider": wallet.provider, "model": wallet.model},
        )
    )
    db.delete(wallet)
    cleanup_unused_assets(db, asset_ids)
    db.commit()


def suggestion_target_error(db: Session, wallet_id: int, category: str, item: dict) -> str | None:
    """Why a suggestion cannot be stored as acceptable, or None when it can.

    The rebalance target rule lives here: an existing position anywhere, a new
    asset in the *same* category, or an already-activated category's cash — a
    brand-new asset in a different category is that category's own decision.
    """
    action = item["action"]
    if action in ("increase", "reduce", "sell_all", "rebalance"):
        if _find_position(db, wallet_id, category, item.get("ticker") or "") is None:
            return "posição não existe nesta categoria"
    if action == "rebalance":
        to_category = item.get("to_category") or category
        if to_category != category:
            row = db.scalar(
                select(AiWalletCategory).where(
                    AiWalletCategory.wallet_id == wallet_id,
                    AiWalletCategory.category == to_category,
                )
            )
            if row is None:
                return "categoria de destino ainda não foi gerada"
            to_ticker = item.get("to_ticker")
            if to_ticker and _find_position(db, wallet_id, to_category, to_ticker) is None:
                return (
                    "ativo novo em outra categoria — essa decisão cabe às sugestões da própria categoria"
                )
    return None


# ---------------------------------------------------------------------------
# Model-output normalisation (pure — the schema gate between AI and money)

SUGGESTION_ACTIONS = {"buy_new", "increase", "reduce", "sell_all", "rebalance"}


def normalize_candidates(data: dict | None, limit: int = 15) -> list[str]:
    """Phase-A output → a clean ticker list (order kept, dupes dropped)."""
    items = (data or {}).get("candidates")
    if not isinstance(items, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        ticker = str((raw or {}).get("ticker") or "").strip().upper() if isinstance(raw, dict) else ""
        if ticker and ticker not in seen:
            seen.add(ticker)
            out.append(ticker)
    return out[:limit]


def normalize_generation(data: dict | None, category: str, max_items: int = 8) -> list[dict]:
    """Phase-B output → validated allocation items; Σ pct scaled down to ≤ 100.

    A short-of-100 total simply leaves cash — the model is allowed to be
    conservative, not to overspend.
    """
    from app.services.ai_research import decode_escapes

    items = (data or {}).get("positions")
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        pct = _decimal_or_none(raw.get("allocation_pct"))
        if pct is None or pct <= ZERO:
            continue
        pct = min(pct, HUNDRED)
        rationale = decode_escapes(str(raw.get("rationale") or "").strip()) or None
        if category == "RENDA_FIXA":
            name = str(raw.get("name") or "").strip()
            index_code = str(raw.get("index_code") or "").strip().upper()
            if not name or index_code not in FI_INDEX_CODES:
                continue
            out.append(
                {
                    "name": name,
                    "index_code": index_code,
                    "percent_of_index": _decimal_or_none(raw.get("percent_of_index")),
                    "spread_annual": _decimal_or_none(raw.get("spread_annual")),
                    "fixed_rate_annual": _decimal_or_none(raw.get("fixed_rate_annual")),
                    "allocation_pct": pct,
                    "rationale": rationale,
                }
            )
        else:
            ticker = str(raw.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            out.append(
                {
                    "ticker": ticker,
                    "name": str(raw.get("name") or "").strip()[:255],
                    "allocation_pct": pct,
                    "rationale": rationale,
                }
            )
    out = out[:max_items]
    total = sum((item["allocation_pct"] for item in out), ZERO)
    if total > HUNDRED:
        for item in out:
            item["allocation_pct"] = (item["allocation_pct"] * HUNDRED / total).quantize(CENT)
    return out


def normalize_suggestions(data: dict | None, category: str, limit: int = 10) -> list[dict]:
    """Suggestion output → validated items (targets checked later, per item)."""
    from app.services.ai_research import decode_escapes

    items = (data or {}).get("suggestions")
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action") or "").strip().lower()
        if action not in SUGGESTION_ACTIONS:
            continue
        ticker = str(raw.get("ticker") or "").strip().upper() or None
        amount = _decimal_or_none(raw.get("amount_brl"))
        if action != "sell_all" and (amount is None or amount <= ZERO):
            continue
        if action != "buy_new" and not ticker:
            continue
        to_category = str(raw.get("to_category") or "").strip().upper() or None
        if to_category is not None and to_category not in CATEGORIES:
            to_category = None
        item = {
            "action": action,
            "ticker": ticker,
            "name": str(raw.get("name") or "").strip()[:255],
            "amount_brl": amount.quantize(CENT) if amount is not None else None,
            "to_ticker": str(raw.get("to_ticker") or "").strip().upper() or None,
            "to_category": to_category,
            "rationale": decode_escapes(str(raw.get("rationale") or "").strip()) or None,
            "raw": raw,
        }
        if category == "RENDA_FIXA" and action == "buy_new":
            index_code = str(raw.get("index_code") or "").strip().upper()
            if index_code not in FI_INDEX_CODES:
                continue
        out.append(item)
    return out[:limit]


# ---------------------------------------------------------------------------
# Applying a generation


def apply_generation(
    db: Session,
    wallet: AiWallet,
    category: str,
    items: list[dict],
    *,
    used_search: bool,
    skipped: list[dict] | None = None,
    strategy: str | None = None,
) -> dict:
    """Turn the model's final allocation into positions, atomically.

    Raises ``sqlalchemy.exc.IntegrityError`` when the category was generated
    concurrently — the unique constraint is the lock. Items must already be
    schema-validated; tickers that fail the market gate here are skipped and
    reported, with their share left as cash.
    """
    today = local_today()
    cat_row = AiWalletCategory(
        wallet_id=wallet.id,
        category=category,
        budget=DEFAULT_BUDGET,
        cash=DEFAULT_BUDGET,
        generated_at=datetime.now(UTC),
        thesis=strategy,
    )
    db.add(cat_row)
    db.flush()

    fx = load_table(db)
    skipped = list(skipped or [])
    bought: list[dict] = []
    deferred: list[dict] = []
    for item in items:
        pct = Decimal(str(item.get("allocation_pct") or 0))
        if pct <= ZERO:
            continue
        amount = (DEFAULT_BUDGET * pct / HUNDRED).quantize(CENT, ROUND_DOWN)

        if category == "RENDA_FIXA":
            position = open_fixed_income(db, cat_row, item, amount, today)
            if position is None:
                skipped.append({"ticker": item.get("name"), "reason": "especificação inválida"})
                continue
            bought.append(
                {"ticker": position.ticker, "amount_brl": Decimal(position.cost_brl), "fi": fi_label(position)}
            )
            continue

        asset = get_or_create_wallet_asset(db, category, str(item.get("ticker") or ""))
        if asset is None:
            skipped.append({"ticker": item.get("ticker"), "reason": "não encontrado no mercado ou fora da categoria"})
            continue
        result = buy_into_category(db, cat_row, asset, amount, fx, item.get("rationale"))
        if result is None:
            # Resolvable but priceless right now (rate limit, FX gap): reserve
            # the money instead of quietly shrinking the allocation.
            reserved = defer_buy(db, cat_row, asset, amount, item.get("rationale"))
            if reserved is None:
                skipped.append({"ticker": asset.ticker, "reason": "sem caixa para reservar"})
                continue
            position, amount_reserved = reserved
            deferred.append({"ticker": position.ticker, "amount_brl": amount_reserved})
            bought.append({"ticker": position.ticker, "amount_brl": amount_reserved, "pending": True})
            continue
        position, cost = result
        bought.append(
            {
                "ticker": position.ticker,
                "quantity": Decimal(position.quantity),
                "price": Decimal(position.avg_price),
                "amount_brl": cost,
            }
        )

    log_event(
        db,
        wallet.id,
        "category.generated",
        category=category,
        provider=wallet.provider,
        model=wallet.model,
        detail={
            "positions": bought,
            "skipped": skipped,
            "deferred": deferred,
            "cash_after": Decimal(cat_row.cash),
            "used_search": used_search,
            "strategy": strategy,
        },
    )
    snapshot_wallet(db, wallet)
    db.commit()
    return {
        "category": category,
        "positions": len(bought),
        "skipped": skipped,
        "pending": deferred,
        "cash": Decimal(cat_row.cash),
        "used_search": used_search,
    }


# ---------------------------------------------------------------------------
# Applying an accepted suggestion


class SuggestionError(Exception):
    """A suggestion that cannot be applied right now — message is pt-BR."""


def _category_row(db: Session, wallet_id: int, category: str) -> AiWalletCategory:
    row = db.scalar(
        select(AiWalletCategory).where(
            AiWalletCategory.wallet_id == wallet_id, AiWalletCategory.category == category
        )
    )
    if row is None:
        raise SuggestionError("Categoria ainda não gerada nesta carteira.")
    return row


def _suggestion_position(db: Session, suggestion) -> AiWalletPosition:
    position = None
    if suggestion.position_id is not None:
        position = db.get(AiWalletPosition, suggestion.position_id)
    if position is None and suggestion.ticker:
        position = _find_position(db, suggestion.wallet_id, suggestion.category, suggestion.ticker)
    if position is None:
        raise SuggestionError("A posição desta sugestão não existe mais na carteira.")
    return position


def apply_suggestion(db: Session, wallet: AiWallet, suggestion) -> dict:
    """Apply one accepted suggestion at current market prices.

    Returns a detail dict describing what actually happened (amounts are
    capped by cash/position value, so they may differ from the proposal).
    Raises :class:`SuggestionError` with a user-readable reason on refusal —
    the caller keeps the suggestion pending in that case.
    """
    fx = load_table(db)
    cat_row = _category_row(db, suggestion.wallet_id, suggestion.category)
    action = suggestion.action
    detail: dict

    if action == "buy_new":
        if suggestion.category == "RENDA_FIXA":
            payload = suggestion.payload or {}
            position = open_fixed_income(
                db, cat_row, payload, Decimal(suggestion.amount_brl or 0), local_today()
            )
            if position is None:
                raise SuggestionError("Caixa insuficiente ou especificação inválida.")
            detail = {"ticker": position.ticker, "amount_brl": Decimal(position.cost_brl)}
            log_action = "position.buy"
        else:
            asset = get_or_create_wallet_asset(db, suggestion.category, suggestion.ticker or "")
            if asset is None:
                raise SuggestionError("Ativo não encontrado no mercado.")
            result = buy_into_category(
                db, cat_row, asset, Decimal(suggestion.amount_brl or 0), fx, suggestion.rationale
            )
            if result is None:
                raise SuggestionError("Sem caixa, cotação ou câmbio para executar a compra.")
            position, cost = result
            detail = {"ticker": position.ticker, "amount_brl": cost}
            log_action = "position.buy"

    elif action == "increase":
        position = _suggestion_position(db, suggestion)
        if position.is_fixed_income:
            amount = min(Decimal(suggestion.amount_brl or 0), Decimal(cat_row.cash)).quantize(
                CENT, ROUND_DOWN
            )
            if amount <= ZERO:
                raise SuggestionError("Caixa insuficiente na categoria.")
            position.cost_brl = Decimal(position.cost_brl) + amount
            cat_row.cash = Decimal(cat_row.cash) - amount
            db.flush()
            detail = {"ticker": position.ticker, "amount_brl": amount}
        else:
            asset = db.get(Asset, position.asset_id) if position.asset_id else None
            if asset is None:
                raise SuggestionError("O ativo desta posição não existe mais.")
            result = buy_into_category(
                db, cat_row, asset, Decimal(suggestion.amount_brl or 0), fx, None
            )
            if result is None:
                raise SuggestionError("Sem caixa, cotação ou câmbio para executar a compra.")
            _, cost = result
            detail = {"ticker": position.ticker, "amount_brl": cost}
        log_action = "position.increase"

    elif action in ("reduce", "sell_all"):
        position = _suggestion_position(db, suggestion)
        amount = None if action == "sell_all" else Decimal(suggestion.amount_brl or 0)
        proceeds, _, closed = sell_from_position(db, position, amount, fx)
        if proceeds <= ZERO:
            raise SuggestionError("Não foi possível precificar a venda.")
        cat_row.cash = Decimal(cat_row.cash) + proceeds
        db.flush()
        detail = {"ticker": suggestion.ticker, "amount_brl": proceeds, "closed": closed}
        log_action = "position.sell" if closed else "position.reduce"

    elif action == "rebalance":
        position = _suggestion_position(db, suggestion)
        target_category = suggestion.to_category or suggestion.category
        target_row = (
            cat_row
            if target_category == suggestion.category
            else _category_row(db, suggestion.wallet_id, target_category)
        )
        # Resolve the target BEFORE selling: get_or_create may commit a new
        # Asset row, and a commit after the sell would persist a half-applied
        # rebalance if the buy leg then failed.
        target_asset = None
        if suggestion.to_ticker:
            target_asset = get_or_create_wallet_asset(db, target_category, suggestion.to_ticker)
            if target_asset is None:
                raise SuggestionError("Ativo de destino não encontrado no mercado.")
        proceeds, _, closed = sell_from_position(
            db, position, Decimal(suggestion.amount_brl or 0) or None, fx
        )
        if proceeds <= ZERO:
            raise SuggestionError("Não foi possível precificar a venda de origem.")
        target_row.cash = Decimal(target_row.cash) + proceeds
        db.flush()
        detail = {
            "from_ticker": suggestion.ticker,
            "from_category": suggestion.category,
            "to_category": target_category,
            "amount_brl": proceeds,
            "closed_source": closed,
        }
        if target_asset is not None:
            result = buy_into_category(db, target_row, target_asset, proceeds, fx, suggestion.rationale)
            if result is None:
                raise SuggestionError("Sem cotação ou câmbio para comprar o destino.")
            target_position, cost = result
            detail["to_ticker"] = target_position.ticker
            detail["bought_brl"] = cost
        log_action = "position.rebalance"

    else:
        raise SuggestionError(f"Ação desconhecida: {action}")

    suggestion.status = "accepted"
    suggestion.resolved_at = datetime.now(UTC)
    suggestion.detail = json.dumps(_jsonable(detail), ensure_ascii=False)
    log_event(
        db,
        wallet.id,
        log_action,
        category=suggestion.category,
        provider=suggestion.provider,
        model=suggestion.model,
        detail=detail,
    )
    log_event(
        db,
        wallet.id,
        "suggestion.accepted",
        category=suggestion.category,
        provider=suggestion.provider,
        model=suggestion.model,
        detail={"suggestion_id": suggestion.id, "action": action, **detail},
    )
    snapshot_wallet(db, wallet)
    db.commit()
    return detail
