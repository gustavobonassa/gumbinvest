"""The full migration chain must run on SQLite.

The desktop build upgrades user databases with ``alembic upgrade head`` on an
SQLite file, so a migration that only works on PostgreSQL would strand every
desktop install at the previous schema. This is the guardrail: any new
migration that breaks SQLite fails here, in the default ``pytest`` run.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _alembic_config(url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = url
    return config


def test_migration_chain_runs_on_sqlite(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'migrations.sqlite'}"
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url, future=True)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "transactions" in tables
        assert "portfolios" in tables
        # Migration 0012 relaxes this column; batch mode must have applied it.
        ticker = next(c for c in inspector.get_columns("ai_chats") if c["name"] == "ticker")
        assert ticker["nullable"] is True
    finally:
        engine.dispose()


def test_asset_universe_exists_after_the_chain(tmp_path):
    """The desktop build runs this chain; a missing table breaks the feature."""
    from sqlalchemy import create_engine, inspect

    url = f"sqlite+pysqlite:///{tmp_path / 'universe.sqlite'}"
    command.upgrade(_alembic_config(url), "head")
    inspector = inspect(create_engine(url))
    assert "asset_universe" in inspector.get_table_names()
    columns = {c["name"]: c for c in inspector.get_columns("asset_universe")}
    # Every metric is nullable on purpose: NULL means "not published", and a
    # NOT NULL column here would force a screener to invent a zero.
    assert columns["market_cap"]["nullable"] is True
    assert columns["pe"]["nullable"] is True
    assert columns["ticker"]["nullable"] is False
