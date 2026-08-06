"""Portfolio-level analytics endpoints."""
from __future__ import annotations

from datetime import date, timedelta

from app.core.dates import local_today
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Query

from app.api.deps import PortfolioSvc

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

RANGE_DAYS = {"1m": 30, "3m": 90, "6m": 182, "1y": 365, "2y": 730, "5y": 1825}


@router.get("/overview", response_model=None, summary="Headline portfolio metrics")
def overview(service: PortfolioSvc) -> dict:
    return service.overview()


@router.get("/positions", response_model=None, summary="Current positions with market data")
def positions(service: PortfolioSvc, include_closed: bool = False) -> list[dict]:
    items = service.asset_positions(include_closed=include_closed)
    total = sum((item.market_value_base for item in items), Decimal(0))
    return [item.to_dict(total) for item in items]


@router.get("/allocation", response_model=None, summary="Allocation grouped by asset, class or broker")
def allocation(service: PortfolioSvc, group_by: Literal["asset", "kind", "broker"] = "asset") -> list[dict]:
    return service.allocation(group_by)


@router.get("/history", response_model=None, summary="Portfolio value over time")
def history(
    service: PortfolioSvc,
    range: Literal["1m", "3m", "6m", "1y", "2y", "5y", "max"] = "max",
    granularity: Literal["auto", "day", "week", "month"] = "auto",
) -> list[dict]:
    start: date | None = None
    if range != "max":
        start = local_today() - timedelta(days=RANGE_DAYS[range])
    return service.history(start=start, granularity=granularity)


@router.get("/profit-history", response_model=None, summary="Accumulated result over time")
def profit_history(
    service: PortfolioSvc,
    range: Literal["6m", "1y", "2y", "5y", "max"] = "1y",
    granularity: Literal["auto", "day", "week", "month"] = "auto",
    group_by: Literal["total", "kind"] = "total",
) -> list[dict]:
    start: date | None = None
    if range != "max":
        start = local_today() - timedelta(days=RANGE_DAYS[range])
    return service.profit_history(start=start, granularity=granularity, group_by=group_by)


@router.get("/income", response_model=None, summary="Dividend / JCP / yield income per period")
def income(service: PortfolioSvc, granularity: Literal["month", "year"] = "month") -> list[dict]:
    return service.income_series(granularity)


@router.get("/dividends", response_model=None, summary="Full income analytics: period, class, asset")
def dividends(
    service: PortfolioSvc, granularity: Literal["month", "quarter", "year"] = "month"
) -> dict:
    return service.dividends(granularity)


@router.get("/dividends/breakdown", response_model=None, summary="Who paid in one period")
def dividends_breakdown(
    service: PortfolioSvc,
    period: str,
    granularity: Literal["month", "quarter", "year"] = "month",
) -> dict:
    return service.income_breakdown(period, granularity)




@router.get("/contributions", response_model=None, summary="Capital deployed per period")
def contributions(service: PortfolioSvc, granularity: Literal["month", "year"] = "month") -> list[dict]:
    return service.contributions_series(granularity)


@router.get("/monthly-returns", response_model=None, summary="Cash-flow adjusted monthly returns")
def monthly_returns(service: PortfolioSvc) -> list[dict]:
    return service.monthly_returns()


@router.post("/snapshots/rebuild", response_model=None, summary="Materialise daily snapshots")
def rebuild_snapshots(service: PortfolioSvc) -> dict:
    return {"points": service.rebuild_snapshots()}


@router.get("/warnings", response_model=None, summary="Data-quality issues detected while replaying history")
def warnings(service: PortfolioSvc, limit: int = Query(default=100, le=1000)) -> list[dict]:
    assets = service.assets()
    issues: list[dict] = []
    for asset_id, position in service.positions().items():
        for message in position.warnings:
            asset = assets.get(asset_id)
            issues.append(
                {
                    "ticker": asset.ticker if asset else str(asset_id),
                    "name": asset.name if asset else "",
                    "message": message,
                }
            )
    return issues[:limit]
