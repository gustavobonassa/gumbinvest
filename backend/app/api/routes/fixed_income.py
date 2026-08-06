"""Fixed income: index series, yield terms and accrued valuation."""
from __future__ import annotations

from datetime import date

from app.core.dates import local_today
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentPortfolio, DbSession, PortfolioSvc
from app.db.models import Asset, FixedIncomeTerms
from app.portfolio import accounts as accounts_api
from app.portfolio.accounts import AccountError
from app.market.fixed_income import (
    ACCRUED_KINDS,
    accrual_factor,
    ensure_terms_for_fixed_income,
    get_terms,
    implied_percent_of_index,
    value_any,
    value_position,
)
from app.market.indices import available_indices, index_status, sync_all_indices, sync_index

router = APIRouter(prefix="/fixed-income", tags=["fixed income"])


class TermsPayload(BaseModel):
    index_code: Literal["CDI", "SELIC", "IPCA", "PRE"] = "CDI"
    percent_of_index: Decimal = Field(default=Decimal(100), ge=0, le=1000)
    spread_annual: Decimal = Field(default=Decimal(0), ge=-100, le=100)
    fixed_rate_annual: Decimal = Field(default=Decimal(0), ge=0, le=100)
    maturity_date: date | None = None
    pays_periodic_interest: bool = False
    notes: str | None = None


def _serialize(asset: Asset, terms: FixedIncomeTerms | None, accrual, implied: dict | None = None) -> dict:
    return {
        "ticker": asset.ticker,
        "name": asset.name,
        "kind": asset.kind,
        "terms": {
            "index_code": terms.index_code if terms else "CDI",
            "percent_of_index": terms.percent_of_index if terms else Decimal(100),
            "spread_annual": terms.spread_annual if terms else Decimal(0),
            "fixed_rate_annual": terms.fixed_rate_annual if terms else Decimal(0),
            "maturity_date": terms.maturity_date if terms else None,
            "pays_periodic_interest": terms.pays_periodic_interest if terms else False,
            "notes": terms.notes if terms else None,
        },
        "accrual": None
        if accrual is None
        else {
            "principal": accrual.principal,
            "value": accrual.value,
            "interest": accrual.interest,
            "factor": accrual.factor,
            "yield_percent": accrual.yield_percent,
            "business_days": accrual.business_days,
            "index_code": accrual.index_code,
            "through": accrual.through,
            "stale": accrual.stale,
        },
        "implied": implied,
    }


@router.get("", response_model=None, summary="Fixed income positions with their accrued value")
def list_fixed_income(db: DbSession, portfolio: CurrentPortfolio, service: PortfolioSvc) -> dict:
    ensure_terms_for_fixed_income(db)
    open_ids = {aid for aid, p in service.positions().items() if p.is_open}
    # Bank balances are fixed income too, but they live in their own tab with
    # their own movements — listing them here as papers would ask the user to
    # edit a "maturity date" for a conta corrente.
    assets = db.scalars(
        select(Asset)
        .where(Asset.kind.in_(ACCRUED_KINDS), Asset.is_cash_account.is_(False))
        .order_by(Asset.ticker)
    ).all()

    items = []
    for asset in assets:
        terms = get_terms(db, asset)
        is_open = asset.id in open_ids
        # A closed paper has no value to accrue, but its cash flows reveal the
        # rate it actually paid — which is the best hint for the open ones.
        accrual = value_position(db, asset, terms, portfolio.id) if (terms and is_open) else None
        implied = implied_percent_of_index(db, asset, terms, portfolio.id) if terms else None
        payload = _serialize(asset, terms, accrual, implied)
        payload["is_open"] = is_open
        items.append(payload)

    held = [i for i in items if i["is_open"] and i["accrual"]]
    return {
        "items": items,
        "totals": {
            "principal": sum((i["accrual"]["principal"] for i in held), Decimal(0)),
            "value": sum((i["accrual"]["value"] for i in held), Decimal(0)),
            "interest": sum((i["accrual"]["interest"] for i in held), Decimal(0)),
        },
        "indices": index_status(db),
        "available_indices": available_indices(),
    }


# ---------------------------------------------------------------------------
# Bank balances kept by hand — see app.portfolio.accounts
# ---------------------------------------------------------------------------
class AccountPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    index_code: Literal["CDI", "SELIC", "IPCA", "PRE"] = "CDI"
    percent_of_index: Decimal = Field(default=Decimal(100), ge=0, le=1000)
    opening_amount: Decimal | None = Field(default=None, ge=0)
    opening_date: date | None = None
    notes: str | None = None


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    index_code: Literal["CDI", "SELIC", "IPCA", "PRE"] | None = None
    percent_of_index: Decimal | None = Field(default=None, ge=0, le=1000)
    notes: str | None = None


class EntryPayload(BaseModel):
    amount: Decimal = Field(gt=0)
    date: date
    kind: Literal["deposit", "withdrawal"] = "deposit"


def _guard(action):
    """Turn an account rule the user broke into a 400, not a 500."""
    try:
        return action()
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/accounts", response_model=None, summary="Bank balances and their accrued interest")
def list_accounts(db: DbSession, portfolio: CurrentPortfolio) -> dict:
    return accounts_api.overview(db, portfolio.id)


