"""Search, settings, market control, watchlist and health endpoints."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import String, cast, func, or_, select

from app.api.deps import CurrentPortfolio, DbSession, PortfolioSvc
from app.core.config import settings
from app.core.dates import local_today
from app.db.models import Asset, AppSetting, AuditLog, Quote, Transaction, WatchlistItem
from app.importer.classifier import known_movements
from app.importer.pdf import available_formats
from app.market.benchmarks import latest_levels
from app.market.crypto import headline_prices, sync_crypto_fx
from app.market.fx import backfill_transaction_fx, fx_status, sync_all_fx
from app.market.providers import available_providers, get_provider
from app.market.service import backfill_history, refresh_quotes
from app.services.ai_providers import providers_public
from app.services.notifications import PAGE_SIZE, catalog as notification_catalog
from app.services.secrets import SECRET_KEYS, secret_status, store_secret

router = APIRouter(tags=["system"])

DEFAULT_SETTINGS: dict[str, object] = {
    "currency": settings.base_currency,
    "timezone": settings.timezone,
    "theme": "dark",
    "market_data_provider": settings.market_data_provider,
    "price_refresh_minutes": settings.price_refresh_minutes,
    "number_format": "pt-BR",
    "hide_values": False,
    "benchmark": "^BVSP",
    # The asset universe is opt-in and off by default: it downloads a few
    # hundred MB of public files that most users will never need.
    "universe_enabled": False,
    "universe_markets": ["B3"],
    #: Years of COTAHIST to reduce. 2 covers a trailing 12-month window in any
    #: month; 1 loses the 12-month return early in the year.
    "universe_history_years": 2,
    #: Identification sent to the SEC. Not a secret — a contact, which may be a
    #: project URL rather than an e-mail. Empty until the user chooses one.
    "sec_user_agent": "",
    #: Kinds silenced in the bell. Empty by default, and stored as the *off*
    #: set so a producer added in a later release is heard rather than missing
    #: from a list saved before it existed — see notifications.MUTED_SETTING.
    "notification_muted_kinds": [],
    #: Cloud backup app identifiers. An OAuth client id / app key is public by
    #: design; the matching secrets live in SECRET_KEYS and never come back.
    "gdrive_client_id": settings.gdrive_client_id,
    "dropbox_app_key": settings.dropbox_app_key,
    #: First-run wizard: set once when it finishes (or is skipped), so a fresh
    #: install reads `false` rather than nothing.
    "onboarding_completed": False,
    #: The owner's name and declared goals, folded into the AI prompts — see
    #: services/user_profile.py. Both optional, both editable later.
    "user_name": "",
    "investor_profile": {},
}


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------
@router.get("/health", response_model=None, summary="Liveness probe")
def health(db: DbSession) -> dict:
    db.execute(select(func.now()))
    return {"status": "ok", "app": settings.app_name, "time": datetime.now(UTC)}


# --------------------------------------------------------------------------
# Global search
# --------------------------------------------------------------------------
@router.get("/search", response_model=None, summary="Search tickers, company names, dates and movements")
def search(q: str, db: DbSession, portfolio: CurrentPortfolio, limit: int = Query(default=20, ge=1, le=100)) -> dict:
    needle = f"%{q.strip().lower()}%"
    assets = db.scalars(
        select(Asset)
        .where(or_(func.lower(Asset.ticker).like(needle), func.lower(Asset.name).like(needle)))
        .limit(limit)
    ).all()
    transactions = db.execute(
        select(Transaction, Asset)
        .join(Asset, Asset.id == Transaction.asset_id)
        .where(
            Transaction.portfolio_id == portfolio.id,
            or_(
                func.lower(Asset.ticker).like(needle),
                func.lower(Transaction.raw_movement).like(needle),
                func.lower(Transaction.raw_product).like(needle),
                cast(Transaction.trade_date, String).like(needle),
            ),
        )
        .order_by(Transaction.trade_date.desc())
        .limit(limit)
    ).all()
    return {
        "assets": [
            {"ticker": a.ticker, "name": a.name, "kind": a.kind} for a in assets
        ],
        "transactions": [
            {
                "id": t.id,
                "date": t.trade_date,
                "ticker": a.ticker,
                "op_type": t.op_type,
                "movement": t.raw_movement,
                "quantity": t.quantity,
                "gross_amount": t.gross_amount,
            }
            for t, a in transactions
        ],
    }


@router.get("/search/market", response_model=None, summary="Search the market for tickers not in the portfolio")
def search_market_endpoint(q: str, db: DbSession) -> dict:
    """Tickers the market knows but the portfolio never traded.

    Separate from ``/search`` on purpose: this one leaves the machine (Yahoo's
    search endpoint), so the local results stay instant and the frontend can
    debounce this call independently. Tickers that already have a local asset
    row are filtered out — they are ``/search``'s answer.
    """
    from app.market.lookup import search_market

    hits = search_market(q)
    if not hits:
        return {"items": []}
    known = set(
        db.scalars(select(Asset.ticker).where(Asset.ticker.in_([h.ticker for h in hits]))).all()
    )
    return {
        "items": [
            {
                "ticker": h.ticker,
                "name": h.name,
                "kind": h.kind,
                "currency": h.currency,
                "exchange": h.exchange,
            }
            for h in hits
            if h.ticker not in known
        ]
    }


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
class SettingsPayload(BaseModel):
    values: dict


@router.get("/settings", response_model=None, summary="Application settings")
def get_settings_endpoint(db: DbSession) -> dict:
    stored = {row.key: row.value for row in db.scalars(select(AppSetting)).all()}
    merged = {**DEFAULT_SETTINGS, **{k: v.get("value") if isinstance(v, dict) else v for k, v in stored.items()}}
    # API keys are write-only: the UI learns whether one is configured, never
    # its value.
    for key in SECRET_KEYS:
        merged.pop(key, None)
    # Machinery rows sharing the settings table are state, not preferences —
    # the cloud device-flow row even carries a device_code, and the bell's row
    # is bookkeeping about what has been read.
    from app.services.cloud_backup import INTERNAL_KEYS as CLOUD_INTERNAL_KEYS
    from app.services.notifications import INTERNAL_KEYS as BELL_INTERNAL_KEYS

    for key in (*CLOUD_INTERNAL_KEYS, *BELL_INTERNAL_KEYS):
        merged.pop(key, None)
    return {
        "values": merged,
        "secrets": secret_status(),
        "ai": providers_public(db),
        "providers": available_providers(),
        "provider_active": get_provider().name,
        "known_movements": known_movements(),
        "statement_formats": available_formats(),
        # The switches the Notificações card draws. Served rather than hardcoded
        # in the UI, so a new producer becomes a new switch on the backend alone.
        "notification_catalog": notification_catalog(),
        "env": {
            "dev_tools": settings.dev_tools,
            "base_currency": settings.base_currency,
            "timezone": settings.timezone,
            "price_refresh_minutes": settings.price_refresh_minutes,
            "quote_cache_ttl": settings.quote_cache_ttl,
            "market_data_provider": settings.market_data_provider,
            "brapi_token_configured": bool(settings.brapi_token),
        },
    }


@router.put("/settings", response_model=None, summary="Update application settings")
def update_settings(payload: SettingsPayload, db: DbSession) -> dict:
    for key, value in payload.values.items():
        if key in SECRET_KEYS:
            store_secret(db, key, str(value or ""))
        else:
            db.merge(AppSetting(key=key, value={"value": value}))
    # Keys only, on purpose: a secret's value must never reach the audit log.
    db.add(AuditLog(action="settings.update", detail={"keys": list(payload.values)}))
    db.commit()
    return get_settings_endpoint(db)


# --------------------------------------------------------------------------
# Market data control
# --------------------------------------------------------------------------
@router.get("/notifications", response_model=None, summary="One page of the header bell")
def notifications(
    db: DbSession,
    portfolio: CurrentPortfolio,
    cursor: int | None = Query(None, description="Last id of the previous page; omit for the first"),
    limit: int = Query(PAGE_SIZE, ge=1, le=50),
) -> dict:
    """Live entries plus a page of history, newest first.

    The live half rides along with the first page only — it describes what is
    happening now, not a point in the history being scrolled.
    """
    from app.services.notifications import feed

    return feed(db, portfolio.id, cursor=cursor, limit=limit)


@router.post("/notifications/read", response_model=None, summary="Mark the bell as read")
def notifications_read(db: DbSession, portfolio: CurrentPortfolio) -> dict:
    """Called when the panel closes: everything visible has now been seen."""
    from app.services.notifications import mark_all_read, unread_count

    marked = mark_all_read(db, portfolio.id)
    db.commit()
    return {"marked": marked, "unread": unread_count(db, portfolio.id)}


class ArchivePayload(BaseModel):
    #: ``"live"`` for a derived entry, ``"stored"`` for a history row.
    source: str
    id: str


@router.post("/notifications/archive", response_model=None, summary="Hide one notification")
def notifications_archive(
    payload: ArchivePayload, db: DbSession, portfolio: CurrentPortfolio
) -> dict:
    from app.services.notifications import archive, unread_count

    if payload.source not in {"live", "stored"}:
        raise HTTPException(status_code=422, detail="source deve ser 'live' ou 'stored'")
    archived = archive(db, payload.source, payload.id)
    if not archived:
        db.rollback()
        raise HTTPException(status_code=404, detail="notificação não encontrada")
    db.commit()
    return {"archived": True, "unread": unread_count(db, portfolio.id)}


@router.get("/market/status", response_model=None, summary="Quote freshness overview")
def market_status(db: DbSession) -> dict:
    rows = db.execute(
        select(Asset.ticker, Quote.price, Quote.change_percent, Quote.source, Quote.fetched_at)
        .join(Quote, Quote.asset_id == Asset.id)
        .order_by(Quote.fetched_at.desc())
    ).all()
    return {
        "provider": get_provider().name,
        "quotes": [
            {
                "ticker": ticker,
                "price": price,
                "change_percent": change,
                "source": source,
                "fetched_at": fetched_at,
            }
            for ticker, price, change, source, fetched_at in rows
        ],
        "last_update": rows[0].fetched_at if rows else None,
        # Exchange rates sit alongside quotes: both are external market data the
        # portfolio depends on, and both are worth seeing the freshness of.
        "fx": fx_status(db),
        # Headline coin prices, converted to the portfolio's currency. Kept
        # apart from `fx` on purpose — one is a price, the other is a rate, and
        # the two only look alike.
        "benchmarks": headline_prices(db, settings.base_currency),
        # Index levels (today: the Ibovespa). Points, not money — kept out of
        # `benchmarks` so nothing is tempted to print "R$" in front of them.
        "indices": latest_levels(db),
    }


@router.post("/market/fx/sync", response_model=None, summary="Refresh the PTAX series now")
def market_fx_sync(db: DbSession) -> dict:
    """Download the latest rates and stamp any movement still missing one."""
    result = sync_all_fx(db)
    # Coin-quoted exchange trades convert through the coin's own closes, which
    # are published into the same table — see app.market.crypto.
    result["crypto"] = sync_crypto_fx(db)["points"]
    result["backfilled"] = backfill_transaction_fx(db)["updated"]
    return result


@router.post("/market/refresh", response_model=None, summary="Refresh quotes now")
def market_refresh(db: DbSession, portfolio: CurrentPortfolio, force: bool = True) -> dict:
    return refresh_quotes(db, portfolio.id, force=force)


@router.post("/market/backfill", response_model=None, summary="Download daily closes for historical charts")
def market_backfill(db: DbSession, portfolio: CurrentPortfolio, limit: int | None = None) -> dict:
    return backfill_history(db, portfolio.id, limit=limit)


@router.post("/market/benchmarks/sync", response_model=None, summary="Download the Ibovespa series")
def market_benchmarks_sync(db: DbSession) -> dict:
    from app.market.benchmarks import sync_benchmarks

    return sync_benchmarks(db)


@router.get("/market/benchmarks", response_model=None, summary="Benchmark series held")
def market_benchmarks(db: DbSession) -> list[dict]:
    from app.market.benchmarks import coverage

    return coverage(db)


# --------------------------------------------------------------------------
# Watchlist
# --------------------------------------------------------------------------
class WatchlistPayload(BaseModel):
    ticker: str
    note: str | None = None
    target_price: Decimal | None = None


@router.get("/watchlist", response_model=None, summary="Watchlist entries")
def list_watchlist(db: DbSession) -> list[dict]:
    items = db.scalars(select(WatchlistItem).order_by(WatchlistItem.ticker)).all()
    quotes = {
        ticker: (price, change)
        for ticker, price, change in db.execute(
            select(Asset.ticker, Quote.price, Quote.change_percent).join(Quote, Quote.asset_id == Asset.id)
        ).all()
    }
    return [
        {
            "id": item.id,
            "ticker": item.ticker,
            "note": item.note,
            "target_price": item.target_price,
            "price": quotes.get(item.ticker, (None, None))[0],
            "change_percent": quotes.get(item.ticker, (None, None))[1],
        }
        for item in items
    ]


@router.post("/watchlist", response_model=None, summary="Add a ticker to the watchlist")
def add_watchlist(payload: WatchlistPayload, db: DbSession) -> dict:
    ticker = payload.ticker.upper().strip()
    if db.scalar(select(WatchlistItem).where(WatchlistItem.ticker == ticker)):
        raise HTTPException(status_code=409, detail="ticker already in the watchlist")
    item = WatchlistItem(ticker=ticker, note=payload.note, target_price=payload.target_price)
    db.add(item)
    db.commit()
    return {"id": item.id, "ticker": item.ticker}


@router.delete("/watchlist/{item_id}", response_model=None, summary="Remove a watchlist entry")
def delete_watchlist(item_id: int, db: DbSession) -> dict:
    item = db.get(WatchlistItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    db.delete(item)
    db.commit()
    return {"deleted": item_id}


# --------------------------------------------------------------------------
# Dividend calendar & audit
# --------------------------------------------------------------------------
@router.get("/dividends/calendar", response_model=None, summary="Most recent income payments")
def dividend_calendar(service: PortfolioSvc, limit: int = Query(default=100, le=600)) -> list[dict]:
    return service.income_calendar(limit)


@router.get("/dividends/upcoming", response_model=None, summary="Declared payments not yet received")
def upcoming_dividends(
    db: DbSession, portfolio: CurrentPortfolio, service: PortfolioSvc, refresh: bool = False
) -> dict:
    """Dividends declared to B3 but not yet paid, sized by the current position.

    Served from the cached per-asset fundamentals (B3's registry carries the
    payment date, record date and amount per share — free, no key). ``refresh``
    re-fetches stale assets inline; the beat schedule also does it daily. The
    totals are estimates: they assume the position on the record date equals
    the position held now.
    """
    from app.db.models import AssetFundamentals
    from app.market.fundamentals import DECLARABLE_KINDS, refresh_held_fundamentals

    if refresh:
        refresh_held_fundamentals(db, portfolio.id, only_stale=True)

    today = local_today().isoformat()
    items: list[dict] = []
    missing: list[str] = []
    updated_at = None
    for ap in service.asset_positions():
        asset = ap.asset
        if asset.kind not in DECLARABLE_KINDS or asset.price_manual:
            continue
        if (asset.currency or "BRL").upper() != "BRL":
            continue
        cached = db.get(AssetFundamentals, asset.id)
        if cached is None or not cached.data:
            missing.append(asset.ticker)
            continue
        if cached.fetched_at is not None and (updated_at is None or cached.fetched_at < updated_at):
            updated_at = cached.fetched_at
        held = float(ap.position.quantity)
        for row in cached.data.get("announced_dividends") or []:
            pending = bool(row.get("date_pending"))
            when = row.get("payment_date") or row.get("record_date")
            # The stored list was upcoming when fetched; it ages until the
            # next refresh, so what has since been paid is filtered out here.
            if not pending and (not when or when < today):
                continue
            rate = row.get("rate")
            if not rate or held <= 0:
                continue
            items.append(
                {
                    "ticker": asset.ticker,
                    "name": asset.name,
                    "kind": asset.kind,
                    "label": row.get("label"),
                    "payment_date": row.get("payment_date"),
                    "record_date": row.get("record_date"),
                    "date_pending": pending,
                    "rate": rate,
                    "quantity": held,
                    "total": round(rate * held, 2),
                }
            )
    items.sort(
        key=lambda item: (item["date_pending"], item["payment_date"] or item["record_date"] or "9999-12-31")
    )
    return {"items": items, "missing": missing, "updated_at": updated_at}


@router.get("/audit", response_model=None, summary="Audit log")
def audit(db: DbSession, limit: int = Query(default=100, ge=1, le=1000)) -> list[dict]:
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
    return [{"id": r.id, "at": r.at, "action": r.action, "detail": r.detail} for r in rows]
