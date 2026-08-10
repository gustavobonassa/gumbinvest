"""Executes pipelines and owns their runs' lifecycle.

The same background-job bargain as ``app.services.jobs`` — POST returns at
once, the UI polls — but with the state in ``pipeline_runs`` rather than in
memory, because a scheduled run executes in the Celery worker while the poll
answers from the backend container, and because runs are history worth
keeping. Every write here opens its own short session: a browser automation
holds no transaction while it waits on a page, and the API always reads a
committed row.

The 2FA hand-off works through the same row. ``RunContext.request_input``
parks the run on ``waiting_input`` and polls for ``input_response``; the
route writes the user's code there. A scheduled run nobody is watching gets a
bell notification and, if the code never comes, times out with a message
telling the user to run it manually.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import sleep

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import AuditLog, PipelineRun
from app.db.session import session_scope
from app.importer.service import ImportResult, ImportService
from app.pipelines.base import (
    Pipeline,
    PipelineCancelled,
    PipelineError,
    PipelineInputTimeout,
    all_pipelines,
    get_pipeline,
)
from app.services.notifications import record
from app.services.portfolio_registry import get_default_portfolio

logger = get_logger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pipeline")

ACTIVE_STATUSES = ("running", "waiting_input")

#: A "running" row whose heartbeat is older than this belongs to a process
#: that died (container restart, crash). Generous because a browser step can
#: legitimately sit on one page for a while between log lines.
STALE_AFTER = timedelta(minutes=10)

#: How long a parked run waits for its 2FA code before giving up. Long enough
#: to fetch a phone; bounded so a scheduled run never blocks the worker for
#: hours.
INPUT_TIMEOUT = timedelta(minutes=15)

_WAIT_TICK = 2.0

GENERIC_ERROR = "A coleta falhou de forma inesperada. Veja o registro da execução."


class PipelineBusy(Exception):
    """This pipeline already has a live run."""


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; they were stored as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _now() -> datetime:
    return datetime.now(UTC)


def _fail_stale(db) -> None:
    """Close runs a dead process left open, so they never block a new claim."""
    cutoff = _now() - STALE_AFTER
    for run in db.scalars(select(PipelineRun).where(PipelineRun.status.in_(ACTIVE_STATUSES))).all():
        beat = _aware(run.heartbeat_at) or _aware(run.started_at)
        if beat is not None and beat < cutoff:
            run.status = "failed"
            run.finished_at = _now()
            run.error = "A execução foi interrompida (o servidor reiniciou no meio dela)."
            run.input_request = None
    # Sessions here run with autoflush off; the claim query that follows must
    # see these rows already failed, not their stale database image.
    db.flush()


def start_run(key: str, *, trigger: str = "manual", options: dict | None = None) -> int:
    """Claim a run for ``key`` and execute it on the pool. Returns the run id.

    Raises :class:`PipelineBusy` when a live run exists, ``KeyError`` for an
    unknown pipeline, :class:`PipelineError` when credentials are missing —
    all before any thread is spawned, so the caller can answer the HTTP
    request truthfully.
    """
    pipeline = get_pipeline(key)
    if not pipeline.is_configured():
        raise PipelineError("Preencha as credenciais desta automação antes de executá-la.")
    run_id = _claim(key, trigger, options)
    _EXECUTOR.submit(_execute, key, run_id)
    return run_id


def run_scheduled() -> dict:
    """The weekly slot: run every configured pipeline, one after the other.

    Sequential on purpose — two browser sessions fighting for the same 2FA
    inbox would be worse than a few extra minutes. Skips (rather than fails)
    pipelines that are unconfigured or already running, so scheduling this
    unconditionally costs nothing on an instance that never set it up.
    """
    outcome: dict[str, str] = {}
    for pipeline in all_pipelines():
        key = pipeline.spec.key
        if not pipeline.is_configured():
            outcome[key] = "not configured"
            continue
        try:
            run_id = _claim(key, "scheduled")
        except PipelineBusy:
            outcome[key] = "already running"
            continue
        _execute(key, run_id)
        with session_scope() as db:
            run = db.get(PipelineRun, run_id)
            outcome[key] = run.status if run else "unknown"
    return outcome


def _claim(key: str, trigger: str, options: dict | None = None) -> int:
    with session_scope() as db:
        _fail_stale(db)
        live = db.scalar(
            select(PipelineRun.id).where(
                PipelineRun.pipeline == key, PipelineRun.status.in_(ACTIVE_STATUSES)
            )
        )
        if live is not None:
            raise PipelineBusy()
        run = PipelineRun(
            pipeline=key,
            trigger=trigger,
            status="running",
            heartbeat_at=_now(),
            log=[],
            options=options or None,
            portfolio_id=get_default_portfolio(db).id,
        )
        db.add(run)
        db.flush()
        return run.id


def _execute(key: str, run_id: int) -> None:
    pipeline = get_pipeline(key)
    with session_scope() as db:
        run = db.get(PipelineRun, run_id)
        options = (run.options if run else None) or {}
    ctx = RunContext(run_id, pipeline, options)
    try:
        result = pipeline.run(ctx)
        _finish(run_id, pipeline, "success", result=result or {})
    except PipelineCancelled:
        _finish(run_id, pipeline, "cancelled")
    except PipelineError as exc:
        _finish(run_id, pipeline, "failed", error=str(exc))
    except Exception:  # noqa: BLE001 — a run must end with a readable error
        logger.exception("pipeline %s run %s failed", key, run_id)
        _finish(run_id, pipeline, "failed", error=GENERIC_ERROR)


def _finish(run_id: int, pipeline: Pipeline, status: str, *, result: dict | None = None, error: str | None = None) -> None:
    name = pipeline.spec.name
    with session_scope() as db:
        run = db.get(PipelineRun, run_id)
        if run is None:  # the database was wiped mid-run; nothing to report on
            return
        run.status = status
        run.finished_at = _now()
        run.heartbeat_at = _now()
        run.input_request = None
        run.input_response = None
        if result is not None:
            run.result = result
        if error is not None:
            run.error = error
        db.add(
            AuditLog(
                action=f"pipeline.{run.pipeline}",
                detail={"run_id": str(run_id), "status": status, "trigger": run.trigger},
            )
        )
        if status == "success":
            body = _result_sentence(run.result)
            record(
                db,
                kind="pipeline",
                level="success",
                title=f"{name}: coleta concluída",
                body=body,
                portfolio_id=run.portfolio_id,
                dedup_key=f"pipeline:{run_id}:done",
            )
        elif status == "failed":
            record(
                db,
                kind="pipeline",
                level="warning",
                title=f"{name}: a coleta falhou",
                body=error or "",
                portfolio_id=run.portfolio_id,
                dedup_key=f"pipeline:{run_id}:done",
            )


def _result_sentence(result: dict) -> str:
    """The counts as one sentence, or empty when the run had none."""
    imported = result.get("rows_imported")
    duplicate = result.get("rows_duplicate")
    if imported is None:
        return ""
    if not imported and duplicate:
        return f"Nenhuma movimentação nova — as {duplicate} do extrato já estavam na carteira."
    return f"{imported} movimentações novas importadas, {duplicate or 0} já conhecidas."


class RunContext:
    """What a pipeline's ``run`` gets to talk to the outside world with."""

    def __init__(self, run_id: int, pipeline: Pipeline, options: dict | None = None) -> None:
        self.run_id = run_id
        self.pipeline = pipeline
        #: Per-run knobs the trigger chose (e.g. ``{"full_history": True}``).
        self.options = options or {}

    # -- narration ----------------------------------------------------------
    def log(self, message: str, level: str = "info") -> None:
        """Append one line the UI shows live; also the cancellation checkpoint."""
        logger.info("pipeline %s: %s", self.pipeline.spec.key, message)
        with session_scope() as db:
            run = db.get(PipelineRun, self.run_id)
            if run is None:
                raise PipelineCancelled()
            if run.cancel_requested:
                raise PipelineCancelled()
            run.log = [*run.log, {"at": _now().isoformat(), "level": level, "message": message}]
            run.heartbeat_at = _now()

    def check_cancel(self) -> None:
        with session_scope() as db:
            run = db.get(PipelineRun, self.run_id)
            if run is None or run.cancel_requested:
                raise PipelineCancelled()
            run.heartbeat_at = _now()

    # -- the 2FA hand-off ---------------------------------------------------
    def request_input(self, prompt: str, *, kind: str = "code") -> str:
        """Park until the user answers ``prompt`` (or the wait times out).

        Rings the bell so a scheduled run has a chance of being answered by
        whoever is at the screen; the Automações tab shows a modal for the
        same request while it is open.
        """
        with session_scope() as db:
            run = db.get(PipelineRun, self.run_id)
            if run is None or run.cancel_requested:
                raise PipelineCancelled()
            run.status = "waiting_input"
            run.input_request = {"prompt": prompt, "kind": kind, "requested_at": _now().isoformat()}
            run.input_response = None
            run.heartbeat_at = _now()
            record(
                db,
                kind="pipeline",
                level="warning",
                title=f"{self.pipeline.spec.name} aguarda um código",
                body=f"{prompt} Abra Configurações → Automações para informá-lo.",
                portfolio_id=run.portfolio_id,
                dedup_key=f"pipeline:{self.run_id}:input:{_now().isoformat()}",
            )

        deadline = _now() + INPUT_TIMEOUT
        while _now() < deadline:
            sleep(_WAIT_TICK)
            with session_scope() as db:
                run = db.get(PipelineRun, self.run_id)
                if run is None or run.cancel_requested:
                    raise PipelineCancelled()
                run.heartbeat_at = _now()
                answer = (run.input_response or {}).get("value")
                if answer is not None:
                    run.status = "running"
                    run.input_request = None
                    run.input_response = None
                    return str(answer).strip()

        with session_scope() as db:
            run = db.get(PipelineRun, self.run_id)
            if run is not None:
                run.input_request = None
        raise PipelineInputTimeout(
            "Ninguém informou o código a tempo. Execute a coleta manualmente em "
            "Configurações → Automações, com o celular ou e-mail por perto."
        )

    # -- results ------------------------------------------------------------
    def import_file(self, payload: bytes, filename: str) -> ImportResult:
        """Feed a downloaded export to the importer — the same path an upload takes."""
        with session_scope() as db:
            portfolio = get_default_portfolio(db)
            return ImportService(db, portfolio).import_csv(payload, filename)

    def last_success_at(self) -> datetime | None:
        """When this pipeline last finished well — the incremental cursor."""
        with session_scope() as db:
            value = db.scalar(
                select(PipelineRun.finished_at)
                .where(PipelineRun.pipeline == self.pipeline.spec.key, PipelineRun.status == "success")
                .order_by(PipelineRun.finished_at.desc())
                .limit(1)
            )
            return _aware(value)

    # -- filesystem ---------------------------------------------------------
    def downloads_dir(self) -> Path:
        """Where downloaded exports are kept, inside the auto-import volume.

        On purpose: a file here is also picked up by the startup auto-import,
        so even a database restored from backup replays these exports — and
        re-importing is a no-op by the dedup invariant.
        """
        path = Path(settings.auto_import_dir) / self.pipeline.spec.key
        path.mkdir(parents=True, exist_ok=True)
        return path

    def debug_dir(self) -> Path:
        """Screenshots and page dumps for a failed browser step.

        Lives under the auto-import volume too, but as ``.png``/``.html`` the
        startup import never globs it.
        """
        path = Path(settings.auto_import_dir) / "pipelines-debug" / self.pipeline.spec.key
        path.mkdir(parents=True, exist_ok=True)
        return path


