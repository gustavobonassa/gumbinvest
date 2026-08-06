"""The universe ingest's run block: progress, cancellation, and the lock.

State lives in one ``app_settings`` row rather than in memory because the
processes that need it are not the same process. In Docker the Celery worker
runs the ingest and the API container answers the status poll; a
``threading.Event`` cannot carry a cancel across that boundary and a dict cannot
survive a restart. A row can do both, and ``app_settings`` already exists as a
generic JSON key/value store, so this needs no table of its own.

There is deliberately no startup recovery hook. A run that dies — the desktop
app closed, the container was killed — leaves ``state="running"`` behind with a
heartbeat that stops advancing, and :func:`read` reports that as *stale*. The UI
shows "interrompida — retomar", the next start is allowed, and no phantom lock
can wedge the feature. That is also what neutralises a ``.gumbinvest`` import
taken mid-run: it restores a heartbeat hours old, which reads as stale rather
than as a run in progress.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AppSetting

#: The ``app_settings`` key holding the block.
KEY = "universe_ingest"

#: A run whose heartbeat is older than this is presumed dead. Generous on
#: purpose: a slow COTAHIST parse must not be declared dead while it works.
STALE_AFTER = timedelta(minutes=3)

#: Terminal states — a new run may start over any of them.
FINISHED = frozenset({"idle", "done", "cancelled", "error", "paused"})

#: The stages, in order. The ingest resumes at the first one not yet done, so
#: this tuple is the resume cursor's alphabet; renaming an entry restarts it.
#:
#: ``prices`` comes first because COTAHIST is both the price source and the
#: identity source for B3 — it lists every instrument that actually traded,
#: with its ISIN and its instrument family, which is a better roster than any
#: registry (a paper listed but never traded is not worth screening).
STAGES: tuple[tuple[str, str], ...] = (
    ("prices", "Baixando preços e liquidez da B3"),
    ("registry", "Cruzando com os registros da B3 e da CVM"),
    ("fundamentals", "Lendo os balanços das empresas (CVM)"),
    ("funds", "Lendo os informes dos FIIs (CVM)"),
    ("us", "Listando os ativos dos EUA (SEC)"),
)

STAGE_LABELS = dict(STAGES)


def _now() -> datetime:
    return datetime.now(UTC)


#: Runs kept for the jobs list. Enough to see whether last night worked.
HISTORY_LIMIT = 8


def _blank() -> dict[str, Any]:
    return {
        "run_id": None,
        "state": "idle",
        "stage": None,
        "message": None,
        "processed": 0,
        "total": 0,
        "started_at": None,
        "heartbeat_at": None,
        "finished_at": None,
        "requested_cancel": False,
        "markets": [],
        "stages_done": [],
        "stage_rows": {},
        "warnings": [],
        #: Per-stage timings for the run in progress:
        #: {name: {started_at, finished_at, seconds, rows, state}}
        "stage_timings": {},
        #: Seconds each stage took the last time it succeeded. This is the only
        #: basis for an estimate — a countdown invented before the work has
        #: ever been measured is decoration, not information.
        "stage_baseline": {},
        #: Finished runs, newest first.
        "history": [],
    }


def _aware(value: object) -> datetime | None:
    """Parse a stored ISO timestamp; naive values are read as UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read(db: Session) -> dict[str, Any]:
    """The run block, with ``stale`` and ``active`` derived for the caller.

    ``active`` is the honest answer to "is something running right now": a
    ``running`` state whose heartbeat has gone quiet is not active, it is
    wreckage, and treating it as active would lock the feature forever.
    """
    block = {**_blank(), **_stored(db)}

    heartbeat = _aware(block.get("heartbeat_at"))
    running = block.get("state") == "running"
    stale = bool(running and (heartbeat is None or _now() - heartbeat > STALE_AFTER))
    block["stale"] = stale
    block["active"] = running and not stale
    block["stage_label"] = STAGE_LABELS.get(block.get("stage") or "", None)
    block["stage_count"] = len(STAGES)
    block["stage_index"] = len(block.get("stages_done") or [])
    block["eta_seconds"] = _eta_seconds(block)
    block["elapsed_seconds"] = _elapsed(block)
    block["jobs"] = jobs_view(block)
    return block


