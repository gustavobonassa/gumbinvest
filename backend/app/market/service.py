"""Persists quotes and price history fetched from the configured provider."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import (
    AppSetting,
    Asset,
    AssetSplit,
    PriceHistory,
    Quote,
    QuoteAttempt,
    Transaction,
)
from app.db.upsert import dialect_insert
from app.domain.enums import AssetKind
from app.market.base import QuoteBatch
from app.market.providers import get_provider

logger = get_logger(__name__)

#: How long to wait before each re-attempt of a transiently failed fetch.
#: Short first, because the usual cause is one throttled window during a large
#: first sync and the user is looking at the screen right then; longer after,
#: because by the third failure the provider is having a bad hour.
RETRY_SCHEDULE = (120, 480, 1800)  # 2min, 8min, 30min

#: A ratio of one is not a split, and a non-positive one is bad data.
ZERO_RATIO = Decimal(0)
ONE_RATIO = Decimal(1)

#: ``AssetSplit.source`` for a ratio a person entered. Providers never write it,
#: and a provider sync never overwrites a row carrying it.
MANUAL_SPLIT_SOURCE = "manual"

#: Marks that the one-time historical split sweep has run on this install.
SPLITS_SYNCED_KEY = "splits_synced_at"

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


def _retry_delay(attempts: int) -> float:
    """Seconds to wait after the *n*-th consecutive failure.

    Past the schedule the asset simply rides the normal refresh cadence: by
    then the user has been told it has no price, and hammering a provider that
    has said no four times over half an hour buys nothing.
    """
    if attempts <= len(RETRY_SCHEDULE):
        return float(RETRY_SCHEDULE[attempts - 1])
    return max(settings.price_refresh_minutes, 1) * 60.0


def is_pending(attempt: QuoteAttempt) -> bool:
    """Whether the queue still expects this one to come good.

    While pending, the asset is *not* reported to the user as unpriced — the
    price is late, not absent. Once the schedule is spent, it is reported.
    """
    return attempt.attempts <= len(RETRY_SCHEDULE)


def _store_quotes(
    db: Session, assets: list[Asset], batch: QuoteBatch, provider_name: str, now: datetime
) -> list[int]:
    """Persist every quote in *batch*; returns the assets that got one."""
    priced: list[int] = []
    for asset in assets:
        data = batch.quotes.get(resolve_market_symbol(asset))
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
                source=provider_name,
                long_name=data.long_name,
                fetched_at=now,
            )
        )
        # Keep a daily close so the history chart has real data going forward.
        _upsert_price(db, asset.id, now.date(), data.price, provider_name)
        priced.append(asset.id)
    return priced


def _clear_attempts(db: Session, asset_ids: list[int]) -> None:
    """A price arrived, so the asset is no longer owed a retry."""
    if asset_ids:
        db.execute(delete(QuoteAttempt).where(QuoteAttempt.asset_id.in_(asset_ids)))


def _queue_failures(
    db: Session, assets: list[Asset], failed: dict[str, str], now: datetime
) -> list[str]:
    """Schedule another attempt for each transiently failed fetch."""
    queued: list[str] = []
    for asset in assets:
        symbol = resolve_market_symbol(asset)
        reason = failed.get(symbol) or failed.get(symbol.upper())
        if reason is None:
            continue
        row = db.get(QuoteAttempt, asset.id)
        if row is None:
            row = QuoteAttempt(asset_id=asset.id, attempts=0, first_failed_at=now)
            db.add(row)
        row.symbol = symbol[:32]
        row.attempts += 1
        row.last_error = reason[:255]
        row.last_attempt_at = now
        row.next_attempt_at = now + timedelta(seconds=_retry_delay(row.attempts))
        queued.append(asset.ticker)
    return queued


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

    batch = provider.fetch_quotes([resolve_market_symbol(a) for a in assets])
    now = datetime.now(UTC)

    priced = _store_quotes(db, assets, batch, provider.name, now)
    _clear_attempts(db, priced)
    queued = _queue_failures(db, assets, batch.failed, now)
    db.commit()

    logger.info(
        "quotes refreshed via %s: %s/%s (%s queued for retry)",
        provider.name,
        len(priced),
        len(assets),
        len(queued),
    )
    return {
        "provider": provider.name,
        "requested": len(assets),
        "updated": len(priced),
        # Answered "unknown" by the provider: no price exists to fetch. Kept
        # apart from `queued`, which is the same symptom with a different cause
        # and a different remedy.
        "missing": [
            a.ticker
            for a in assets
            if resolve_market_symbol(a) not in batch.quotes
            and resolve_market_symbol(a) not in batch.failed
        ],
        "queued": queued,
        "fetched_at": now,
    }


def pending_quotes(db: Session, portfolio_id: int | None = None) -> list[dict]:
    """Assets whose price is late rather than absent, soonest first.

    The source behind the "atualizando cotações" notification, and the reason
    those tickers are kept out of the unpriced warning: telling someone their
    asset has no price, when what happened is that Yahoo throttled a first
    sync, is an alarm about nothing they can act on.
    """
    rows = db.scalars(select(QuoteAttempt)).all()
    if not rows:
        return []
    wanted = {a.id: a for a in quotable_assets(db, portfolio_id)}
    pending = [
        {
            "ticker": wanted[row.asset_id].ticker,
            "symbol": row.symbol,
            "attempts": row.attempts,
            "last_error": row.last_error,
            "next_attempt_at": row.next_attempt_at,
        }
        for row in rows
        if row.asset_id in wanted and is_pending(row)
    ]
    pending.sort(key=lambda item: (item["next_attempt_at"], item["ticker"]))
    return pending


def pending_quote_asset_ids(db: Session) -> set[int]:
    """Just the ids, for callers deciding whether to warn about an asset."""
    return {row.asset_id for row in db.scalars(select(QuoteAttempt)).all() if is_pending(row)}


def retry_pending_quotes(db: Session, limit: int = 100) -> dict:
    """Re-attempt the fetches that failed transiently and are now due.

    Runs often and cheaply: the queue is empty whenever nothing is wrong, so
    the common case is one indexed-free scan of an empty table.
    """
    provider = get_provider()
    if provider.name == "none":
        return {"provider": "none", "retried": 0, "recovered": 0}

    now = datetime.now(UTC)
    # Compared here rather than in SQL — see QuoteAttempt.next_attempt_at.
    due: list[QuoteAttempt] = [
        row
        for row in db.scalars(select(QuoteAttempt)).all()
        if row.next_attempt_at.replace(tzinfo=row.next_attempt_at.tzinfo or UTC) <= now
    ][:limit]
    if not due:
        return {"provider": provider.name, "retried": 0, "recovered": 0}

    assets = [db.get(Asset, row.asset_id) for row in due]
    stale = [
        row.asset_id
        for row, asset in zip(due, assets, strict=True)
        if asset is None or asset.price_manual or asset.kind in UNQUOTABLE_KINDS
    ]
    # A paper that was given a manual price, or reclassified into a family with
    # no public quote, is no longer waiting on anything.
    _clear_attempts(db, stale)
    assets = [a for a in assets if a is not None and a.id not in stale]
    if not assets:
        db.commit()
        return {"provider": provider.name, "retried": 0, "recovered": 0}

    batch = provider.fetch_quotes([resolve_market_symbol(a) for a in assets])
    priced = _store_quotes(db, assets, batch, provider.name, now)
    _clear_attempts(db, priced)
    # Answered "unknown" this time: the ticker is not late, it does not exist.
    # Dropping the row lets the asset be reported as unpriced, honestly.
    _clear_attempts(
        db,
        [
            a.id
            for a in assets
            if resolve_market_symbol(a) not in batch.quotes
            and resolve_market_symbol(a) not in batch.failed
        ],
    )
    _queue_failures(db, assets, batch.failed, now)
    db.commit()

    logger.info("quote retries via %s: %s/%s recovered", provider.name, len(priced), len(assets))
    return {
        "provider": provider.name,
        "retried": len(assets),
        "recovered": len(priced),
        "recovered_tickers": [a.ticker for a in assets if a.id in set(priced)],
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
    now = datetime.now(UTC)

    batch = provider.fetch_quotes([symbol])
    priced = _store_quotes(db, [asset], batch, provider.name, now)
    _clear_attempts(db, priced)
    # A page opened during a throttled window queues like any other fetch, so
    # the price turns up on its own instead of the user reloading and hoping.
    _queue_failures(db, [asset], batch.failed, now)

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
    splits_stored = 0
    for asset in assets:
        series = provider.fetch_history(resolve_market_symbol(asset), start=first_trade)
        # Splits are stored even when the closes come back empty: they are what
        # lets an already-stored series be read in the right shares.
        splits_stored += _upsert_splits(db, asset.id, series.splits, provider.name)
        if not series.points:
            db.commit()
            continue
        covered += 1
        for point in series.points:
            _upsert_price(db, asset.id, point.day, point.close, provider.name)
            total_points += 1
        db.commit()
    logger.info(
        "history backfill: %s assets, %s points, %s splits", covered, total_points, splits_stored
    )
    return {
        "provider": provider.name,
        "assets": covered,
        "points": total_points,
        "splits": splits_stored,
    }


def sync_splits(db: Session, portfolio_id: int | None = None) -> dict:
    """Re-check every held asset for declared splits.

    Runs on its own schedule rather than riding the history backfill, because
    the backfill deliberately only touches assets that have *no* history: on an
    install that has been running a while it never fires, and a split declared
    last week would silently invalidate every stored close before it. This costs
    one small request per asset and is the only thing that keeps a historical
    curve honest after a split.
    """
    provider = get_provider()
    if provider.name == "none":
        return {"provider": "none", "assets": 0, "splits": 0}

    assets = quotable_assets(db, portfolio_id, only_held=False)
    stored = 0
    for asset in assets:
        splits = provider.get_splits(resolve_market_symbol(asset))
        if splits:
            stored += _upsert_splits(db, asset.id, splits, provider.name)
    db.commit()
    logger.info("split sync via %s: %s assets, %s splits", provider.name, len(assets), stored)
    return {"provider": provider.name, "assets": len(assets), "splits": stored}


def _upsert_splits(
    db: Session, asset_id: int, splits: list[tuple[date, Decimal]], source: str
) -> int:
    """Record the share splits a provider reported for one asset."""
    stored = 0
    for day, ratio in splits:
        if ratio <= ZERO_RATIO or ratio == ONE_RATIO:
            continue
        stmt = dialect_insert(db)(AssetSplit).values(
            asset_id=asset_id, date=day, ratio=ratio, source=source
        )
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=[AssetSplit.asset_id, AssetSplit.date],
                set_={"ratio": stmt.excluded.ratio, "source": stmt.excluded.source},
                # A ratio typed by hand outranks the provider: it is only ever
                # entered because the provider was wrong or silent, and letting
                # the next sync overwrite it would undo the correction nightly.
                where=AssetSplit.source != MANUAL_SPLIT_SOURCE,
            )
        )
        stored += 1
    return stored


def heal_market_data(db: Session) -> dict:
    """Fetch whatever a failed bootstrap left missing.

    The startup bootstraps (PTAX pairs, index series, benchmarks) swallow
    failures on purpose — a cold start must not hang on an external API — and
    the daily jobs only come round once a day. A first run that hit a timeout
    or a rate limit would leave the sidebar and the return chart empty until
    the next day's slot. This closes the gap: it runs every half hour, fetches
    only what is *absent*, and is a complete no-op the moment everything
    exists — so it can never hammer a provider that is rate-limiting us.
    """
    from app.market import benchmarks as benchmarks_module
    from app.market import fx as fx_module
    from app.market import indices as indices_module

    healed: dict = {}

    try:
        pairs = fx_module.missing_pairs(db)
        for base, quote in pairs:
            fx_module.sync_fx(db, base, quote)
        if pairs:
            healed["fx"] = [f"{base}/{quote}" for base, quote in pairs]
    except Exception:  # noqa: BLE001 — one group failing must not stop the others
        logger.exception("market heal: fx sync failed")

    try:
        absent = benchmarks_module.missing(db)
        if absent:
            benchmarks_module.sync_benchmarks(db)
            healed["benchmarks"] = absent
    except Exception:  # noqa: BLE001
        logger.exception("market heal: benchmark sync failed")

    try:
        if not indices_module.index_status(db):
            indices_module.sync_all_indices(db)
            healed["indices"] = True
    except Exception:  # noqa: BLE001
        logger.exception("market heal: index sync failed")

    try:
        # An install that predates split tracking has years of stored closes and
        # no ratios to read them with, and nothing else would ever fetch them:
        # the nightly backfill only touches assets with no history at all. One
        # pass, then never again — the weekly sync owns it from here.
        #
        # Marked by a settings row rather than by "the table is empty", because
        # a portfolio whose papers never split would leave it empty forever and
        # re-run this every half hour.
        if db.get(AppSetting, SPLITS_SYNCED_KEY) is None:
            if db.scalar(select(PriceHistory.id).limit(1)) is not None:
                healed["splits"] = sync_splits(db)["splits"]
            db.merge(AppSetting(key=SPLITS_SYNCED_KEY, value={"at": datetime.now(UTC).isoformat()}))
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("market heal: split sync failed")

    return healed
