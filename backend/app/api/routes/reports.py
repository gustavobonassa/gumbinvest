"""Reporting endpoints (monthly, annual, income, performers, allocation)."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter

from app.api.deps import PortfolioSvc

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/monthly", response_model=None, summary="Monthly performance")
def monthly(service: PortfolioSvc) -> list[dict]:
    return service.monthly_returns()


@router.get("/annual", response_model=None, summary="Yearly totals: capital deployed, sold, income")
def annual(service: PortfolioSvc) -> list[dict]:
    return service.annual_report()


@router.get("/income", response_model=None, summary="Income report by period")
def income(service: PortfolioSvc, granularity: Literal["month", "year"] = "year") -> list[dict]:
    return service.income_series(granularity)


@router.get("/performers", response_model=None, summary="Best and worst performing assets")
def performers(
    service: PortfolioSvc,
    limit: int = 5,
    window: Literal["day", "1m", "3m", "6m", "1y", "total"] = "total",
) -> dict:
    return service.performance(window, limit)


@router.get("/allocation", response_model=None, summary="Allocation report across every grouping")
def allocation(service: PortfolioSvc) -> dict:
    return {
        "by_asset": service.allocation("asset"),
        "by_kind": service.allocation("kind"),
        "by_broker": service.allocation("broker"),
    }


@router.get("/summary", response_model=None, summary="Everything the reports page needs in one call")
def summary(service: PortfolioSvc) -> dict:
    overview = service.overview()
    income_by_year = service.income_series("year")
    return {
        "overview": overview,
        "annual": service.annual_report(),
        "income_by_year": income_by_year,
        "performers": service.performers(5),
        "allocation": {
            "by_kind": service.allocation("kind"),
            "by_broker": service.allocation("broker"),
        },
        "totals": {
            "income": sum((row["total"] for row in income_by_year), Decimal(0)),
            "realized": overview["realized_pnl"],
            "unrealized": overview["unrealized_pnl"],
        },
    }
