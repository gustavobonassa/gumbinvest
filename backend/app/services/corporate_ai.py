"""AI-assisted discovery of corporate events for the user's own tickers.

The B3 export never links a predecessor to its successor, and the heuristic in
:mod:`app.portfolio.corporate_actions` can only reason from movement evidence.
This module adds the third source: the configured AI model searches the web
for events (renames, mergers, delistings) affecting the portfolio's tickers
and proposes them as :class:`SuccessionAiSuggestion` rows. Nothing is ever
applied by the model — every proposal waits for the user's accept/decline,
and accepting goes through the same ``AssetSuccession`` any manual
declaration uses.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import (
    Asset,
    AssetSuccession,
    AuditLog,
    SuccessionAiSuggestion,
    Transaction,
)
from app.domain.enums import AssetKind
from app.market import lookup

logger = get_logger(__name__)

ZERO = Decimal(0)

EVENT_TYPES = {"rename", "merger", "delisting", "spinoff", "other"}

#: Families corporate events can actually hit. Synthetic papers (renda fixa,
#: cash accounts) and rights never rename or merge.
_EXCLUDED_KINDS = {
    AssetKind.FIXED_INCOME.value,
    AssetKind.TREASURY.value,
    AssetKind.SUBSCRIPTION.value,
    AssetKind.OTHER.value,
}


def scan_context(db: Session, portfolio_id: int) -> tuple[list[dict], set[str]]:
    """(model-facing asset list, known tickers) for one portfolio.

    Only assets the user actually transacted — the whole point is fixing the
    replay of their history. Already-declared successions ride along so the
    model does not repeat them.
    """
    rows = db.execute(
        select(
            Asset,
            func.min(Transaction.trade_date),
            func.max(Transaction.trade_date),
        )
        .join(Transaction, Transaction.asset_id == Asset.id)
        .where(Transaction.portfolio_id == portfolio_id)
        .group_by(Asset.id)
        .order_by(Asset.ticker)
    ).all()

    declared_ids = {
        row.from_asset_id
        for row in db.scalars(
            select(AssetSuccession).where(AssetSuccession.portfolio_id == portfolio_id)
        ).all()
    }

    context: list[dict] = []
    known: set[str] = set()
    for asset, first_trade, last_trade in rows:
        if asset.kind in _EXCLUDED_KINDS or asset.is_cash_account:
            continue
        known.add(asset.ticker)
        context.append(
            {
                "ticker": asset.ticker,
                "nome": asset.name,
                "classe": asset.kind,
                "moeda": asset.currency,
                "primeiro_movimento": first_trade.isoformat() if first_trade else None,
                "ultimo_movimento": last_trade.isoformat() if last_trade else None,
                "evento_ja_declarado": asset.id in declared_ids,
            }
        )
    return context, known


def normalize_events(data: dict | None, known: set[str]) -> list[dict]:
    """Model output → validated event items. Unknown tickers are dropped —
    the scan is about the user's history, not the market at large."""
    items = (data or {}).get("events")
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        from_ticker = str(raw.get("from_ticker") or "").strip().upper()
        if not from_ticker or from_ticker not in known or from_ticker in seen:
            continue
        try:
            effective = date.fromisoformat(str(raw.get("effective_date") or ""))
        except ValueError:
            continue
        to_ticker = str(raw.get("to_ticker") or "").strip().upper() or None
        if to_ticker == from_ticker:
            continue
        try:
            cash = Decimal(str(raw.get("cash_amount") or 0))
        except ArithmeticError:
            cash = ZERO
        event_type = str(raw.get("event_type") or "other").strip().lower()
        seen.add(from_ticker)
        out.append(
            {
                "from_ticker": from_ticker,
                "to_ticker": to_ticker,
                "effective_date": effective,
                "cash_amount": max(cash, ZERO).quantize(Decimal("0.01")),
                "event_type": event_type if event_type in EVENT_TYPES else "other",
                "rationale": str(raw.get("rationale") or "").strip() or None,
                "source": (str(raw.get("source") or "").strip() or None),
            }
        )
    return out[:30]


