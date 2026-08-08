"""In-process replacement for the Celery beat schedule.

Mirrors ``app.workers.celery_app`` job for job, calling the same underlying
functions the Celery tasks wrap — no route ever enqueues a task, so nothing
else is needed. APScheduler rather than a hand-rolled loop because a desktop
machine sleeps: ``coalesce`` + ``misfire_grace_time`` make a laptop waking at
10:00 run the missed morning jobs once each instead of skipping them.

Running in-process is also strictly better for the replay cache in
``app.portfolio.service`` — the scheduler's writes invalidate it, which a
separate worker process never could.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import AuditLog
from app.db.session import session_scope
from app.services.backup import catch_up_backup, run_scheduled_backup
from app.services.portfolio_registry import get_default_portfolio

logger = get_logger(__name__)

_JOB_DEFAULTS = {"coalesce": True, "misfire_grace_time": 3600, "max_instances": 1}


def _audited(action: str, fn) -> None:
    """Run one job in its own session, recording the same audit row Celery would."""
    try:
        with session_scope() as db:
            result = fn(db)
            detail = {k: str(v) for k, v in result.items()} if isinstance(result, dict) else {}
            db.add(AuditLog(action=action, detail=detail))
    except Exception:  # noqa: BLE001 — one failed job must not kill the schedule
        logger.exception("scheduled job %s failed", action)


def _refresh_quotes(db) -> dict:
    from app.market.service import refresh_quotes

    return refresh_quotes(db, get_default_portfolio(db).id)


def _retry_quotes() -> None:
    """Drain the quote retry queue.

    Not wrapped in :func:`_audited`: this runs every minute, and an audit row
    per minute saying "nothing was due" would drown the log. One is written
    only when a retry actually recovered a price.
    """
    from app.market.service import retry_pending_quotes

    try:
        with session_scope() as db:
            result = retry_pending_quotes(db)
            if result.get("recovered"):
                db.add(AuditLog(action="market.retry", detail={k: str(v) for k, v in result.items()}))
    except Exception:  # noqa: BLE001 — one failed job must not kill the schedule
        logger.exception("scheduled job market.retry failed")


def _sync_splits(db) -> dict:
    from app.market.service import sync_splits

    return sync_splits(db, get_default_portfolio(db).id)


def _backfill_missing(db) -> dict:
    from app.market.service import backfill_history

    return backfill_history(db, get_default_portfolio(db).id, only_missing=True)


def _heal_market_data() -> None:
    """Re-fetch series a failed bootstrap left empty (see heal_market_data).

    Like the quote retry, it is not `_audited`: it runs every half hour and is
    almost always a no-op — an audit row is only worth writing when something
    was actually missing and got fetched.
    """
    from app.market.service import heal_market_data

    try:
        with session_scope() as db:
            healed = heal_market_data(db)
            if healed:
                db.add(AuditLog(action="market.heal", detail={k: str(v) for k, v in healed.items()}))
    except Exception:  # noqa: BLE001 — one failed job must not kill the schedule
        logger.exception("scheduled job market.heal failed")


def _sync_indices(db) -> dict:
    from app.market.fixed_income import ensure_terms_for_fixed_income
    from app.market.indices import sync_all_indices

    ensure_terms_for_fixed_income(db)
    return sync_all_indices(db)


def _sync_benchmarks(db) -> dict:
    from app.market.benchmarks import sync_benchmarks

    return sync_benchmarks(db)


def _sync_fx(db) -> dict:
    from app.market.crypto import sync_crypto_fx
    from app.market.fx import backfill_transaction_fx, sync_all_fx

    result = sync_all_fx(db)
    result["crypto"] = sync_crypto_fx(db)["points"]
    result["backfilled"] = backfill_transaction_fx(db)["updated"]
    return result


def _sync_treasury(db) -> dict:
    from app.market.treasury import sync_treasury_prices

    return sync_treasury_prices(db)


def _refresh_fundamentals(db) -> dict:
    from app.market.fundamentals import refresh_held_fundamentals

    return refresh_held_fundamentals(db, get_default_portfolio(db).id, only_stale=True)


def _rebuild_snapshots(db) -> dict:
    from app.portfolio.service import PortfolioService

    return {"points": PortfolioService(db, get_default_portfolio(db).id).rebuild_snapshots()}


def _snapshot_ai_wallets(db) -> dict:
    from app.services.ai_wallet import snapshot_ai_wallets

    return snapshot_ai_wallets(db)


def _sync_universe() -> dict:
    """Refresh the asset universe, if the user has switched it on.

    Opens its own sessions rather than taking one from ``_audited``: a run
    spans several downloads and must never hold a transaction across them,
    which on SQLite would park the writer lock while the UI polls.
    """
    from app.db.session import session_scope
    from app.market.universe import ingest, state

    with session_scope() as db:
        if not state.is_enabled(db) or state.read(db)["active"]:
            return {"skipped": True}
    result = ingest.run_ingest(budget_seconds=900)
    with session_scope() as db:
        db.add(
            AuditLog(
                action="universe.sync",
                detail={"state": result.get("state"), "message": result.get("message")},
            )
        )
        db.commit()
    return {"state": result.get("state")}


def _hhmm(value: str, fallback: tuple[int, int]) -> tuple[int, int]:
    try:
        hour, minute = value.split(":")
        return int(hour), int(minute)
    except (AttributeError, ValueError):
        return fallback


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone, job_defaults=_JOB_DEFAULTS)

    def cron(hour: int, minute: int) -> CronTrigger:
        return CronTrigger(hour=hour, minute=minute, timezone=settings.timezone)

    scheduler.add_job(
        lambda: _audited("market.refresh", _refresh_quotes),
        IntervalTrigger(minutes=max(settings.price_refresh_minutes, 1)),
        # An interval job first fires after one full interval — on a fresh
        # install that means the just-imported portfolio sits at cost for half
        # an hour. Fire once shortly after boot instead (after the startup
        # auto-import has had time to create the assets).
        next_run_time=datetime.now() + timedelta(seconds=90),
        id="refresh-quotes",
    )
    scheduler.add_job(_retry_quotes, IntervalTrigger(minutes=1), id="retry-quotes")
    # A cold start whose downloads timed out heals within the half hour rather
    # than waiting for the next day's scheduled slot.
    scheduler.add_job(
        _heal_market_data,
        IntervalTrigger(minutes=30),
        next_run_time=datetime.now() + timedelta(minutes=3),
        id="heal-market-data",
    )
    # Same rationale as the beat schedule (see app/workers/celery_app.py):
    # new tickers need their history; BC publishes CDI in the morning; PTAX
    # settles by early afternoon; benchmarks after the B3 close.
    scheduler.add_job(lambda: _audited("market.backfill", _backfill_missing), cron(6, 40), id="backfill-new-assets")
    scheduler.add_job(lambda: _audited("fundamentals.refresh", _refresh_fundamentals), cron(8, 5), id="refresh-fundamentals")
    scheduler.add_job(
        lambda: _audited("market.splits", _sync_splits),
        CronTrigger(day_of_week="sat", hour=7, minute=10, timezone=settings.timezone),
        id="sync-splits",
    )
    scheduler.add_job(lambda: _audited("market.indices", _sync_indices), cron(9, 20), id="sync-indices")
    scheduler.add_job(lambda: _audited("market.treasury", _sync_treasury), cron(11, 15), id="sync-treasury")
    scheduler.add_job(lambda: _audited("market.fx", _sync_fx), cron(14, 10), id="sync-fx")
    scheduler.add_job(lambda: _audited("market.benchmarks", _sync_benchmarks), cron(19, 30), id="sync-benchmarks")

    snapshot_hour, snapshot_minute = _hhmm(settings.snapshot_time, (23, 10))
    scheduler.add_job(
        lambda: _audited("portfolio.snapshots", _rebuild_snapshots),
        cron(snapshot_hour, snapshot_minute),
        id="daily-snapshot",
    )
    # After the portfolio snapshot, mirroring the beat schedule's 23:30 slot.
    scheduler.add_job(
        lambda: _audited("ai_wallet.snapshots", _snapshot_ai_wallets),
        cron(23, 30),
        id="ai-wallet-snapshot",
    )

    # Mirrors the beat schedule's 05:40 slot; no-ops unless the user enabled it.
    scheduler.add_job(_sync_universe, cron(5, 40), id="sync-universe")

    if settings.backup_time:
        backup_hour, backup_minute = _hhmm(settings.backup_time, (3, 30))

        def _weekly_backup() -> None:
            """Local dump + cloud mirror; both audit themselves (see run_scheduled_backup)."""
            try:
                run_scheduled_backup()
            except Exception:  # noqa: BLE001 — one failed job must not kill the schedule
                logger.exception("scheduled job backup.weekly failed")

        def _backup_catch_up() -> None:
            """A machine that was off on Sunday backs up on the next check.

            Runs shortly after boot (the "backup next time the app opens"
            path) and hourly after that; a no-op while the newest dump is
            under a week old.
            """
            try:
                catch_up_backup()
            except Exception:  # noqa: BLE001 — one failed job must not kill the schedule
                logger.exception("scheduled job backup.catch-up failed")

        # Weekly, mirroring the beat schedule: Sunday at BACKUP_TIME.
        scheduler.add_job(
            _weekly_backup,
            CronTrigger(day_of_week="sun", hour=backup_hour, minute=backup_minute, timezone=settings.timezone),
            id="weekly-backup",
        )
        scheduler.add_job(
            _backup_catch_up,
            IntervalTrigger(hours=1),
            # After the startup auto-import, so a fresh import lands in the dump.
            next_run_time=datetime.now() + timedelta(seconds=150),
            id="backup-catch-up",
        )

    return scheduler
