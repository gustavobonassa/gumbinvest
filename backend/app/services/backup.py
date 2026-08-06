"""Database backups, dispatched on the database actually in use.

PostgreSQL (Docker) keeps the original ``pg_dump | gzip`` pipeline. SQLite
(the desktop build) uses the connection's own ``backup()`` — the only way to
copy a live database safely under WAL — and gzips the result. Both write into
``BACKUP_DIR`` and rotate down to ``BACKUP_KEEP`` files.
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.engine import make_url

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import AuditLog
from app.db.session import session_scope

logger = get_logger(__name__)


def backup_database() -> dict:
    """Back up the configured database into ``BACKUP_DIR`` and rotate."""
    target_dir = Path(settings.backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    if settings.database_url.startswith("sqlite"):
        return _backup_sqlite(target_dir, stamp)
    return _backup_postgres(target_dir, stamp)


def _rotate(target_dir: Path, pattern: str) -> int:
    backups = sorted(target_dir.glob(pattern))
    removed = 0
    for old in backups[: max(len(backups) - settings.backup_keep, 0)]:
        old.unlink(missing_ok=True)
        removed += 1
    return removed


def _record(target: Path, removed: int) -> dict:
    with session_scope() as db:
        db.add(AuditLog(action="backup.database", detail={"file": target.name, "rotated": removed}))
    logger.info("backup written to %s (rotated %s)", target, removed)
    return {"status": "ok", "file": str(target), "rotated": removed}


def _backup_sqlite(target_dir: Path, stamp: str) -> dict:
    source = make_url(settings.database_url).database
    if not source or not Path(source).exists():
        return {"status": "failed", "error": f"database file not found: {source}"}

    target = target_dir / f"gumbinvest-{stamp}.db.gz"
    snapshot = target_dir / f"gumbinvest-{stamp}.db.tmp"
    src_conn = sqlite3.connect(source)
    try:
        dest_conn = sqlite3.connect(snapshot)
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()
    try:
        with snapshot.open("rb") as raw, gzip.open(target, "wb") as packed:
            shutil.copyfileobj(raw, packed)
    finally:
        snapshot.unlink(missing_ok=True)

    removed = _rotate(target_dir, "gumbinvest-*.db.gz")
    return _record(target, removed)


def _backup_postgres(target_dir: Path, stamp: str) -> dict:
    target = target_dir / f"gumbinvest-{stamp}.sql.gz"
    url = urlparse(settings.database_url.replace("postgresql+psycopg2", "postgresql"))
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    command = (
        f"pg_dump -h {url.hostname} -p {url.port or 5432} -U {url.username} "
        f"-d {(url.path or '/').lstrip('/')} | gzip > {target}"
    )
    try:
        subprocess.run(["/bin/sh", "-c", command], check=True, env=env, capture_output=True)
    except subprocess.CalledProcessError as exc:
        logger.error("backup failed: %s", exc.stderr.decode(errors="replace")[:500])
        return {"status": "failed", "error": exc.stderr.decode(errors="replace")[:500]}

    removed = _rotate(target_dir, "gumbinvest-*.sql.gz")
    return _record(target, removed)
