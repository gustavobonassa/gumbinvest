"""Environment defaults for desktop mode.

Must run before anything imports ``app.core.config`` — the settings object is
a module-level singleton, so whatever environment exists at first import is
what the whole process lives with. ``setdefault`` keeps explicit overrides
working: a user (or developer) who exports ``DATABASE_URL`` gets exactly that
database, Postgres included.

This module deliberately imports nothing from ``app``.
"""
from __future__ import annotations

import os

from app.desktop import paths

DEFAULT_PORT = 8873


def configure_environment() -> None:
    db_file = paths.database_file().as_posix()
    os.environ.setdefault("DESKTOP_MODE", "1")
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{db_file}")
    os.environ.setdefault("BACKUP_DIR", str(paths.backups_dir()))
    os.environ.setdefault("AUTO_IMPORT_DIR", str(paths.auto_import_dir()))
    # The desktop machine has a real display, and a visible real-browser
    # session is what clears the broker portals' anti-bot challenges (B3 sits
    # behind Cloudflare). Docker has no display, so it keeps the headless
    # default from config.py. Either can be overridden with PIPELINE_HEADLESS.
    os.environ.setdefault("PIPELINE_HEADLESS", "0")


def port() -> int:
    try:
        return int(os.environ.get("GUMBINVEST_PORT", ""))
    except ValueError:
        return DEFAULT_PORT
