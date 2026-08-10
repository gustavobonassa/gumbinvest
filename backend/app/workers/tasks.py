"""Background jobs: quote refresh, snapshots, history backfill, backups."""
from __future__ import annotations

from app.core.logging import get_logger
from app.db.models import AuditLog
from app.db.session import session_scope
from app.market.crypto import sync_crypto_fx
from app.market.fixed_income import ensure_terms_for_fixed_income
from app.market.fx import backfill_transaction_fx, sync_all_fx
from app.market.indices import sync_all_indices
from app.market.service import (
    backfill_history,
    refresh_quotes,
    retry_pending_quotes,
    sync_splits,
)
from app.market.treasury import sync_treasury_prices
from app.portfolio.service import PortfolioService
from app.services.backup import backup_database
from app.services.portfolio_registry import get_default_portfolio
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


@celery_app.task(name="app.workers.tasks.refresh_quotes_task")
def refresh_quotes_task(force: bool = False) -> dict:
    with session_scope() as db:
        portfolio = get_default_portfolio(db)
        result = refresh_quotes(db, portfolio.id, force=force)
        db.add(AuditLog(action="market.refresh", detail={k: str(v) for k, v in result.items()}))
        return {k: str(v) for k, v in result.items()}


@celery_app.task(name="app.workers.tasks.sync_splits_task")
def sync_splits_task() -> dict:
    """Re-check declared splits. Cheap, and the only thing that keeps a
    historical curve honest once a paper splits."""
    with session_scope() as db:
        portfolio = get_default_portfolio(db)
        result = sync_splits(db, portfolio.id)
        db.add(AuditLog(action="market.splits", detail={k: str(v) for k, v in result.items()}))
        return {k: str(v) for k, v in result.items()}


@celery_app.task(name="app.workers.tasks.retry_quotes_task")
def retry_quotes_task() -> dict:
    """Drain the retry queue. A no-op — one empty read — when nothing failed.

    Deliberately not audited: it runs every minute, and a log line per minute
    saying "nothing to do" would bury the entries that matter.
    """
    with session_scope() as db:
        result = retry_pending_quotes(db)
        if result.get("recovered"):
            db.add(AuditLog(action="market.retry", detail={k: str(v) for k, v in result.items()}))
        return {k: str(v) for k, v in result.items()}


@celery_app.task(name="app.workers.tasks.backfill_history_task")
def backfill_history_task(limit: int | None = None, only_missing: bool = False) -> dict:
    with session_scope() as db:
        portfolio = get_default_portfolio(db)
        result = backfill_history(db, portfolio.id, limit=limit, only_missing=only_missing)
        db.add(AuditLog(action="market.backfill", detail={k: str(v) for k, v in result.items()}))
        return {k: str(v) for k, v in result.items()}


@celery_app.task(name="app.workers.tasks.sync_indices_task")
def sync_indices_task() -> dict:
    """Refresh CDI/Selic/IPCA so fixed income keeps accruing."""
    with session_scope() as db:
        ensure_terms_for_fixed_income(db)
        result = sync_all_indices(db)
        db.add(AuditLog(action="market.indices", detail={k: str(v) for k, v in result.items()}))
        return {k: str(v) for k, v in result.items()}


@celery_app.task(name="app.workers.tasks.sync_benchmarks_task")
def sync_benchmarks_task() -> dict:
    """Refresh the Ibovespa close so the return chart keeps its comparison."""
    from app.market.benchmarks import sync_benchmarks

    with session_scope() as db:
        result = sync_benchmarks(db)
        db.add(AuditLog(action="market.benchmarks", detail={k: str(v) for k, v in result.items()}))
        return {k: str(v) for k, v in result.items()}


@celery_app.task(name="app.workers.tasks.sync_fx_task")
def sync_fx_task() -> dict:
    """Refresh the PTAX series so offshore holdings keep converting to reais."""
    with session_scope() as db:
        result = sync_all_fx(db)
        # Trades an exchange priced in a coin need that coin's own closes as a
        # rate; they only exist once the history backfill has run, so this is
        # re-published on every pass rather than once at import time.
        result["crypto"] = sync_crypto_fx(db)["points"]
        # Any movement imported while the series was behind gets its rate now.
        result["backfilled"] = backfill_transaction_fx(db)["updated"]
        db.add(AuditLog(action="market.fx", detail={k: str(v) for k, v in result.items()}))
        return {k: str(v) for k, v in result.items()}


