"""Where the desktop build keeps user data and finds bundled resources.

User data lives under ``%LOCALAPPDATA%\\GumbInvest`` and survives both app
upgrades and uninstalls. Bundled read-only resources (the built SPA, the
alembic scripts) live next to the executable when frozen by PyInstaller, and
in the repository during development — :func:`resource_path` hides the
difference.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def data_root() -> Path:
    # Must match desktop-shell/main.js — the Electron shell reads port.txt from
    # this directory to find the server.
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "GumbInvest"
    else:
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "GumbInvest"
    root.mkdir(parents=True, exist_ok=True)
    return root


def database_file() -> Path:
    return data_root() / "gumbinvest.db"


def backups_dir() -> Path:
    path = data_root() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def auto_import_dir() -> Path:
    path = data_root() / "auto-import"
    path.mkdir(parents=True, exist_ok=True)
    # One folder per source, mirroring the repo's data/ layout. Purely a
    # convention for whoever opens the folder — the importer scans recursively
    # and detects each file's type from its contents, wherever it sits.
    for source in ("b3", "avenue", "nomad", "binance"):
        (path / source).mkdir(exist_ok=True)
    return path


def logs_dir() -> Path:
    path = data_root() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def qr_file() -> Path:
    return data_root() / "phone-qr.png"


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_path(*parts: str) -> Path:
    """A bundled resource, wherever this build keeps it.

    Frozen: PyInstaller unpacks ``datas`` under ``sys._MEIPASS``. Unfrozen:
    resources sit in the repository, two directories above ``app/``.
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS")).joinpath(*parts)
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root.joinpath(*parts)


def frontend_dist() -> Path:
    return resource_path("frontend", "dist")


def alembic_dir() -> Path:
    if is_frozen():
        return resource_path("alembic")
    return Path(__file__).resolve().parents[2] / "alembic"


def alembic_ini() -> Path:
    if is_frozen():
        return resource_path("alembic.ini")
    return Path(__file__).resolve().parents[2] / "alembic.ini"