def _elapsed(block: dict[str, Any]) -> float | None:
    """How long the run has been going, or how long it took."""
    started = _aware(block.get("started_at"))
    if started is None:
        return None
    end = _aware(block.get("finished_at")) if block.get("state") != "running" else _now()
    return round(((end or _now()) - started).total_seconds(), 1)


#: Derived on read, never stored.
_DERIVED = (
    "stale",
    "active",
    "stage_label",
    "stage_count",
    "stage_index",
    "eta_seconds",
    "elapsed_seconds",
    "jobs",
)


def write(db: Session, block: dict[str, Any], *, claim: bool = False) -> None:
    """Persist the block. The caller commits — this may run inside a batch.

    Two things an ordinary write must not do, both of which cost a bug:

    * **Clobber a cancel.** ``requested_cancel`` is set by an API request in
      another process, between two of the worker's heartbeats. Writing the
      worker's in-memory copy back verbatim erases it, and the cancel button
      appears to work while the run carries on regardless.
    * **Overwrite a newer run.** Once another run has claimed the block, this
      one is superseded and has no business writing to it.

    ``claim=True`` is how :func:`start` takes the block over — the one write
    that is *supposed* to replace whatever was there.
    """
    stored = _stored(db)
    if not claim and stored and block.get("run_id") and stored.get("run_id") != block.get("run_id"):
        return
    payload = {key: value for key, value in block.items() if key not in _DERIVED}
    if not claim and stored.get("requested_cancel"):
        payload["requested_cancel"] = True
    db.merge(AppSetting(key=KEY, value=payload))


def _stored(db: Session) -> dict[str, Any]:
    """The block exactly as persisted — no defaults, no derived fields."""
    row = db.get(AppSetting, KEY)
    if row is not None:
        db.refresh(row)
    return row.value if row is not None and isinstance(row.value, dict) else {}


def start(db: Session, markets: list[str]) -> dict[str, Any]:
    """Claim the run. Returns the fresh block; raises if one is already active."""
    current = read(db)
    if current["active"]:
        raise AlreadyRunning("Já existe uma atualização do universo em andamento.")
    block = _blank()
    block.update(
        run_id=str(uuid.uuid4()),
        state="running",
        markets=list(markets),
        started_at=_now().isoformat(),
        heartbeat_at=_now().isoformat(),
        message="Preparando…",
        # A fresh run, but not a fresh installation: what earlier runs measured
        # is what lets this one estimate, and the history is what the jobs tab
        # lists. Only the in-flight fields start empty.
        stage_baseline=dict(current.get("stage_baseline") or {}),
        history=list(current.get("history") or []),
    )
    write(db, block, claim=True)
    db.commit()
    return block


class AlreadyRunning(RuntimeError):
    """A run is in progress; ``str(exc)`` is a pt-BR message for the user."""


def heartbeat(
    db: Session,
    block: dict[str, Any],
    *,
    message: str | None = None,
    processed: int | None = None,
    total: int | None = None,
    commit: bool = True,
) -> bool:
    """Advance progress and report whether a cancel has been requested.

    Returning the cancel flag from the same call that writes progress is what
    keeps the worker loop honest: it cannot update the bar without also asking
    whether it should stop.
    """
    block["heartbeat_at"] = _now().isoformat()
    if message is not None:
        block["message"] = message
    if processed is not None:
        block["processed"] = processed
    if total is not None:
        block["total"] = total
    # Read the flag before writing, so the in-memory copy carries it and the
    # write cannot erase a cancel that arrived since the last heartbeat.
    stop = cancel_requested(db, block)
    block["requested_cancel"] = stop
    write(db, block)
    if commit:
        db.commit()
    return stop


def cancel_requested(db: Session, block: dict[str, Any]) -> bool:
    """Has someone asked this run to stop? Re-read, because they are elsewhere.

    The cancel is written by an API request in another process, so the answer
    cannot come from the in-memory copy — that is the whole reason this state
    is in the database.
    """
    stored = _stored(db)
    if stored.get("run_id") != block.get("run_id"):
        # Another run took over; this one has been superseded and should stop.
        return True
    return bool(stored.get("requested_cancel"))


