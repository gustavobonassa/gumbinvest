"""Tesouro Direto: daily prices and yields from Tesouro Transparente."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.deps import CurrentPortfolio, DbSession, PortfolioSvc
from app.db.models import Asset
from app.market.treasury import (
    SOURCE,
    contracted_rate,
    coverage,
    is_stale,
    latest_price,
    sync_treasury_prices,
    treasury_assets,
)
from app.portfolio.service import pct

router = APIRouter(prefix="/treasury", tags=["treasury"])

ZERO = Decimal(0)


def _serialize(db, asset: Asset, position, portfolio_id: int) -> dict:
    price = latest_price(db, asset.id)
    quantity = position.quantity if position else ZERO
    cost_basis = position.cost_basis if position else ZERO

    # Positions are marked at the sell side: that is what an early redemption
    # pays. The buy side is reported alongside so the spread stays visible.
    value = quantity * price.sell_price if price else cost_basis
    unrealized = value - cost_basis
    return {
        "ticker": asset.ticker,
        "name": asset.name,
        "quantity": quantity,
        "average_price": position.average_price if position else ZERO,
        "cost_basis": cost_basis,
        "value": value,
        "unrealized": unrealized,
        "unrealized_pct": pct(unrealized, cost_basis),
        "price_date": price.date if price else None,
        "sell_price": price.sell_price if price else None,
        "buy_price": price.buy_price if price else None,
        "sell_rate": price.sell_rate if price else None,
        "buy_rate": price.buy_rate if price else None,
        "spread_pct": (
            pct(price.buy_price - price.sell_price, price.sell_price)
            if price and price.sell_price
            else None
        ),
        "contracted_rate": contracted_rate(db, asset.id, portfolio_id),
        "stale": is_stale(price.date if price else None),
        "is_open": bool(position and position.is_open),
    }


@router.get("", response_model=None, summary="Tesouro Direto positions priced from the daily feed")
def list_treasury(db: DbSession, portfolio: CurrentPortfolio, service: PortfolioSvc) -> dict:
    positions = service.positions()
    items = [_serialize(db, asset, positions.get(asset.id), portfolio.id) for asset in treasury_assets(db)]
    held = [item for item in items if item["is_open"]]
    return {
        "items": items,
        "totals": {
            "cost_basis": sum((item["cost_basis"] for item in held), ZERO),
            "value": sum((item["value"] for item in held), ZERO),
            "unrealized": sum((item["unrealized"] for item in held), ZERO),
        },
        "coverage": coverage(db),
        "source": SOURCE,
    }


@router.post("/sync", response_model=None, summary="Download prices from Tesouro Transparente")
def sync(db: DbSession) -> dict:
    result = sync_treasury_prices(db)
    if result.get("error"):
        raise HTTPException(status_code=502, detail=f"Tesouro Transparente unavailable: {result['error']}")
    return result


@router.get("/{ticker}/history", response_model=None, summary="Daily buy/sell prices and yields")
def history(
    ticker: str,
    db: DbSession,
    start: date | None = None,
    end: date | None = None,
) -> dict:
    from app.db.models import TreasuryPrice

    asset = db.scalar(select(Asset).where(Asset.ticker == ticker.upper()))
    if asset is None:
        raise HTTPException(status_code=404, detail=f"asset {ticker} not found")

    stmt = select(TreasuryPrice).where(TreasuryPrice.asset_id == asset.id)
    if start:
        stmt = stmt.where(TreasuryPrice.date >= start)
    if end:
        stmt = stmt.where(TreasuryPrice.date <= end)
    rows = db.scalars(stmt.order_by(TreasuryPrice.date)).all()
    return {
        "ticker": asset.ticker,
        "name": asset.name,
        "points": [
            {
                "date": row.date,
                "buy_price": row.buy_price,
                "sell_price": row.sell_price,
                "buy_rate": row.buy_rate,
                "sell_rate": row.sell_rate,
            }
            for row in rows
        ],
    }
