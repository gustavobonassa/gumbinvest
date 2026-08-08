"""Cloud backup orchestration: export → encrypt → upload → rotate, and back.

Runs in two places with different plumbing. The nightly sync executes in the
Celery worker (or the desktop scheduler thread) right after the local dump,
so its outcome is written to a durable ``app_settings`` row — the API
container answers the status poll from that row, since an in-memory job
registry never crosses the container boundary. The manual "Enviar agora" is
HTTP-triggered and therefore *does* share a process with its poller; the
route wraps this same :func:`sync_to_cloud` in a ``JobRegistry``.

A nightly worker run and a manual send can theoretically overlap. The worst
case is one extra file in the cloud, and rotation converges on the next run —
not worth a cross-process lock.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import AuditLog
from app.db.session import session_scope
from app.services.backup import backup_database
from app.services.cloud_backup.base import (
    CloudBackupProvider,
    available_providers,
    get_provider,
    is_backup_name,
    read_row,
    read_value,
    write_row,
)
from app.services.cloud_backup.crypto import (
    ENCRYPTED_EXTENSION,
    CloudBackupError,
    decrypt,
    encrypt,
    is_encrypted,
)
from app.services.full_backup import FILE_EXTENSION, export_snapshot, import_snapshot

logger = get_logger(__name__)

STATUS_KEY = "cloud_backup_status"


def _rotate(db: Session, provider: CloudBackupProvider) -> int:
    """Keep the newest ``backup_keep`` of our files; the stamp sorts by name."""
    backups = [b for b in provider.list_backups(db) if is_backup_name(b.name)]
    backups.sort(key=lambda backup: backup.name, reverse=True)
    removed = 0
    for stale in backups[max(settings.backup_keep, 1) :]:
        provider.delete(db, stale.id)
        removed += 1
    return removed


def _announce(db: Session, results: dict[str, dict], now: str) -> None:
    """One bell entry per provider per run — the history the panel scrolls.

    Keyed by provider and run timestamp so a task retried after a partial
    failure cannot announce the same upload twice.
    """
    from app.services.notifications import record

    for name, entry in results.items():
        try:
            label = get_provider(name).label
        except Exception:  # noqa: BLE001 — a renamed provider must not lose the entry
            label = name
        ok = entry["state"] == "ok"
        record(
            db,
            kind="cloud_backup",
            level="success" if ok else "warning",
            title="Backup na nuvem enviado" if ok else "Backup na nuvem falhou",
            body=(
                f"{label}: {entry.get('file')}"
                if ok
                else f"{label}: {entry.get('message') or 'falha no último envio'}"
            ),
            dedup_key=f"cloud_backup:{name}:{now}",
        )


def sync_to_cloud() -> dict:
    """Upload a fresh export to every connected provider. Own session, like
    ``backup_database()`` — callers in schedulers hold no session of theirs.
    """
    with session_scope() as db:
        providers = [p for p in available_providers() if p.connected(db)]
        if not providers:
            return {"status": "skipped", "reason": "nenhum provedor conectado"}

        payload = export_snapshot(db)
        passphrase = read_value(db, "cloud_backup_passphrase")
        if passphrase:
            payload = encrypt(payload, passphrase)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"gumbinvest-{stamp}" + (ENCRYPTED_EXTENSION if passphrase else FILE_EXTENSION)

        now = datetime.now(UTC).isoformat()
        results: dict[str, dict] = {}
        for provider in providers:
            try:
                remote = provider.upload(db, filename, payload)
                entry = {
                    "state": "ok",
                    "file": remote.name,
                    "size": len(payload),
                    "at": now,
                    "message": None,
                }
                try:
                    entry["rotated"] = _rotate(db, provider)
                except Exception:  # noqa: BLE001 — a failed cleanup must not fail the upload
                    logger.exception("cloud backup rotation failed (%s)", provider.name)
                results[provider.name] = entry
            except CloudBackupError as exc:
                results[provider.name] = {
                    "state": "error",
                    "file": None,
                    "size": None,
                    "at": now,
                    "message": str(exc),
                }
            except Exception:  # noqa: BLE001 — one provider must not stop the others
                logger.exception("cloud backup upload failed (%s)", provider.name)
                results[provider.name] = {
                    "state": "error",
                    "file": None,
                    "size": None,
                    "at": now,
                    "message": f"falha inesperada ao enviar para o {provider.label}",
                }

        write_row(db, STATUS_KEY, {"last_run_at": now, "providers": results})
        db.add(
            AuditLog(
                action="backup.cloud",
                detail={name: entry["state"] for name, entry in results.items()},
            )
        )
        _announce(db, results, now)
        failed = [name for name, entry in results.items() if entry["state"] == "error"]
        status = "ok" if not failed else ("partial" if len(failed) < len(results) else "failed")
        return {"status": status, "providers": results}


def status_payload(db: Session) -> dict:
    """What the Backup tab renders: per-provider connection + last outcome."""
    row = read_row(db, STATUS_KEY) or {}
    last_by_provider = row.get("providers") or {}
    return {
        "providers": [
            {
                "name": provider.name,
                "label": provider.label,
                "configured": provider.configured(db),
                "connected": provider.connected(db),
                "last": last_by_provider.get(provider.name),
            }
            for provider in available_providers()
        ],
        "last_run_at": row.get("last_run_at"),
        "encryption": {"passphrase_set": bool(read_value(db, "cloud_backup_passphrase"))},
        "backup_time": settings.backup_time,
        "backup_keep": settings.backup_keep,
    }


def list_remote(db: Session) -> dict:
    """Every connected provider's backups; one failing lists as its error."""
    providers: dict[str, dict] = {}
    for provider in available_providers():
        if not provider.connected(db):
            continue
        try:
            providers[provider.name] = {
                "items": [
                    {
                        "id": backup.id,
                        "name": backup.name,
                        "size": backup.size,
                        "modified_at": backup.modified_at.isoformat()
                        if backup.modified_at
                        else None,
                        "encrypted": backup.encrypted,
                    }
                    for backup in provider.list_backups(db)
                ]
            }
        except CloudBackupError as exc:
            providers[provider.name] = {"error": str(exc)}
        except Exception:  # noqa: BLE001 — one provider must not hide the other's list
            logger.exception("cloud backup listing failed (%s)", provider.name)
            providers[provider.name] = {"error": f"falha ao consultar o {provider.label}"}
    return {"providers": providers}


