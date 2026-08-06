"""Persists quotes and price history fetched from the configured provider."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Asset, PriceHistory, Quote, Transaction
from app.db.upsert import dialect_insert
from app.domain.enums import AssetKind
from app.market.providers import get_provider

logger = get_logger(__name__)

#: Instrument families that no public quote API covers.
UNQUOTABLE_KINDS = {
    AssetKind.FIXED_INCOME.value,
    AssetKind.TREASURY.value,
    AssetKind.FUTURE.value,
    AssetKind.OPTION.value,
    AssetKind.SUBSCRIPTION.value,
    AssetKind.OTHER.value,
}


def resolve_market_symbol(asset: Asset) -> str:
    """The symbol to ask the provider for.

    A B3 ticker needs the ``.SA`` suffix and a US one must not have it — and the
    only reliable signal for which is which is the asset's currency, not the
    shape of the ticker. ``BAC.SA`` is a Brazilian company that has nothing to
    do with Bank of America.
    """
    symbol = (asset.market_symbol or asset.ticker).upper()
    if "." in symbol:
        return symbol
    if (asset.currency or "BRL").upper() == "BRL":
        return f"{symbol}.SA"
    return symbol


def tracked_asset_ids(db: Session, portfolio_id: int | None = None) -> set[int]:
    """Assets someone has asked to keep current, by an explicit act.

    Three such acts, and only three: holding the paper, putting it on the
    watchlist, or an AI wallet taking a position in it. Everything else — a
    ticker opened once from search or from the asset universe — is *browsing*,
    and browsing must not enrol a paper in a job that runs every half hour
    forever. With thousands of universe rows one click away, the old rule
    ("anything without transactions") would have grown the refresh set without
    limit and without anyone choosing it.

    A browsed asset is not abandoned: :func:`refresh_if_stale` brings it up to
    date when its page is opened, which is when anyone is looking.
    """
    from app.db.models import AiWalletPosition, WatchlistItem
    from app.portfolio.service import PortfolioService  # local: avoids a cycle

    if portfolio_id is None:
        portfolio_id = db.scalar(select(Transaction.portfolio_id).limit(1))

    tracked: set[int] = set()
    if portfolio_id is not None:
        tracked |= {
            position.asset_id
            for position in PortfolioService(db, portfolio_id).positions().values()
            if position.is_open
        }
    tracked |= {
        asset_id
        for (asset_id,) in db.execute(
            select(Asset.id).join(WatchlistItem, WatchlistItem.ticker == Asset.ticker)
        ).all()
    }
    tracked |= {
        asset_id
        for (asset_id,) in db.execute(
            select(AiWalletPosition.asset_id).where(AiWalletPosition.asset_id.is_not(None)).distinct()
        ).all()
    }
    return tracked


def quotable_assets(db: Session, portfolio_id: int | None = None, only_held: bool = True) -> list[Asset]:
    """Assets worth asking the provider about.

    Skips manual-priced assets, unquotable families and — by default —
    everything nobody asked to track (see :func:`tracked_asset_ids`).
    """
    stmt = select(Asset).where(Asset.price_manual.is_(False), Asset.kind.notin_(UNQUOTABLE_KINDS))
    assets = list(db.scalars(stmt).all())
    if not only_held:
        return assets

    tracked = tracked_asset_ids(db, portfolio_id)
    return [asset for asset in assets if asset.id in tracked]


#: How stale a browsed asset's quote may be before opening its page refetches.
#: Matches the scheduled cadence, so a tracked and an untracked asset are never
#: more than the same interval out of date when you are actually looking at one.
BROWSE_REFRESH_AFTER = timedelta(minutes=max(settings.price_refresh_minutes, 1))


def refresh_if_stale(db: Session, asset: Asset, portfolio_id: int | None = None) -> bool:
    """Bring one untracked asset up to date, if it is being looked at.

    This is the other half of the rule in :func:`tracked_asset_ids`: a paper
    nobody asked to follow is not refreshed on a schedule, so it is refreshed
    on sight instead. Assets that *are* tracked do nothing here — the scheduled
    job already owns them, and a second fetch per page view would be waste.
    """
    if asset.price_manual or asset.kind in UNQUOTABLE_KINDS:
        return False
    if asset.id in tracked_asset_ids(db, portfolio_id):
        return False
    quote = db.get(Quote, asset.id)
    fresh_since = datetime.now(UTC) - BROWSE_REFRESH_AFTER
    if quote is not None and quote.fetched_at is not None:
        fetched = quote.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=UTC)
        if fetched > fresh_since:
            return False
    try:
        ensure_market_data(db, asset)
    except Exception:  # noqa: BLE001 — a stale price must not 500 the page
        logger.exception("on-demand refresh failed for %s", asset.ticker)
        return False
    return True


def refresh_quotes(db: Session, portfolio_id: int | None = None, force: bool = False) -> dict:
    """Fetch and store the latest price for every held, quotable asset."""
    provider = get_provider()
    if provider.name == "none":
        return {"provider": "none", "updated": 0, "skipped": 0, "detail": "market data disabled"}

    assets = quotable_assets(db, portfolio_id)
    if not force:
        cutoff = datetime.now(UTC) - timedelta(seconds=settings.quote_cache_ttl)
        fresh = {
            q.asset_id
            for q in db.scalars(select(Quote)).all()
            if q.fetched_at and q.fetched_at.replace(tzinfo=q.fetched_at.tzinfo or UTC) > cutoff
        }
        assets = [a for a in assets if a.id not in fresh]

    if not assets:
        return {"provider": provider.name, "updated": 0, "skipped": 0, "detail": "quotes are fresh"}

    symbols = [resolve_market_symbol(a) for a in assets]
    quotes = provider.get_quotes(symbols)
    updated = 0
    now = datetime.now(UTC)

    for asset in assets:
        symbol = resolve_market_symbol(asset)
        data = quotes.get(symbol)
        if data is None:
            continue
        db.merge(
            Quote(
                asset_id=asset.id,
                price=data.price,
                previous_close=data.previous_close,
                change=data.change,
                change_percent=data.change_percent,
                currency=data.currency or asset.currency,
                source=provider.name,
                long_name=data.long_name,
                fetched_at=now,
            )
        )
        # Keep a daily close so the history chart has real data going forward.
        _upsert_price(db, asset.id, now.date(), data.price, provider.name)
        updated += 1

    db.commit()
    logger.info("quotes refreshed via %s: %s/%s", provider.name, updated, len(assets))
    return {
        "provider": provider.name,
        "requested": len(assets),
        "updated": updated,
        "missing": [a.ticker for a in assets if resolve_market_symbol(a) not in quotes],
        "fetched_at": now,
    }


def ensure_market_data(db: Session, asset: Asset) -> None:
    """First quote and full price history for one asset, synchronously.

    A watch-only asset is created the moment its page is first opened; waiting
    for the nightly backfill would leave that page without a price or a chart
    until tomorrow. Two provider calls, a second or two — and a failure leaves
    the asset priceless rather than failing the page.
    """
    provider = get_provider()
    if provider.name == "none" or asset.price_manual or asset.kind in UNQUOTABLE_KINDS:
        return
    symbol = resolve_market_symbol(asset)

    data = provider.get_quotes([symbol]).get(symbol)
    if data is not None:
        db.merge(
            Quote(
                asset_id=asset.id,
                price=data.price,
                previous_close=data.previous_close,
                change=data.change,
                change_percent=data.change_percent,
                currency=data.currency or asset.currency,
                source=provider.name,
                long_name=data.long_name,
                fetched_at=datetime.now(UTC),
            )
        )

    if provider.supports_history():
        for point in provider.get_history(symbol):
            _upsert_price(db, asset.id, point.day, point.close, provider.name)
    db.commit()


def _upsert_price(db: Session, asset_id: int, day: date, close: Decimal, source: str) -> None:
    stmt = (
        dialect_insert(db)(PriceHistory)
        .values(asset_id=asset_id, date=day, close=close, source=source)
        .on_conflict_do_update(
            index_elements=[PriceHistory.asset_id, PriceHistory.date],
            set_={"close": close, "source": source},
        )
    )
    db.execute(stmt)


def backfill_history(
    db: Session,
    portfolio_id: int | None = None,
    limit: int | None = None,
    only_missing: bool = False,
) -> dict:
    """Download daily closes so historical charts show market value, not cost.

    Runs asset by asset and tolerates partial failures; whatever is fetched is
    stored, and the history endpoint falls back to cost basis for the rest.

    ``only_missing`` restricts the run to assets with no stored history at all,
    which is what a newly imported ticker looks like. A full run re-downloads
    every series and takes minutes; this one costs a request per new asset, so
    it can follow an import without turning every statement upload into a
    several-minute download of history already on disk.
    """
    provider = get_provider()
    if not provider.supports_history():
        return {"provider": provider.name, "assets": 0, "points": 0, "detail": "no history support"}

    assets = quotable_assets(db, portfolio_id, only_held=False)
    if only_missing:
        # A single close is what `refresh_quotes` leaves behind, so "has a row"
        # is not the same as "has a history".
        counts = dict(
            db.execute(
                select(PriceHistory.asset_id, func.count(PriceHistory.id)).group_by(PriceHistory.asset_id)
            ).all()
        )
        assets = [a for a in assets if counts.get(a.id, 0) <= 1]
    if limit:
        assets = assets[:limit]
    if not assets:
        return {"provider": provider.name, "assets": 0, "points": 0, "detail": "history is complete"}

    first_trade = db.scalar(select(Transaction.trade_date).order_by(Transaction.trade_date).limit(1))
    total_points = 0
    covered = 0
    for asset in assets:
        points = provider.get_history(resolve_market_symbol(asset), start=first_trade)
        if not points:
            continue
        covered += 1
        for point in points:
            _upsert_price(db, asset.id, point.day, point.close, provider.name)
            total_points += 1
        db.commit()
    logger.info("history backfill: %s assets, %s points", covered, total_points)
    return {"provider": provider.name, "assets": covered, "points": total_points}
