"""Aggregates every route module under /api."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    ai,
    ai_wallet,
    assets,
    cloud_backup,
    corporate_actions,
    fixed_income,
    imports,
    investors,
    misc,
    pipelines,
    portfolio,
    reports,
    smart_invest,
    transactions,
    treasury,
    universe,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(misc.router)
api_router.include_router(portfolio.router)
api_router.include_router(assets.router)
api_router.include_router(transactions.router)
api_router.include_router(imports.router)
api_router.include_router(pipelines.router)
api_router.include_router(cloud_backup.router)
api_router.include_router(reports.router)
api_router.include_router(fixed_income.router)
api_router.include_router(treasury.router)
api_router.include_router(corporate_actions.router)
api_router.include_router(ai.router)
api_router.include_router(ai_wallet.router)
api_router.include_router(smart_invest.router)
api_router.include_router(investors.router)
api_router.include_router(universe.router)
