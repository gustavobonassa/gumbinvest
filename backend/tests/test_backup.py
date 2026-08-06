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