# ----------------------------------------------------------------- API surface


def run_payload(run: PipelineRun | None) -> dict | None:
    """The wire shape of one run — list rows and the poll both use it."""
    if run is None:
        return None
    return {
        "id": run.id,
        "pipeline": run.pipeline,
        "trigger": run.trigger,
        "status": run.status,
        "active": run.status in ACTIVE_STATUSES,
        "started_at": (_aware(run.started_at).isoformat() if run.started_at else None),
        "finished_at": (_aware(run.finished_at).isoformat() if run.finished_at else None),
        "log": run.log,
        "input_request": run.input_request,
        "options": run.options or {},
        "result": run.result or {},
        "error": run.error,
    }


def pipelines_payload(db) -> list[dict]:
    """Every registered pipeline with its credentials' state and latest runs."""
    _fail_stale(db)
    items: list[dict] = []
    for pipeline in all_pipelines():
        spec = pipeline.spec
        active = db.scalar(
            select(PipelineRun)
            .where(PipelineRun.pipeline == spec.key, PipelineRun.status.in_(ACTIVE_STATUSES))
            .order_by(PipelineRun.id.desc())
            .limit(1)
        )
        last = db.scalar(
            select(PipelineRun)
            .where(PipelineRun.pipeline == spec.key, PipelineRun.status.not_in(ACTIVE_STATUSES))
            .order_by(PipelineRun.id.desc())
            .limit(1)
        )
        items.append(
            {
                "key": spec.key,
                "name": spec.name,
                "description": spec.description,
                "schedule": spec.schedule,
                "configured": pipeline.is_configured(),
                "credentials": [
                    {"key": key, "label": label, "configured": bool(getattr(settings, key, ""))}
                    for key, label in spec.credentials
                ],
                "active_run": run_payload(active),
                "last_run": run_payload(last),
            }
        )
    return items
