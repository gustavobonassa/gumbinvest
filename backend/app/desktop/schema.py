"""Schema creation and upgrades for the desktop database.

A fresh database gets the current schema straight from the models plus an
``alembic stamp head`` — the same shortcut the test suite uses — so first
launch never replays four-plus years of migration history. An existing
database gets ``alembic upgrade head``, which is how a new app version
carries every user's data forward.
"""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.core.config import settings
from app.core.logging import get_logger
from app.desktop import paths

logger = get_logger(__name__)


def _alembic_config() -> Config:
    config = Config(str(paths.alembic_ini()))
    config.set_main_option("script_location", str(paths.alembic_dir()))
    config.attributes["database_url"] = settings.database_url
    # alembic.ini's [loggers] section, applied via fileConfig, *replaces* the
    # root handlers — which silently killed the desktop file log right after
    # "applying migrations" on every boot. The CLI keeps its config; a
    # programmatic caller keeps its own logging.
    config.attributes["skip_logging_config"] = True
    return config


def init_db() -> None:
    from app.db.models import Base
    from app.db.session import engine

    has_tables = bool(inspect(engine).get_table_names())
    if not has_tables:
        logger.info("fresh database — creating schema at %s", settings.database_url)
        Base.metadata.create_all(engine)
        command.stamp(_alembic_config(), "head")
    else:
        logger.info("existing database — applying migrations")
        command.upgrade(_alembic_config(), "head")

    # The Electron shell kills this server rather than asking it to stop, so
    # the write-ahead log never checkpoints on exit and grows until it rivals
    # the database itself (a 111 MB WAL was observed) — and every query then
    # reads through it. Folding it back into the main file at boot keeps reads
    # fast and the file pair small; on a healthy WAL this is near-instant.
    if engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            result = connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            logger.info("wal checkpoint (busy, log frames, checkpointed): %s", result)
