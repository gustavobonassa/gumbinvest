"""Automated collectors: list, trigger, watch, answer, cancel.

Thin by design — the lifecycle lives in ``app.pipelines.runner``, and this
module only translates it to HTTP. The one endpoint with a story is
``/runs/{id}/input``: it is the other half of ``RunContext.request_input``,
writing the 2FA code into the row the parked worker thread is polling.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DbSession
from app.db.models import AuditLog, PipelineRun
from app.pipelines.base import PipelineError
from app.pipelines.runner import (
    ACTIVE_STATUSES,
    PipelineBusy,
    pipelines_payload,
    run_payload,
    start_run,
)

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


@router.get("", response_model=None, summary="Every automated collector and its current state")
def list_pipelines(db: DbSession) -> dict:
    payload = {"pipelines": pipelines_payload(db)}
    # The payload may have just failed a stale run (a crashed process's
    # leftover). Request sessions never commit on their own, and a cleanup
    # that only exists inside one response would be redone on every poll.
    db.commit()
    return payload


@router.get("/runs", response_model=None, summary="Past executions, newest first")
def list_runs(
    db: DbSession,
    pipeline: str | None = Query(None, description="Filter by pipeline key"),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    stmt = select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)
    if pipeline:
        stmt = stmt.where(PipelineRun.pipeline == pipeline)
    return {"runs": [run_payload(run) for run in db.scalars(stmt).all()]}


class RunOptions(BaseModel):
    #: Pull the whole available history (a first-time backfill) rather than just
    #: what is new since the last run.
    full_history: bool = False


@router.post("/{key}/run", response_model=None, summary="Start a collection now")
def trigger(key: str, db: DbSession, payload: RunOptions | None = Body(default=None)) -> dict:
    options = {"full_history": bool(payload and payload.full_history)}
    try:
        run_id = start_run(key, trigger="manual", options=options)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="automação desconhecida") from exc
    except PipelineBusy as exc:
        raise HTTPException(status_code=409, detail="esta automação já está em execução") from exc
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(AuditLog(action=f"pipeline.{key}.start", detail={"run_id": str(run_id), **options}))
    db.commit()
    return {"run_id": run_id}


class InputPayload(BaseModel):
    value: str


@router.post("/runs/{run_id}/input", response_model=None, summary="Answer a run waiting for a code")
def answer(run_id: int, payload: InputPayload, db: DbSession) -> dict:
    run = db.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="execução não encontrada")
    if run.status != "waiting_input":
        raise HTTPException(status_code=409, detail="esta execução não está esperando um código")
    run.input_response = {"value": payload.value.strip()}
    # Deliberately not audited: the value is a one-time code, but the habit of
    # never logging anything typed into a credential-shaped field is cheaper
    # than deciding case by case.
    db.commit()
    return {"accepted": True}


@router.post("/runs/{run_id}/cancel", response_model=None, summary="Ask a live run to stop")
def cancel(run_id: int, db: DbSession) -> dict:
    run = db.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="execução não encontrada")
    if run.status not in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="esta execução já terminou")
    run.cancel_requested = True
    db.add(AuditLog(action=f"pipeline.{run.pipeline}.cancel", detail={"run_id": str(run_id)}))
    db.commit()
    return {"cancelling": True}