@celery_app.task(name="app.workers.tasks.heal_market_data_task")
def heal_market_data_task() -> dict:
    """Fetch series a failed bootstrap left empty (PTAX pairs, indices, Ibov).

    A no-op when everything exists; an audit row is only written when it
    actually fetched something, so an hourly heartbeat never floods the log.
    """
    from app.market.service import heal_market_data

    with session_scope() as db:
        healed = heal_market_data(db)
        if healed:
            db.add(AuditLog(action="market.heal", detail={k: str(v) for k, v in healed.items()}))
        return {k: str(v) for k, v in healed.items()}


@celery_app.task(name="app.workers.tasks.sync_treasury_task")
def sync_treasury_task() -> dict:
    """Refresh Tesouro Direto prices from Tesouro Transparente."""
    with session_scope() as db:
        result = sync_treasury_prices(db)
        db.add(AuditLog(action="market.treasury", detail={k: str(v) for k, v in result.items()}))
        return {k: str(v) for k, v in result.items()}


@celery_app.task(name="app.workers.tasks.rebuild_snapshots_task")
def rebuild_snapshots_task() -> dict:
    with session_scope() as db:
        portfolio = get_default_portfolio(db)
        count = PortfolioService(db, portfolio.id).rebuild_snapshots()
        db.add(AuditLog(action="portfolio.snapshots", detail={"points": count}))
        return {"points": count}


@celery_app.task(name="app.workers.tasks.snapshot_ai_wallets_task")
def snapshot_ai_wallets_task() -> dict:
    """Daily value snapshot for every AI-managed virtual wallet."""
    from app.services.ai_wallet import snapshot_ai_wallets

    with session_scope() as db:
        result = snapshot_ai_wallets(db)
        db.add(AuditLog(action="ai_wallet.snapshots", detail={k: str(v) for k, v in result.items()}))
        return result


@celery_app.task(name="app.workers.tasks.backup_database_task")
def backup_database_task() -> dict:
    """Dump the database into ``BACKUP_DIR``, then mirror it to the cloud."""
    from app.services.backup import run_scheduled_backup

    return run_scheduled_backup()


@celery_app.task(name="app.workers.tasks.backup_catch_up_task")
def backup_catch_up_task() -> dict:
    """Run the weekly backup now if the Sunday slot was missed (host off)."""
    from app.services.backup import catch_up_backup

    return catch_up_backup()


@celery_app.task(name="app.workers.tasks.refresh_fundamentals_task")
def refresh_fundamentals_task(only_stale: bool = True) -> dict:
    """Refresh held assets' fundamentals — the dividend calendar's data."""
    from app.market.fundamentals import refresh_held_fundamentals

    with session_scope() as db:
        portfolio = get_default_portfolio(db)
        result = refresh_held_fundamentals(db, portfolio.id, only_stale=only_stale)
        db.add(AuditLog(action="fundamentals.refresh", detail={k: str(v) for k, v in result.items()}))
        return result


@celery_app.task(name="app.workers.tasks.sync_universe_task")
def sync_universe_task() -> dict:
    """Refresh the asset universe from the published bulk files.

    One bounded slice per run: the budget is half of ``task_time_limit`` so the
    task always returns before Celery kills it, and a run that does not finish
    resumes at its next stage tomorrow. Does nothing unless the user has
    switched the universe on — it is opt-in and downloads a few hundred MB.
    """
    from app.market.universe import ingest, state

    with session_scope() as db:
        if not state.is_enabled(db):
            return {"skipped": "universo desativado"}
        if state.read(db)["active"]:
            return {"skipped": "já em execução"}

    result = ingest.run_ingest(budget_seconds=900)
    with session_scope() as db:
        db.add(
            AuditLog(
                action="universe.sync",
                detail={"state": result.get("state"), "message": result.get("message")},
            )
        )
    return {"state": result.get("state"), "stages": result.get("stage_rows")}


@celery_app.task(name="app.workers.tasks.run_pipelines_task")
def run_pipelines_task() -> dict:
    """The weekly collection: every configured pipeline, one after the other.

    ``run_scheduled`` manages its own sessions — a browser automation must
    never hold a transaction across minutes of page waits — and each run
    audits and notifies itself, so this wrapper only records the roll-up.
    """
    from app.pipelines.runner import run_scheduled

    result = run_scheduled()
    with session_scope() as db:
        db.add(AuditLog(action="pipeline.scheduled", detail={k: str(v) for k, v in result.items()}))
    return result
