"""Corporate actions: declaring that one asset was replaced by another."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentPortfolio, DbSession, PortfolioSvc
from app.db.models import Asset, AssetSuccession, AuditLog
from app.portfolio.corporate_actions import suggest_successions

router = APIRouter(prefix="/corporate-actions", tags=["corporate actions"])


class SuccessionPayload(BaseModel):
    from_ticker: str
    #: ``None`` marks the asset an artifact: every movement of it is dropped.
    to_ticker: str | None = None
    effective_date: date
    cash_amount: Decimal = Field(default=Decimal(0), ge=0)
    note: str | None = None
    source: str = "manual"


def _serialize(db, row: AssetSuccession) -> dict:
    source = db.get(Asset, row.from_asset_id)
    target = db.get(Asset, row.to_asset_id) if row.to_asset_id else None
    return {
        "id": row.id,
        "from_ticker": source.ticker if source else None,
        "from_name": source.name if source else None,
        "to_ticker": target.ticker if target else None,
        "to_name": target.name if target else None,
        "effective_date": row.effective_date,
        "cash_amount": row.cash_amount,
        "note": row.note,
        "source": row.source,
    }


def _asset(db, ticker: str) -> Asset:
    asset = db.scalar(select(Asset).where(Asset.ticker == ticker.upper()))
    if asset is None:
        raise HTTPException(status_code=404, detail=f"asset {ticker} not found")
    return asset


@router.get("", response_model=None, summary="Declared successions and detected candidates")
def list_actions(db: DbSession, portfolio: CurrentPortfolio) -> dict:
    rows = db.scalars(
        select(AssetSuccession)
        .where(AssetSuccession.portfolio_id == portfolio.id)
        .order_by(AssetSuccession.effective_date)
    ).all()
    return {
        "items": [_serialize(db, row) for row in rows],
        "suggestions": suggest_successions(db, portfolio.id),
    }


@router.post("", response_model=None, summary="Declare that an asset was replaced")
def create_action(payload: SuccessionPayload, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    source = _asset(db, payload.from_ticker)
    target = _asset(db, payload.to_ticker) if payload.to_ticker else None
    if target is not None and target.id == source.id:
        raise HTTPException(status_code=422, detail="an asset cannot succeed itself")

    existing = db.scalar(
        select(AssetSuccession).where(
            AssetSuccession.portfolio_id == portfolio.id,
            AssetSuccession.from_asset_id == source.id,
        )
    )
    row = existing or AssetSuccession(portfolio_id=portfolio.id, from_asset_id=source.id)
    row.to_asset_id = target.id if target else None
    row.effective_date = payload.effective_date
    row.cash_amount = payload.cash_amount
    row.note = payload.note
    row.source = payload.source
    db.add(row)
    db.add(
        AuditLog(
            action="portfolio.succession",
            detail={
                "from": source.ticker,
                "to": target.ticker if target else None,
                "date": payload.effective_date.isoformat(),
                "cash": str(payload.cash_amount),
            },
        )
    )
    db.commit()
    db.refresh(row)
    return _serialize(db, row)


@router.delete("/{action_id}", response_model=None, summary="Undo a declared succession")
def delete_action(action_id: int, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    row = db.get(AssetSuccession, action_id)
    if row is None or row.portfolio_id != portfolio.id:
        raise HTTPException(status_code=404, detail="succession not found")
    db.delete(row)
    db.commit()
    return {"deleted": action_id}


@router.get("/preview", response_model=None, summary="Positions affected by the declared successions")
def preview(service: PortfolioSvc, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    """What the successions changed, for a sanity check after applying them."""
    touched: set[int] = set()
    for row in db.scalars(
        select(AssetSuccession).where(AssetSuccession.portfolio_id == portfolio.id)
    ).all():
        touched.add(row.from_asset_id)
        if row.to_asset_id:
            touched.add(row.to_asset_id)

    assets = service.assets()
    positions = service.positions()
    return {
        "positions": [
            {
                "ticker": assets[asset_id].ticker,
                "quantity": position.quantity,
                "cost_basis": position.cost_basis,
                "average_price": position.average_price,
                "realized_pnl": position.realized_pnl,
                "returned_capital": position.returned_capital,
                "notes": position.notes,
            }
            for asset_id, position in positions.items()
            if asset_id in touched and asset_id in assets
        ]
    }
