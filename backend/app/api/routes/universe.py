"""HTTP surface for the asset universe: the ingest, its progress, the screener.

The ingest is started, not awaited. Downloading and reducing a year of B3 prices
plus the CVM filings takes tens of seconds, which is far too long to hold a
request open, so POST claims the run and returns; the UI polls ``/status``. Same
contract as the AI wallet's jobs, with one difference that matters: the lock and
the progress live in the database rather than in a process-local dict, so a
cancel issued against one worker reaches a run executing in another.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from app.api.deps import CurrentPortfolio, DbSession
from app.db.models import AssetUniverse, AuditLog
from app.market.universe import ingest, state
from app.services import universe as screener

router = APIRouter(prefix="/universe", tags=["universe"])


class IngestPayload(BaseModel):
    markets: list[str] | None = Field(default=None, max_length=4)


def _status_payload(db: DbSession) -> dict:
    block = state.read(db)
    return {
        **block,
        "settings": state.all_settings(db),
        "stages": state.stages_public(),
        "coverage": screener.coverage(db),
    }


@router.get("/status", response_model=None, summary="Universe ingest status and coverage")
def status(db: DbSession) -> dict:
    return _status_payload(db)


@router.post("/ingest", response_model=None, summary="Start the universe ingest")
def start_ingest(payload: IngestPayload, db: DbSession) -> dict:
    if not state.is_enabled(db):
        raise HTTPException(
            status_code=409,
            detail="O universo de ativos está desativado — ative-o em Configurações → Dados.",
        )
    markets = [m.upper() for m in (payload.markets or state.markets(db))]
    unknown = [m for m in markets if m not in ("B3", "US")]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Mercado desconhecido: {', '.join(unknown)}")
    try:
        ingest.start_background(db, markets)
    except state.AlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.add(AuditLog(action="universe.ingest", detail={"markets": markets}))
    db.commit()
    return _status_payload(db)


@router.post("/ingest/cancel", response_model=None, summary="Cancel the running ingest")
def cancel_ingest(db: DbSession) -> dict:
    if not state.request_cancel(db):
        raise HTTPException(status_code=409, detail="Nenhuma atualização em andamento.")
    return _status_payload(db)


@router.delete("", response_model=None, summary="Delete every row in the asset universe")
def clear_universe(db: DbSession) -> dict:
    """Empty the table and forget the run history.

    Safe by construction: this holds only public data downloaded from B3 and
    the CVM, so the cost of deleting it is one ingest. Nothing of the user's is
    stored here — no positions, no transactions, no settings — which is also
    why the ``.gumbinvest`` export leaves it alone.
    """
    if state.read(db)["active"]:
        raise HTTPException(
            status_code=409,
            detail="Há uma atualização em andamento — cancele-a antes de apagar os dados.",
        )
    removed = db.scalar(select(func.count()).select_from(AssetUniverse)) or 0
    db.execute(sa_delete(AssetUniverse))
    state.reset(db)
    db.add(AuditLog(action="universe.cleared", detail={"rows": removed}))
    db.commit()
    return {**_status_payload(db), "removed": removed}


@router.get("", response_model=None, summary="Screen the asset universe")
def screen(
    db: DbSession,
    market: str | None = Query(default=None),
    kind: list[str] | None = Query(default=None),
    sector: str | None = Query(default=None),
    index: str | None = Query(default=None, max_length=12),
    text: str | None = Query(default=None, max_length=60),
    order_by: str = Query(default="valor_de_mercado"),
    descending: bool = Query(default=True),
    only_active: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    min_dy: float | None = Query(default=None, ge=0),
    max_pe: float | None = Query(default=None, gt=0),
    max_pb: float | None = Query(default=None, gt=0),
    min_roe: float | None = Query(default=None),
    min_volume: float | None = Query(default=None, ge=0),
) -> dict:
    """One page of screened rows, plus what the filters had to drop."""
    filters = []
    for field, op, value in (
        ("dividend_yield_pct", "gte", min_dy),
        ("p_l", "lte", max_pe),
        ("p_vp", "lte", max_pb),
        ("roe_pct", "gte", min_roe),
        ("liquidez_media_diaria", "gte", min_volume),
    ):
        if value is not None:
            filters.append(screener.Filter(field=field, op=op, value=value))
    try:
        request = screener.ScreenRequest(
            market=market,
            kinds=frozenset(kind) if kind else None,
            sector=sector,
            index=index,
            text=text,
            filters=tuple(filters),
            order_by=order_by,
            descending=descending,
            only_active=only_active,
            limit=limit,
            offset=offset,
        )
        result = screener.screen(db, request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "items": result.rows,
        "total": result.total,
        "dropped_for_missing_data": result.dropped_for_missing_data,
        "stalest_fundamentals_at": result.stalest_fundamentals_at,
        "fields": screener.screenable_fields(),
    }


@router.get("/sectors", response_model=None, summary="Sectors present in the universe")
def sectors(db: DbSession) -> list[dict]:
    rows = db.execute(
        select(AssetUniverse.sector, func.count())
        .where(AssetUniverse.sector.is_not(None))
        .group_by(AssetUniverse.sector)
        .order_by(func.count().desc())
    ).all()
    return [{"sector": sector, "count": count} for sector, count in rows]


@router.get("/fit", response_model=None, summary="How this portfolio compares to the market")
def fit(db: DbSession, portfolio: CurrentPortfolio) -> dict:
    return screener.portfolio_fit(db, portfolio.id)
