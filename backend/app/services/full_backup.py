"""The ``.gumbinvest`` format: a whole database in one portable file.

Exists to move a life's history between instances — typically Docker/Postgres
to the desktop build's SQLite — without asking the user to re-import years of
statements. It is a *clone*, not a merge: gzipped JSON of every table, primary
keys preserved, so the target shows exactly what the source showed.

One documented exception to "every table": :data:`EXCLUDED_TABLES`. Those hold
public data an instance rebuilds for itself, so they are neither exported nor
cleared on import — the target keeps whatever it already had.

Because it is a clone, importing is only allowed into an instance with no
transactions. Merging two histories would need the dedup machinery to arbitrate
every row; refusing loudly is the honest version of that feature until someone
needs it.

The file records the source's alembic revision. Import requires the same
revision on the target — "update both installs, then try again" beats silently
loading rows into a schema they no longer fit.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Table, text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Base, ImportBatch, Transaction
from app.portfolio.service import clear_replay_cache

logger = get_logger(__name__)

FORMAT_NAME = "gumbinvest-export"
FORMAT_VERSION = 1
FILE_EXTENSION = ".gumbinvest"
GZIP_MAGIC = b"\x1f\x8b"

#: Tables the clone deliberately leaves alone — public data this instance can
#: rebuild for itself, not history only the source has.
#:
#: ``asset_universe`` is a few thousand rows of B3 and CVM figures downloaded
#: from public files. Carrying it would inflate every export with data the
#: target can fetch in half a minute, and — the part that actually matters —
#: the import path below *deletes every table it touches*. Skipping the export
#: without skipping the delete would silently wipe a universe the user had
#: already built. So it is excluded from all three loops, which makes an import
#: preserve it rather than replace it.
EXCLUDED_TABLES: frozenset[str] = frozenset({"asset_universe"})


def _dumpable_tables() -> list[Table]:
    """Tables the export/import clone operates on, in FK-safe order."""
    return [t for t in Base.metadata.sorted_tables if t.name not in EXCLUDED_TABLES]


class FullBackupError(ValueError):
    """Raised when a .gumbinvest file cannot be imported. Message is pt-BR."""


def is_full_backup(payload: bytes, filename: str) -> bool:
    """Cheap detection for the upload endpoint: extension or gzip magic."""
    return filename.lower().endswith(FILE_EXTENSION) or bytes(payload[:2]) == GZIP_MAGIC


def _schema_revision(db: Session) -> str | None:
    try:
        return db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001 — create_all databases carry no revision
        return None


def _encode(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _decode(table: Table, row: dict) -> dict:
    """Coerce JSON values back to what each column's Python type expects."""
    out: dict = {}
    for name, value in row.items():
        column = table.columns.get(name)
        if column is None or value is None:
            out[name] = value
            continue
        python_type: type | None
        try:
            python_type = column.type.python_type
        except NotImplementedError:  # JSON columns — pass through as-is
            python_type = None
        if python_type is Decimal:
            out[name] = Decimal(value)
        elif python_type is datetime:
            out[name] = datetime.fromisoformat(value)
        elif python_type is date:
            out[name] = date.fromisoformat(value)
        else:
            out[name] = value
    return out


def _drop_secrets(table_name: str, rows: list[dict]) -> list[dict]:
    """API keys live in app_settings; a shared export must never carry them."""
    if table_name != "app_settings":
        return rows
    from app.services.secrets import SECRET_KEYS

    return [row for row in rows if row.get("key") not in SECRET_KEYS]


def export_snapshot(db: Session) -> bytes:
    """Every table, in FK-safe order, as gzipped JSON."""
    tables: dict[str, list[dict]] = {}
    total = 0
    for table in _dumpable_tables():
        rows = db.execute(table.select()).mappings().all()
        tables[table.name] = _drop_secrets(
            table.name,
            [{name: _encode(value) for name, value in row.items()} for row in rows],
        )
        total += len(tables[table.name])

    document = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "schema_revision": _schema_revision(db),
        "exported_at": datetime.now(UTC).isoformat(),
        "row_count": total,
        "tables": tables,
    }
    logger.info("full export: %s rows across %s tables", total, len(tables))
    return gzip.compress(json.dumps(document, ensure_ascii=False).encode("utf-8"))