def request_cancel(db: Session) -> bool:
    """Ask the running ingest to stop. False when there was nothing to stop."""
    block = read(db)
    if not block["active"]:
        return False
    block["requested_cancel"] = True
    block["message"] = "Cancelando…"
    write(db, block)
    db.commit()
    return True


def stage_started(db: Session, block: dict[str, Any], stage: str) -> None:
    block["stage"] = stage
    block["message"] = STAGE_LABELS.get(stage, stage)
    block["processed"] = 0
    block["total"] = 0
    timings = dict(block.get("stage_timings") or {})
    timings[stage] = {
        "started_at": _now().isoformat(),
        "finished_at": None,
        "seconds": None,
        "rows": 0,
        "state": "running",
    }
    block["stage_timings"] = timings
    heartbeat(db, block)


def stage_done(
    db: Session, block: dict[str, Any], stage: str, rows: int, *, state: str = "done"
) -> None:
    done = list(block.get("stages_done") or [])
    if stage not in done:
        done.append(stage)
    block["stages_done"] = done
    block["stage_rows"] = {**(block.get("stage_rows") or {}), stage: rows}

    timings = dict(block.get("stage_timings") or {})
    entry = dict(timings.get(stage) or {})
    started = _aware(entry.get("started_at"))
    finished = _now()
    seconds = round((finished - started).total_seconds(), 1) if started else None
    entry.update(finished_at=finished.isoformat(), seconds=seconds, rows=rows, state=state)
    timings[stage] = entry
    block["stage_timings"] = timings

    # Only a stage that actually did its work becomes the estimate for next
    # time. A skipped or failed stage returns in a second and would make the
    # next run promise a finish it cannot keep.
    if state == "done" and seconds is not None and rows > 0:
        block["stage_baseline"] = {**(block.get("stage_baseline") or {}), stage: seconds}
    heartbeat(db, block)


#: Items a stage must have finished before its own rate is extrapolated.
#:
#: One is not enough, and the reason is concrete: the price stage downloads one
#: 89 MB annual file followed by eight ~10 MB monthly ones. Projecting the
#: whole stage from the first item announced twelve minutes remaining on a run
#: that took two — a number nobody should have been shown while deciding
#: whether to wait.
MIN_SAMPLES_FOR_RATE = 3


def _eta_seconds(block: dict[str, Any]) -> float | None:
    """Seconds left, from measurement rather than assumption.

    Preference order for the running stage: how long it took last time, then
    its own observed rate once several items have gone through. Stages after it
    are counted only where a previous run measured them. Returns None until
    there is evidence for any of that — "—" is a better answer than a
    confidently wrong countdown.
    """
    if block.get("state") != "running":
        return None
    baseline: dict[str, float] = block.get("stage_baseline") or {}
    done = set(block.get("stages_done") or [])
    current = block.get("stage")
    remaining = 0.0
    known = False

    if current:
        entry = (block.get("stage_timings") or {}).get(current) or {}
        started = _aware(entry.get("started_at"))
        elapsed = (_now() - started).total_seconds() if started else None
        processed = block.get("processed") or 0
        total = block.get("total") or 0

        by_rate: float | None = None
        if elapsed is not None and processed >= MIN_SAMPLES_FOR_RATE and total > processed:
            by_rate = elapsed / processed * (total - processed)

        if current in baseline and elapsed is not None:
            # A whole-stage measurement beats an in-flight extrapolation, but
            # not once the stage has visibly outrun it.
            by_baseline = max(baseline[current] - elapsed, 0.0)
            remaining += max(by_baseline, by_rate) if by_rate is not None else by_baseline
            known = True
        elif by_rate is not None:
            remaining += by_rate
            known = True

    for name, _label in STAGES:
        if name in done or name == current:
            continue
        if name in baseline:
            remaining += baseline[name]
            known = True

    return round(remaining, 1) if known else None