@router.post("/accounts", response_model=None, summary="Register a bank balance")
def create_account(payload: AccountPayload, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    asset = _guard(
        lambda: accounts_api.create_account(
            db,
            portfolio.id,
            payload.name,
            index_code=payload.index_code,
            percent_of_index=payload.percent_of_index,
            opening_amount=payload.opening_amount,
            opening_date=payload.opening_date,
            notes=payload.notes,
        )
    )
    return accounts_api.serialize(db, portfolio.id, asset)


@router.patch("/accounts/{ticker}", response_model=None, summary="Rename a balance or change its rate")
def update_account(
    ticker: str, payload: AccountUpdate, db: DbSession, portfolio: CurrentPortfolio
) -> dict:
    asset = _guard(lambda: accounts_api.get_account(db, ticker))
    _guard(
        lambda: accounts_api.update_account(
            db,
            asset,
            name=payload.name,
            index_code=payload.index_code,
            percent_of_index=payload.percent_of_index,
            notes=payload.notes,
        )
    )
    return accounts_api.serialize(db, portfolio.id, asset)


@router.delete(
    "/accounts/{ticker}", status_code=204, response_model=None, summary="Forget a bank balance"
)
def delete_account(ticker: str, db: DbSession, portfolio: CurrentPortfolio) -> None:
    asset = _guard(lambda: accounts_api.get_account(db, ticker))
    accounts_api.delete_account(db, asset)


@router.post("/accounts/{ticker}/entries", response_model=None, summary="Record a deposit or a withdrawal")
def add_entry(
    ticker: str, payload: EntryPayload, db: DbSession, portfolio: CurrentPortfolio
) -> dict:
    asset = _guard(lambda: accounts_api.get_account(db, ticker))
    _guard(
        lambda: accounts_api.add_entry(
            db,
            portfolio.id,
            asset,
            payload.amount,
            payload.date,
            deposit=payload.kind == "deposit",
            commit=True,
        )
    )
    return accounts_api.serialize(db, portfolio.id, asset)


@router.delete(
    "/accounts/{ticker}/entries/{entry_id}", response_model=None, summary="Undo a movement"
)
def delete_entry(
    ticker: str, entry_id: int, db: DbSession, portfolio: CurrentPortfolio
) -> dict:
    asset = _guard(lambda: accounts_api.get_account(db, ticker))
    _guard(lambda: accounts_api.delete_entry(db, portfolio.id, asset, entry_id))
    return accounts_api.serialize(db, portfolio.id, asset)


@router.put("/{ticker}", response_model=None, summary="Set the yield terms of a paper")
def update_terms(ticker: str, payload: TermsPayload, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    asset = db.scalar(select(Asset).where(Asset.ticker == ticker.upper()))
    if asset is None:
        raise HTTPException(status_code=404, detail=f"asset {ticker} not found")

    terms = get_terms(db, asset, create=True)
    for field, value in payload.model_dump().items():
        setattr(terms, field, value)
    db.commit()
    db.refresh(terms)

    accrual = value_any(db, asset, terms, portfolio.id)
    return _serialize(asset, terms, accrual, implied_percent_of_index(db, asset, terms, portfolio.id))


@router.get("/{ticker}/preview", response_model=None, summary="Accrual factor for a hypothetical rate")
def preview(
    ticker: str,
    db: DbSession,
    portfolio: CurrentPortfolio,
    index_code: Literal["CDI", "SELIC", "IPCA", "PRE"] = "CDI",
    percent_of_index: Decimal = Decimal(100),
    spread_annual: Decimal = Decimal(0),
    fixed_rate_annual: Decimal = Decimal(0),
) -> dict:
    """What the position would be worth under a different rate — no writes."""
    asset = db.scalar(select(Asset).where(Asset.ticker == ticker.upper()))
    if asset is None:
        raise HTTPException(status_code=404, detail=f"asset {ticker} not found")
    hypothetical = FixedIncomeTerms(
        asset_id=asset.id,
        index_code=index_code,
        percent_of_index=percent_of_index,
        spread_annual=spread_annual,
        fixed_rate_annual=fixed_rate_annual,
    )
    accrual = value_position(db, asset, hypothetical, portfolio.id)
    return _serialize(asset, hypothetical, accrual)


@router.post("/indices/sync", response_model=None, summary="Download index series from Banco Central")
def sync_indices(db: DbSession, code: str | None = None) -> dict:
    """Refresh CDI / Selic / IPCA from the BCB SGS API (public, no key)."""
    if code:
        return sync_index(db, code)
    return sync_all_indices(db)


@router.get("/indices/status", response_model=None, summary="Stored index coverage")
def indices_status(db: DbSession) -> dict:
    return {"indices": index_status(db), "available": available_indices()}


@router.get("/indices/factor", response_model=None, summary="Accumulated index factor between two dates")
def factor(
    db: DbSession,
    start: date,
    end: date | None = None,
    index_code: Literal["CDI", "SELIC", "IPCA", "PRE"] = "CDI",
    percent_of_index: Decimal = Decimal(100),
) -> dict:
    terms = FixedIncomeTerms(
        asset_id=0,
        index_code=index_code,
        percent_of_index=percent_of_index,
        spread_annual=Decimal(0),
        fixed_rate_annual=Decimal(0),
    )
    value, days, stale = accrual_factor(db, terms, start, end)
    return {
        "index_code": index_code,
        "percent_of_index": percent_of_index,
        "start": start,
        "end": end or local_today(),
        "business_days": days,
        "factor": value,
        "variation_percent": (value - Decimal(1)) * Decimal(100),
        "stale": stale,
    }
