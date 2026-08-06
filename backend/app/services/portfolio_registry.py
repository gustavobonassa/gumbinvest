"""Portfolio bootstrap helpers (the app seeds one default portfolio)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Portfolio

DEFAULT_PORTFOLIO_NAME = "Principal"


def get_default_portfolio(db: Session) -> Portfolio:
    """Return the default portfolio, creating it on first use."""
    portfolio = db.scalar(select(Portfolio).where(Portfolio.is_default.is_(True)))
    if portfolio is None:
        portfolio = db.scalar(select(Portfolio).order_by(Portfolio.id).limit(1))
    if portfolio is None:
        portfolio = Portfolio(
            name=DEFAULT_PORTFOLIO_NAME,
            base_currency=settings.base_currency,
            is_default=True,
            description="Carteira criada automaticamente na primeira execução.",
        )
        db.add(portfolio)
        db.commit()
        db.refresh(portfolio)
    return portfolio


def get_portfolio(db: Session, portfolio_id: int | None) -> Portfolio:
    if portfolio_id is None:
        return get_default_portfolio(db)
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        return get_default_portfolio(db)
    return portfolio