def jobs_view(block: dict[str, Any]) -> list[dict[str, Any]]:
    """The stage list for the jobs tab: what each one is and where it stands."""
    timings = block.get("stage_timings") or {}
    baseline = block.get("stage_baseline") or {}
    done = set(block.get("stages_done") or [])
    current = block.get("stage") if block.get("state") == "running" else None

    jobs: list[dict[str, Any]] = []
    for name, label in STAGES:
        entry = timings.get(name) or {}
        # Finished wins over current: ``block["stage"]`` still names the last
        # stage until the next one starts, so asking "is this the current
        # stage" first would leave a completed job showing as running.
        if name in done:
            status = entry.get("state") or "done"
        elif name == current:
            status = "running"
        else:
            status = "pending"
        running_now = status == "running"
        processed = block.get("processed") if running_now else entry.get("rows")
        jobs.append(
            {
                "name": name,
                "label": label,
                "status": status,
                "rows": entry.get("rows") or 0,
                "seconds": entry.get("seconds"),
                "expected_seconds": baseline.get(name),
                "started_at": entry.get("started_at"),
                "finished_at": entry.get("finished_at"),
                "processed": processed if running_now else None,
                "total": block.get("total") if running_now else None,
                "message": block.get("message") if running_now else None,
            }
        )
    return jobs


def warn(db: Session, block: dict[str, Any], message: str) -> None:
    """Record something the user should see but that does not stop the run.

    A source that changed shape, a market skipped for a missing User-Agent, a
    ticker dropped for a collision: visible, not swallowed.
    """
    warnings = list(block.get("warnings") or [])
    if message not in warnings:
        warnings.append(message)
    block["warnings"] = warnings[:20]
    heartbeat(db, block)


def finish(db: Session, block: dict[str, Any], state: str, message: str | None = None) -> None:
    """Close the run and file it in the history the jobs tab lists."""
    finished = _now()
    block["state"] = state
    block["finished_at"] = finished.isoformat()
    block["heartbeat_at"] = finished.isoformat()
    block["requested_cancel"] = False
    block["stage"] = None
    if message is not None:
        block["message"] = message

    started = _aware(block.get("started_at"))
    history = [
        entry for entry in (block.get("history") or []) if entry.get("run_id") != block.get("run_id")
    ]
    history.insert(
        0,
        {
            "run_id": block.get("run_id"),
            "state": state,
            "message": block.get("message"),
            "markets": list(block.get("markets") or []),
            "started_at": block.get("started_at"),
            "finished_at": finished.isoformat(),
            "seconds": round((finished - started).total_seconds(), 1) if started else None,
            "rows": sum((block.get("stage_rows") or {}).values()),
            "stage_rows": dict(block.get("stage_rows") or {}),
            "warnings": list(block.get("warnings") or []),
        },
    )
    block["history"] = history[:HISTORY_LIMIT]
    write(db, block)
    db.commit()


def reset(db: Session) -> None:
    """Forget every run: progress, timings, baseline and history.

    Called when the universe is emptied. Keeping a baseline measured against
    rows that no longer exist would make the next run promise a finish time
    for work it has to redo from nothing. The caller commits.
    """
    write(db, _blank(), claim=True)


def is_enabled(db: Session) -> bool:
    """Whether the user has switched the universe on in Configurações."""
    row = db.get(AppSetting, "universe_enabled")
    if row is None:
        return False
    value = row.value
    if isinstance(value, dict):
        value = value.get("value")
    return bool(value)


def markets(db: Session) -> list[str]:
    """Which markets the user wants ingested; B3 when unset."""
    row = db.get(AppSetting, "universe_markets")
    value = row.value if row is not None else None
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, list) and value:
        return [str(item).upper() for item in value]
    return ["B3"]


def history_years(db: Session) -> int:
    """How many COTAHIST year-files to reduce. Clamped to something sane."""
    row = db.get(AppSetting, "universe_history_years")
    value = row.value if row is not None else None
    if isinstance(value, dict):
        value = value.get("value")
    try:
        years = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        years = 2
    return max(1, min(years, 5))


def all_settings(db: Session) -> dict[str, Any]:
    """The three universe settings in one read, for the status endpoint."""
    from app.market.universe.sources import sec

    return {
        "enabled": is_enabled(db),
        "markets": markets(db),
        "history_years": history_years(db),
        # Not a secret — a contact string, shown so the field round-trips.
        "sec_user_agent": sec.resolve_user_agent(db),
    }


def stages_public() -> list[dict[str, str]]:
    """The stage list for the UI, so progress can be shown before a run starts."""
    return [{"name": name, "label": label} for name, label in STAGES]
