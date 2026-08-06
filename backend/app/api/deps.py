"""Shared FastAPI dependencies."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from app.db.models import Portfolio
from app.db.session import get_db
from app.portfolio.service import PortfolioService
from app.services.portfolio_registry import get_portfolio

DbSession = Annotated[Session, Depends(get_db)]


def current_portfolio(
    db: DbSession, portfolio_id: Annotated[int | None, Query(description="Portfolio id")] = None
) -> Portfolio:
    return get_portfolio(db, portfolio_id)


CurrentPortfolio = Annotated[Portfolio, Depends(current_portfolio)]


def portfolio_service(db: DbSession, portfolio: CurrentPortfolio) -> PortfolioService:
    return PortfolioService(db, portfolio.id)


PortfolioSvc = Annotated[PortfolioService, Depends(portfolio_service)]