def store_suggestions(
    db: Session, portfolio_id: int, items: list[dict], *, provider: str, model: str
) -> list[SuccessionAiSuggestion]:
    """Persist proposals, skipping what the user already settled.

    Skipped: tickers with a declared succession, tickers with a pending
    proposal, and proposals identical to one previously declined — a re-scan
    must not nag about a decision already made. The caller commits.
    """
    declared = {
        asset.ticker
        for asset in db.scalars(
            select(Asset)
            .join(AssetSuccession, AssetSuccession.from_asset_id == Asset.id)
            .where(AssetSuccession.portfolio_id == portfolio_id)
        ).all()
    }
    existing = db.scalars(
        select(SuccessionAiSuggestion).where(
            SuccessionAiSuggestion.portfolio_id == portfolio_id
        )
    ).all()
    pending_tickers = {row.from_ticker for row in existing if row.status == "pending"}
    settled_keys = {
        (row.from_ticker, row.to_ticker, row.effective_date)
        for row in existing
        if row.status in ("declined", "accepted")
    }

    stored: list[SuccessionAiSuggestion] = []
    for item in items:
        if item["from_ticker"] in declared or item["from_ticker"] in pending_tickers:
            continue
        if (item["from_ticker"], item["to_ticker"], item["effective_date"]) in settled_keys:
            continue
        row = SuccessionAiSuggestion(
            portfolio_id=portfolio_id,
            from_ticker=item["from_ticker"],
            to_ticker=item["to_ticker"],
            effective_date=item["effective_date"],
            cash_amount=item["cash_amount"],
            event_type=item["event_type"],
            rationale=item["rationale"],
            source=(item["source"] or "")[:255] or None,
            status="pending",
            provider=provider,
            model=model,
        )
        db.add(row)
        db.flush()
        stored.append(row)
        pending_tickers.add(row.from_ticker)
    return stored


def serialize_suggestion(row: SuccessionAiSuggestion) -> dict:
    return {
        "id": row.id,
        "from_ticker": row.from_ticker,
        "to_ticker": row.to_ticker,
        "effective_date": row.effective_date,
        "cash_amount": row.cash_amount,
        "event_type": row.event_type,
        "rationale": row.rationale,
        "source": row.source,
        "status": row.status,
        "provider": row.provider,
        "model": row.model,
        "created_at": row.created_at,
    }


class SuggestionError(Exception):
    """Why a suggestion cannot be applied — message is pt-BR, shown as-is."""


def accept_suggestion(db: Session, portfolio_id: int, row: SuccessionAiSuggestion) -> AssetSuccession:
    """Turn one accepted proposal into a real declared succession.

    Same semantics as the manual endpoint: upserts on (portfolio, from_asset).
    An unknown successor ticker is resolved against the market and created as
    a watch-only asset — a rename's new code may never have been imported.
    The caller commits.
    """
    source = db.scalar(select(Asset).where(Asset.ticker == row.from_ticker))
    if source is None:
        raise SuggestionError(f"O ativo {row.from_ticker} não existe mais na base.")

    target: Asset | None = None
    if row.to_ticker:
        target = db.scalar(select(Asset).where(Asset.ticker == row.to_ticker))
        if target is None:
            hit = lookup.resolve(row.to_ticker)
            if hit is None:
                raise SuggestionError(
                    f"Não foi possível validar {row.to_ticker} no mercado. Declare manualmente se tiver certeza."
                )
            target = db.scalar(select(Asset).where(Asset.ticker == hit.ticker))
            if target is None:
                target = Asset(
                    ticker=hit.ticker,
                    name=hit.name[:255],
                    kind=hit.kind,
                    currency=hit.currency,
                    market_symbol=hit.market_symbol,
                )
                db.add(target)
                db.add(
                    AuditLog(
                        action="asset.watch",
                        detail={"ticker": hit.ticker, "name": hit.name},
                    )
                )
                db.flush()
        if target.id == source.id:
            raise SuggestionError("Um ativo não pode suceder a si mesmo.")

    existing = db.scalar(
        select(AssetSuccession).where(
            AssetSuccession.portfolio_id == portfolio_id,
            AssetSuccession.from_asset_id == source.id,
        )
    )
    succession = existing or AssetSuccession(portfolio_id=portfolio_id, from_asset_id=source.id)
    succession.to_asset_id = target.id if target else None
    succession.effective_date = row.effective_date
    succession.cash_amount = row.cash_amount
    succession.note = row.rationale
    succession.source = "ai"
    db.add(succession)
    db.add(
        AuditLog(
            action="portfolio.succession",
            detail={
                "from": source.ticker,
                "to": target.ticker if target else None,
                "date": row.effective_date.isoformat(),
                "cash": str(row.cash_amount),
                "source": "ai",
            },
        )
    )
    row.status = "accepted"
    row.resolved_at = datetime.now(UTC)
    return succession
