"""Impostos: the monthly income-tax worksheet on sales (DARF)."""
from __future__ import annotations

import re
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import CurrentPortfolio, DbSession, PortfolioSvc
from app.portfolio import monthly_tax

router = APIRouter(prefix="/taxes", tags=["taxes"])

_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class DayTradePayload(BaseModel):
    #: The month's net day-trade result from the broker's report; may be negative.
    result: Decimal = Field(max_digits=14, decimal_places=2)
    #: IRRF withheld on day trade (1 % of gains), as the report states it.
    irrf: Decimal = Field(default=Decimal(0), ge=0, max_digits=12, decimal_places=2)


class PaidPayload(BaseModel):
    paid: bool


def _check_period(period: str) -> None:
    if not _PERIOD.match(period):
        raise HTTPException(status_code=422, detail="período deve ser AAAA-MM")


@router.get("/monthly", response_model=None, summary="Monthly tax worksheet on sales")
def monthly(service: PortfolioSvc) -> dict:
    return monthly_tax.monthly_taxes(service)


@router.put("/day-trade/{period}", response_model=None, summary="Inform a month's day-trade result")
def set_day_trade(
    period: str, payload: DayTradePayload, db: DbSession, portfolio: CurrentPortfolio
) -> dict:
    _check_period(period)
    monthly_tax.save_day_trade(db, portfolio.id, period, payload.result, payload.irrf)
    db.commit()
    return {"saved": True}


@router.delete("/day-trade/{period}", response_model=None, summary="Clear a month's day-trade result")
def clear_day_trade(period: str, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    _check_period(period)
    monthly_tax.save_day_trade(db, portfolio.id, period, None)
    db.commit()
    return {"deleted": True}


@router.put("/paid/{period}", response_model=None, summary="Mark a month's DARF as paid (or not)")
def set_paid(period: str, payload: PaidPayload, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    _check_period(period)
    monthly_tax.set_paid(db, portfolio.id, period, payload.paid)
    db.commit()
    return {"paid": payload.paid}
