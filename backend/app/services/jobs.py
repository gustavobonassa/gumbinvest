"""In-process background jobs with polled status.

AI-driven operations take minutes (web search, per-ticker verification), and
tying them to an open HTTP response meant a tab switch aborted the run. The
pattern used app-wide instead: POST starts the job on a worker pool and
returns at once, the UI polls a status endpoint, and the run finishes whether
or not anyone is watching. One live job per key — the registry doubles as the
lock. Works identically under Docker (single uvicorn process) and the desktop
server.
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Callable, Hashable

from app.services.ai_research import AiResearchError

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="bg-job")


class JobConflict(Exception):
    """A job is already running for this key."""


@dataclass
class BackgroundJob:
    id: str
    kind: str
    status: str | None = None
    error: str | None = None
    result: dict | None = None
    done: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def job_payload(job: BackgroundJob | None) -> dict:
    """The wire shape every job-status endpoint returns (see AiWalletJob)."""
    if job is None:
        return {
            "active": False,
            "id": None,
            "kind": None,
            "status": None,
            "error": None,
            "result": None,
            "finished_at": None,
        }
    return {
        "active": not job.done,
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "error": job.error,
        "result": _jsonable(job.result) if job.result is not None else None,
        "finished_at": job.finished_at,
    }


class JobRegistry:
    """One live job per key; a finished job stays readable for ``retention``.

    The lingering result is the user's feedback (errors, skips), but not
    forever — an hour-old result reappearing would only confuse.
    """

    def __init__(self, retention: timedelta = timedelta(hours=1)) -> None:
        self._jobs: dict[Hashable, BackgroundJob] = {}
        self._lock = threading.Lock()
        self._retention = retention

    def current(self, key: Hashable) -> BackgroundJob | None:
        with self._lock:
            job = self._jobs.get(key)
            if (
                job
                and job.done
                and job.finished_at
                and datetime.now(UTC) - job.finished_at > self._retention
            ):
                self._jobs.pop(key, None)
                return None
            return job

    def start(
        self,
        key: Hashable,
        kind: str,
        runner: Callable[[BackgroundJob], None],
        *,
        error_message: str,
        logger,
    ) -> BackgroundJob:
        """Run ``runner(job)`` on the pool; raises :class:`JobConflict` if busy.

        The runner reports failures by setting ``job.error``; anything it
        raises is caught here — an :class:`AiResearchError` surfaces its own
        (pt-BR) message, everything else the generic ``error_message``.
        """
        with self._lock:
            current = self._jobs.get(key)
            if current and not current.done:
                raise JobConflict()
            job = BackgroundJob(id=str(uuid.uuid4()), kind=kind, status="Iniciando…")
            self._jobs[key] = job

        def guarded() -> None:
            try:
                runner(job)
            except AiResearchError as exc:
                job.error = str(exc)
            except Exception:  # noqa: BLE001 — a job must end with a readable error
                logger.exception("background job failed (%s)", kind)
                job.error = error_message
            finally:
                job.done = True
                job.finished_at = datetime.now(UTC)

        _EXECUTOR.submit(guarded)
        return job
