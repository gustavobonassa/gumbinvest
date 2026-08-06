"""Pricing support for coins held on an exchange.

Quotes and daily closes need nothing special: a coin is a dollar-denominated
asset with a ``BTC-USD``-shaped market symbol, so :mod:`app.market.service`
already fetches it alongside every US holding.

What does need something is the handful of trades an exchange prices **in a
coin** rather than in money. ``NEARBTC`` says a purchase cost 0,0000999 BTC and
nothing more; there is no rate on file that turns that into reais, so the
movement would sit outside every base-currency total. This module publishes the
missing rate — how many reais one BTC was worth on a given day — into the same
``fx_rates`` table the PTAX series lives in, which is exactly what it is. From
there ``backfill_transaction_fx`` picks the movements up on its next pass, with
no special case anywhere in the portfolio code.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Asset, FxRate, PriceHistory, Quote, Transaction
from app.db.upsert import dialect_insert
from app.importer.crypto import symbols as coins

logger = get_logger(__name__)


def crypto_assets(db: Session) -> list[Asset]:
    return list(db.scalars(select(Asset).where(Asset.kind.in_(coins.CRYPTO_KINDS))).all())


def coin_denominated_currencies(db: Session, base_currency: str = "BRL") -> list[str]:
    """Currencies that movements are booked in but no money series covers.

    In practice this is the short list of coins that appear on the *quote* side
    of a pair — Bitcoin and BNB, on this portfolio's history.
    """
    rows = db.scalars(select(func.distinct(Transaction.currency))).all()
    return sorted(
        currency.upper()
        for currency in rows
        if currency
        and currency.upper() != base_currency.upper()
        and not coins.is_fiat(currency)
    )


#: Marks the rows this module owns, so cleaning up cannot touch a PTAX series.
SOURCE = "crypto-close"

#: Coins worth a headline of their own. Bitcoin is the number the market is read
#: by, so it earns a line next to the dollar — but as a *price*, taken from the
#: live quote, not from the rate series in ``fx_rates``. Those exist to convert
#: a few legacy trades and would vanish the moment nothing is denominated in a
#: coin any more; a price should not blink out because an importer changed its
#: mind about how to read a pair.
HEADLINE_COINS: tuple[str, ...] = ("BTC",)


def headline_prices(db: Session, base_currency: str = "BRL") -> list[dict]:
    """Live price of the headline coins, in the portfolio's own currency.

    Empty when the coin is not held: quotes are only fetched for open positions,
    and a price nobody's portfolio depends on is not worth a line in the UI.
    """
    from app.market.fx import load_table  # local import avoids a cycle

    held = {coins.asset_symbol(asset.ticker): asset for asset in crypto_assets(db)}
    rate = load_table(db, "USD", base_currency).latest
    prices: list[dict] = []
    for symbol in HEADLINE_COINS:
        asset = held.get(symbol)
        if asset is None:
            continue
        quote = db.get(Quote, asset.id)
        if quote is None or quote.price is None:
            continue
        quoted_in = (quote.currency or asset.currency or "USD").upper()
        if quoted_in == base_currency.upper():
            converted = Decimal(quote.price)
        elif rate is None:
            # No rate yet: report the price in the currency it came in rather
            # than converting it at a made-up one.
            converted = None
        else:
            converted = Decimal(quote.price) * rate
        prices.append(
            {
                "symbol": symbol,
                "name": coins.coin_name(symbol),
                "price": quote.price,
                "currency": quoted_in,
                "price_base": converted,
                "base_currency": base_currency.upper(),
                "change_percent": quote.change_percent,
                "fetched_at": quote.fetched_at,
            }
        )
    return prices


def _drop_stale_series(db: Session, currencies: list[str]) -> int:
    """Remove coin series no longer backing any movement.

    Which coins appear on the quote side of a trade changes as the history is
    re-read — a better importer resolves a pair differently — and a series left
    behind is a rate for a currency nothing is denominated in any more. Only
    rows this module wrote are eligible, so a PTAX series can never be caught by
    it.
    """
    stale = select(FxRate.id).where(FxRate.source == SOURCE)
    if currencies:
        stale = stale.where(FxRate.base.notin_(currencies))
    ids = list(db.scalars(stale).all())
    if not ids:
        return 0
    db.execute(delete(FxRate).where(FxRate.id.in_(ids)))
    db.commit()
    logger.info("dropped %s stale coin rate rows", len(ids))
    return len(ids)


def sync_crypto_fx(db: Session, base_currency: str = "BRL") -> dict:
    """Write "one coin was worth N reais on day D" rates for coin-quoted trades.

    The coin's own daily closes are in dollars (that is how it is quoted), so
    each one is carried to reais with the PTAX rate of the same day. Days where
    either side is missing are skipped rather than approximated — a movement
    with no rate stays visibly unconverted, which is the same contract the
    offshore statements already work under.
    """
    from app.market.fx import load_table  # local import avoids a cycle

    currencies = coin_denominated_currencies(db, base_currency)
    removed = _drop_stale_series(db, currencies)
    if not currencies:
        return {"currencies": 0, "points": 0, "removed": removed}

    ptax = load_table(db, "USD", base_currency)
    if ptax.is_empty:
        logger.info("crypto fx: no %s rates yet, nothing to publish", base_currency)
        return {
            "currencies": len(currencies),
            "points": 0,
            "removed": removed,
            "detail": "no PTAX series",
        }

    by_symbol = {coins.asset_symbol(asset.ticker): asset for asset in crypto_assets(db)}
    written = 0
    covered = 0
    for currency in currencies:
        asset = by_symbol.get(currency)
        if asset is None:
            continue
        closes = db.execute(
            select(PriceHistory.date, PriceHistory.close).where(PriceHistory.asset_id == asset.id)
        ).all()
        if not closes:
            continue
        covered += 1
        for day, close in closes:
            rate = ptax.rate_on(day)
            if rate is None or close is None:
                continue
            db.execute(
                dialect_insert(db)(FxRate)
                .values(
                    base=currency,
                    quote=base_currency.upper(),
                    date=day,
                    rate=Decimal(close) * rate,
                    source=SOURCE,
                )
                .on_conflict_do_update(
                    index_elements=[FxRate.base, FxRate.quote, FxRate.date],
                    set_={"rate": Decimal(close) * rate, "source": SOURCE},
                )
            )
            written += 1
    db.commit()
    if written:
        logger.info("crypto fx: %s rates published for %s coins", written, covered)
    return {"currencies": covered, "points": written, "removed": removed}
