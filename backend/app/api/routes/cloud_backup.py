"""Cloud backup: connect Google Drive/Dropbox, send now, list and restore."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import DbSession
from app.core.logging import get_logger
from app.services.cloud_backup import (
    CloudBackupError,
    DropboxProvider,
    GoogleDriveProvider,
    available_providers,
    get_provider,
)
from app.services.cloud_backup import service as cloud_service
from app.services.full_backup import FullBackupError
from app.services.jobs import BackgroundJob, JobConflict, JobRegistry, job_payload

router = APIRouter(prefix="/cloud-backup", tags=["cloud-backup"])
logger = get_logger(__name__)

#: The manual "Enviar agora". HTTP-triggered, so the registry and its poller
#: share a process under Docker and desktop alike; the nightly worker run
#: reports through the durable status row instead (see cloud_backup.service).
_REGISTRY = JobRegistry()
_JOB_KEY = "cloud_backup"


def _gdrive() -> GoogleDriveProvider:
    provider = get_provider("gdrive")
    assert isinstance(provider, GoogleDriveProvider)
    return provider


def _dropbox() -> DropboxProvider:
    provider = get_provider("dropbox")
    assert isinstance(provider, DropboxProvider)
    return provider


@router.get("/status", response_model=None, summary="Cloud backup connections and last run")
def status(db: DbSession) -> dict:
    payload = cloud_service.status_payload(db)
    payload["job"] = job_payload(_REGISTRY.current(_JOB_KEY))
    return payload


# --------------------------------------------------------------------------
# Authorization flows
# --------------------------------------------------------------------------
@router.post("/gdrive/device/start", response_model=None, summary="Start the Google device-code flow")
def gdrive_device_start(db: DbSession) -> dict:
    try:
        result = _gdrive().start_device_flow(db)
    except CloudBackupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return result


@router.post("/gdrive/device/poll", response_model=None, summary="Poll the Google device-code flow")
def gdrive_device_poll(db: DbSession) -> dict:
    try:
        result = _gdrive().poll_device_flow(db)
    except CloudBackupError as exc:
        # The flow may have advanced state (expired code removed, token
        # cleared) before failing — that must persist.
        db.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return result


@router.post("/dropbox/authorize", response_model=None, summary="Build the Dropbox authorization URL")
def dropbox_authorize(db: DbSession) -> dict:
    try:
        url = _dropbox().build_authorize_url(db)
    except CloudBackupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return {"authorize_url": url}


class DropboxCodePayload(BaseModel):
    code: str


@router.post("/dropbox/complete", response_model=None, summary="Exchange the Dropbox code for tokens")
def dropbox_complete(payload: DropboxCodePayload, db: DbSession) -> dict:
    try:
        _dropbox().complete_authorization(db, payload.code)
    except CloudBackupError as exc:
        db.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return {"status": "connected"}


@router.post("/{provider}/disconnect", response_model=None, summary="Forget a provider's tokens")
def disconnect(provider: str, db: DbSession) -> dict:
    try:
        cloud_service.disconnect(db, provider)
    except CloudBackupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return {"status": "disconnected"}


# --------------------------------------------------------------------------
# Send / list / restore
# --------------------------------------------------------------------------
def _run_sync(job: BackgroundJob) -> None:
    job.status = "Exportando e enviando…"
    result = cloud_service.sync_to_cloud()
    job.result = result
    entries = result.get("providers") or {}
    errors = [e["message"] for e in entries.values() if e.get("state") == "error" and e.get("message")]
    if errors and not any(e.get("state") == "ok" for e in entries.values()):
        job.error = errors[0]


@router.post("/send", response_model=None, summary="Upload a fresh backup to the cloud now")
def send_now(db: DbSession) -> dict:
    if not any(p.connected(db) for p in available_providers()):
        raise HTTPException(
            status_code=422,
            detail="nenhum provedor de nuvem conectado, conecte o Google Drive ou o Dropbox primeiro",
        )
    try:
        job = _REGISTRY.start(
            _JOB_KEY,
            "cloud_backup",
            _run_sync,
            error_message="falha ao enviar o backup para a nuvem",
            logger=logger,
        )
    except JobConflict:
        raise HTTPException(status_code=409, detail="já existe um envio em andamento") from None
    return job_payload(job)


@router.get("/backups", response_model=None, summary="Backups available in the connected clouds")
def remote_backups(db: DbSession) -> dict:
    return cloud_service.list_remote(db)


class RestorePayload(BaseModel):
    provider: str
    backup_id: str
    name: str | None = None
    passphrase: str | None = None


@router.post("/restore", response_model=None, summary="Restore a cloud backup into this installation")
def restore(payload: RestorePayload, db: DbSession) -> dict:
    try:
        return cloud_service.restore_from_cloud(
            db, payload.provider, payload.backup_id, payload.passphrase, name=payload.name
        )
    except (CloudBackupError, FullBackupError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
