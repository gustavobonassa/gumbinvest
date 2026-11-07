"""Dividends declared to B3 but not yet paid, sized by the current position.

Served from the cached per-asset fundamentals (B3's registry carries the
payment date, record date and amount per share — free, no key). The totals are
estimates: they assume the position on the record date equals the position
held now. Shared by the Proventos page and the AI analyst's tools, so both
answer "what is coming" with the same list.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.dates import local_today
from app.db.models import AssetFundamentals


def upcoming_dividends(db: Session, service, *, refresh: bool = False) -> dict:
    """``{"items", "missing", "updated_at"}`` — see the module docstring.

    ``refresh`` re-fetches stale assets inline; the beat schedule also does it
    daily.
    """
    from app.market.fundamentals import DECLARABLE_KINDS, refresh_held_fundamentals

    if refresh:
        refresh_held_fundamentals(db, service.portfolio_id, only_stale=True)

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
        key=lambda item: (
            item["date_pending"],
            item["payment_date"] or item["record_date"] or "9999-12-31",
        )
    )
    return {"items": items, "missing": missing, "updated_at": updated_at}
