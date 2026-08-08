"""Celery application and beat schedule."""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings
from app.core.logging import configure_logging

configure_logging()

celery_app = Celery("gumbinvest", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=settings.timezone,
    enable_utc=True,
    task_acks_late=True,
    worker_max_tasks_per_child=200,
    task_time_limit=1800,
    result_expires=3600,
    imports=("app.workers.tasks",),
)


def _hhmm(value: str, fallback: tuple[int, int]) -> tuple[int, int]:
    try:
        hour, minute = value.split(":")
        return int(hour), int(minute)
    except (AttributeError, ValueError):
        return fallback


schedule: dict[str, dict] = {
    "refresh-quotes": {
        "task": "app.workers.tasks.refresh_quotes_task",
        "schedule": max(settings.price_refresh_minutes, 1) * 60.0,
    },
    # Drains the transient-failure queue. Runs every minute because the first
    # retry is two minutes out and the user is watching the screen right then;
    # it costs one read of a table that is empty whenever nothing is wrong.
    "retry-quotes": {
        "task": "app.workers.tasks.retry_quotes_task",
        "schedule": 60.0,
    },
}

# A newly imported ticker arrives with no history, and `refresh_quotes` only
# ever adds today's close — so without this its chart stays empty and, worse,
# the patrimônio curve draws it flat at cost until enough days accumulate. This
# run touches only assets that have nothing stored, so it is a no-op on any day
# no new asset appeared. The full re-download stays manual.
schedule["backfill-new-assets"] = {
    "task": "app.workers.tasks.backfill_history_task",
    "schedule": crontab(hour=6, minute=40),
    "kwargs": {"only_missing": True},
}

# Banco Central publishes the previous day's CDI in the morning.
schedule["sync-indices"] = {
    "task": "app.workers.tasks.sync_indices_task",
    "schedule": crontab(hour=9, minute=20),
}

# After the B3 close, so the day the chart compares against is a settled one.
schedule["sync-benchmarks"] = {
    "task": "app.workers.tasks.sync_benchmarks_task",
    "schedule": crontab(hour=19, minute=30),
}

# PTAX is published around 13:00 on business days; the closing rate for the
# previous day is stable well before this run.
schedule["sync-fx"] = {
    "task": "app.workers.tasks.sync_fx_task",
    "schedule": crontab(hour=14, minute=10),
}

# A cold start whose downloads failed (offline, timeout, rate limit) heals
# within the hour instead of waiting a day for the slots above. No-op — and
# no audit row — whenever every series already exists.
schedule["heal-market-data"] = {
    "task": "app.workers.tasks.heal_market_data_task",
    "schedule": crontab(minute=25),
}

# Tesouro Transparente republishes the price file every business morning,
# shortly after the 9 a.m. snapshot it contains.
schedule["sync-treasury"] = {
    "task": "app.workers.tasks.sync_treasury_task",
    "schedule": crontab(hour=11, minute=15),
}

# Keeps the dividend calendar's "upcoming" side current without anyone opening
# every asset page: fundamentals (with B3's declared payment schedule) for each
# held asset, refreshed once a day. Only stale entries are fetched.
schedule["refresh-fundamentals"] = {
    "task": "app.workers.tasks.refresh_fundamentals_task",
    "schedule": crontab(hour=8, minute=5),
}

snapshot_hour, snapshot_minute = _hhmm(settings.snapshot_time, (23, 10))
schedule["daily-snapshot"] = {
    "task": "app.workers.tasks.rebuild_snapshots_task",
    "schedule": crontab(hour=snapshot_hour, minute=snapshot_minute),
}

# After the portfolio snapshot (23:10 default), so both series close the same
# day with the same quotes.
schedule["ai-wallet-snapshot"] = {
    "task": "app.workers.tasks.snapshot_ai_wallets_task",
    "schedule": crontab(hour=23, minute=30),
}

# The asset universe, from B3 and CVM bulk files. Early: the CVM republishes
# overnight and B3's previous session is settled by then, so this runs before
# anyone is looking at a screener. The task itself no-ops unless the user has
# enabled the feature, so scheduling it unconditionally costs nothing.
schedule["sync-universe"] = {
    "task": "app.workers.tasks.sync_universe_task",
    "schedule": crontab(hour=5, minute=40),
}

if settings.backup_time:
    backup_hour, backup_minute = _hhmm(settings.backup_time, (3, 30))
    schedule["daily-backup"] = {
        "task": "app.workers.tasks.backup_database_task",
        "schedule": crontab(hour=backup_hour, minute=backup_minute),
    }
    # A host that was off at BACKUP_TIME backs up at the next opportunity
    # instead of skipping the day. No-op whenever the newest dump is recent.
    schedule["backup-catch-up"] = {
        "task": "app.workers.tasks.backup_catch_up_task",
        "schedule": crontab(minute=35),
    }

celery_app.conf.beat_schedule = schedule