def restore_from_cloud(
    db: Session,
    provider_name: str,
    backup_id: str,
    passphrase: str | None = None,
    name: str | None = None,
    confirm_replace: bool = False,
) -> dict:
    """Download one backup and hand it to the regular full import.

    ``import_snapshot`` keeps all of its own rules — refusing a non-empty
    installation, refusing a schema mismatch — and its pt-BR errors surface
    unchanged. ``confirm_replace`` is the exception the UI grants only after
    the user types the confirmation phrase: the current data is dumped to the
    local backup directory first, then wiped and replaced by the snapshot.
    Still a clone, never a merge.
    """
    from app.db.models import Transaction

    provider = get_provider(provider_name)
    payload = provider.download(db, backup_id)
    filename = name or backup_id.rsplit("/", 1)[-1] or f"cloud-{provider_name}{FILE_EXTENSION}"
    if is_encrypted(payload):
        effective = (passphrase or "").strip() or read_value(db, "cloud_backup_passphrase")
        payload = decrypt(payload, effective)
        if filename.endswith(ENCRYPTED_EXTENSION):
            filename = filename[: -len(".enc")]

    replacing = confirm_replace and db.query(Transaction.id).first() is not None
    safety: dict | None = None
    if replacing:
        # The typed confirmation authorises the wipe, but the current data
        # still gets one last local dump — "tenho certeza" must never be the
        # only thing standing between the user and an irreversible mistake.
        safety = backup_database()
        if safety.get("status") != "ok":
            raise CloudBackupError(
                "não foi possível criar o backup local de segurança antes da "
                "restauração; nada foi apagado — tente de novo"
            )

    result = import_snapshot(db, payload, filename, replace_existing=confirm_replace)
    if replacing:
        # After import_snapshot: it wipes audit_logs along with everything
        # else, so a row added earlier would not survive the restore.
        db.add(
            AuditLog(
                action="backup.cloud_restore_replace",
                detail={
                    "provider": provider_name,
                    "file": filename,
                    "safety_dump": str((safety or {}).get("file")),
                },
            )
        )
        db.commit()
    return result


def disconnect(db: Session, provider_name: str) -> None:
    """Forget a provider's tokens and drop its entry from the status row."""
    provider = get_provider(provider_name)
    provider.disconnect(db)
    row = read_row(db, STATUS_KEY)
    if row and provider_name in (row.get("providers") or {}):
        row["providers"].pop(provider_name)
        write_row(db, STATUS_KEY, row)
