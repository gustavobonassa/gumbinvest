# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the GumbInvest desktop server.

The Electron shell (desktop-shell/) spawns this bundle and owns the window
and tray; this is only the Python server: SQLite, scheduler, API + SPA.
onedir on purpose: onefile re-extracts to %TEMP% at every launch — slow, and
a known Defender false-positive magnet. electron-builder ships the folder as
an extraResource.
"""
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent          # repo root
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))      # so collect_submodules can see `app`

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = (
    # uvicorn loads the app and its own components from strings.
    collect_submodules("app")
    + [
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ]
)

a = Analysis(
    ["launcher.py"],
    pathex=[str(BACKEND)],
    datas=[
        (str(ROOT / "frontend" / "dist"), "frontend/dist"),
        (str(BACKEND / "alembic"), "alembic"),
        (str(BACKEND / "alembic.ini"), "."),
    ],
    hiddenimports=hiddenimports,
    excludes=[
        "celery",
        "redis",
        "psycopg2",
        "yfinance",
        "pandas",
        "numpy",
        "matplotlib",
        "pytest",
        "tkinter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="gumbinvest-server",
    console=False,
    # .ico is a Windows format; on macOS/Linux the binary carries no icon
    # (the Electron shell owns the visible app identity anyway).
    icon=str(Path(SPECPATH) / "assets" / "gumbinvest.ico") if sys.platform == "win32" else None,
)

coll = COLLECT(exe, a.binaries, a.datas, name="gumbinvest-server")