def _parse(payload: bytes) -> dict:
    try:
        document = json.loads(gzip.decompress(payload))
    except (OSError, ValueError) as exc:
        raise FullBackupError("o arquivo não é um export .gumbinvest válido") from exc
    if not isinstance(document, dict) or document.get("format") != FORMAT_NAME:
        raise FullBackupError("o arquivo não é um export .gumbinvest válido")
    if document.get("format_version") != FORMAT_VERSION:
        raise FullBackupError(
            "este export foi gerado por uma versão mais nova do GumbInvest; "
            "atualize esta instalação antes de importar"
        )
    return document


def import_snapshot(db: Session, payload: bytes, filename: str) -> dict:
    """Replace this instance's data with the snapshot. Fresh installs only."""
    document = _parse(payload)

    if db.query(Transaction.id).first() is not None:
        raise FullBackupError(
            "esta instalação já tem movimentações; o import completo só é "
            "permitido numa instalação vazia, para nunca misturar duas histórias"
        )

    source_rev = document.get("schema_revision")
    target_rev = _schema_revision(db)
    if source_rev and target_rev and source_rev != target_rev:
        raise FullBackupError(
            f"as versões do banco não coincidem (origem {source_rev}, destino "
            f"{target_rev}); atualize as duas instalações e exporte de novo"
        )

    tables = document.get("tables", {})
    unknown = sorted(set(tables) - {t.name for t in _dumpable_tables()})
    inserted: dict[str, int] = {}

    # One transaction: either the whole history arrives or nothing changes.
    for table in reversed(_dumpable_tables()):
        db.execute(table.delete())
    for table in _dumpable_tables():
        # _drop_secrets again on the way in: files made by older versions may
        # still carry keys, and they must not become this instance's secrets.
        rows = _drop_secrets(
            table.name, [_decode(table, row) for row in tables.get(table.name, [])]
        )
        if rows:
            db.execute(table.insert(), rows)
        inserted[table.name] = len(rows)

    if db.get_bind().dialect.name == "postgresql":
        _resync_sequences(db)

    total = sum(inserted.values())
    portfolio_id = db.execute(
        text("SELECT id FROM portfolios ORDER BY is_default DESC, id LIMIT 1")
    ).scalar()
    if portfolio_id is None:
        db.rollback()
        raise FullBackupError("o export não contém nenhuma carteira; arquivo corrompido?")
    batch = ImportBatch(
        portfolio_id=portfolio_id,
        filename=filename,
        file_hash=hashlib.sha256(payload).hexdigest(),
        status="COMPLETED",
        rows_total=total,
        rows_imported=total,
        source_kind="FULL",
        summary={
            "tables": inserted,
            "schema_revision": source_rev,
            "exported_at": document.get("exported_at"),
            "warnings": (
                [f"tabelas desconhecidas ignoradas: {', '.join(unknown)}"] if unknown else []
            ),
        },
        finished_at=datetime.now(UTC),
    )
    db.add(batch)
    db.commit()
    clear_replay_cache()
    logger.info("full import of %s: %s rows restored", filename, total)

    return {
        "batch_id": batch.id,
        "filename": filename,
        "status": "COMPLETED",
        "rows_total": total,
        "rows_imported": total,
        "rows_duplicate": 0,
        "rows_failed": 0,
        "issues": [],
        "summary": batch.summary,
    }


def _resync_sequences(db: Session) -> None:
    """Explicit-id inserts leave Postgres sequences behind; catch them up."""
    for table in Base.metadata.sorted_tables:
        pk = list(table.primary_key.columns)
        # Surrogate keys only: an integer PK that is not itself a foreign key
        # (quotes/fundamentals/terms are keyed by asset_id and have no sequence).
        if len(pk) == 1 and pk[0].type.python_type is int and not pk[0].foreign_keys:
            db.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk[0].name}'), "
                    f"COALESCE((SELECT MAX({pk[0].name}) FROM {table.name}), 1))"
                )
            )
