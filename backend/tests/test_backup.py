"""SQLite branch of the backup service: snapshot, gzip, rotation."""
from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

from app.core.config import settings
from app.services import backup as backup_service


def _make_source_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('ok')")
        conn.commit()
    finally:
        conn.close()


def test_sqlite_backup_writes_gzip_and_rotates(tmp_path: Path, monkeypatch, db) -> None:
    source = tmp_path / "source.db"
    _make_source_db(source)
    backup_dir = tmp_path / "backups"

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{source}")
    monkeypatch.setattr(settings, "backup_dir", str(backup_dir))
    monkeypatch.setattr(settings, "backup_keep", 2)

    # Pre-existing backups beyond the keep limit must rotate away.
    backup_dir.mkdir(parents=True)
    for stamp in ("20200101-000000", "20200102-000000"):
        (backup_dir / f"gumbinvest-{stamp}.db.gz").write_bytes(b"old")

    result = backup_service.backup_database()

    assert result["status"] == "ok"
    remaining = sorted(backup_dir.glob("gumbinvest-*.db.gz"))
    assert len(remaining) == settings.backup_keep
    assert Path(result["file"]) == remaining[-1]

    # The newest file must be a valid gzip of a valid SQLite database.
    restored = tmp_path / "restored.db"
    with gzip.open(remaining[-1], "rb") as packed:
        restored.write_bytes(packed.read())
    conn = sqlite3.connect(restored)
    try:
        assert conn.execute("SELECT v FROM t").fetchone() == ("ok",)
    finally:
        conn.close()


def test_sqlite_backup_missing_file_fails_loudly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'absent.db'}")
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))

    result = backup_service.backup_database()
    assert result["status"] == "failed"


def test_backup_is_due_judges_by_the_newest_dump(tmp_path: Path, monkeypatch) -> None:
    """The catch-up trigger: no dump, or a stale one, means the slot was missed."""
    import os
    import time

    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(settings, "backup_dir", str(backup_dir))

    # No directory / no files: a first run is always due.
    assert backup_service.backup_is_due() is True

    backup_dir.mkdir(parents=True)
    dump = backup_dir / "gumbinvest-20260807-033000.db.gz"
    dump.write_bytes(b"x")
    assert backup_service.backup_is_due() is False  # fresh mtime

    two_days_ago = time.time() - 2 * 24 * 3600
    os.utime(dump, (two_days_ago, two_days_ago))
    assert backup_service.backup_is_due() is True

    # Backups disabled: never due, the catch-up must stay silent.
    monkeypatch.setattr(settings, "backup_time", "")
    assert backup_service.backup_is_due() is False


def test_catch_up_runs_only_when_due(monkeypatch) -> None:
    runs: list[bool] = []
    monkeypatch.setattr(backup_service, "run_daily_backup", lambda: runs.append(True) or {"status": "ok"})

    monkeypatch.setattr(backup_service, "backup_is_due", lambda: False)
    assert backup_service.catch_up_backup() == {"skipped": True}
    assert runs == []

    monkeypatch.setattr(backup_service, "backup_is_due", lambda: True)
    assert backup_service.catch_up_backup() == {"status": "ok"}
    assert runs == [True]
