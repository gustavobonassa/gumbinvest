"""Cloud storage providers behind one interface, like ``app.market.providers``.

Each provider knows how to authorize against one service and move backup
files; everything above (export, encryption, rotation, restore) lives in
:mod:`app.services.cloud_backup.service` and never touches a concrete API.
Registration happens in the package ``__init__`` so adding WebDAV or S3 later
is a new module plus one ``register()`` call.

Credentials are read DB-first with the env as fallback — never the settings
singleton alone. ``apply_stored_secrets()`` runs only in the FastAPI lifespan,
so the Celery worker's singleton never sees keys saved through the UI; the
weekly sync runs in that worker.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AppSetting
from app.services.cloud_backup.crypto import ENCRYPTED_EXTENSION, CloudBackupError
from app.services.full_backup import FILE_EXTENSION

#: app_settings rows that hold cloud-backup machinery (flow state, folder ids,
#: run results) — internal, so the settings endpoint must not echo them.
INTERNAL_KEYS = (
    "gdrive_device_flow",
    "gdrive_folder_id",
    "dropbox_pkce",
    "cloud_backup_status",
)


def _client() -> httpx.Client:
    """One place to build the HTTP client, so tests can inject a MockTransport."""
    return httpx.Client(timeout=settings.request_timeout, follow_redirects=True)


def read_value(db: Session, key: str) -> str:
    """A setting's effective value: the stored row, else the env."""
    row = db.get(AppSetting, key)
    value = (row.value or {}).get("value") if row is not None else None
    if value:
        return str(value)
    return str(getattr(settings, key, "") or "")


def read_row(db: Session, key: str) -> dict | None:
    """An internal state row (device flow, folder cache, status), or None."""
    row = db.get(AppSetting, key)
    value = row.value if row is not None else None
    return value if isinstance(value, dict) else None


def write_row(db: Session, key: str, value: dict | None) -> None:
    """Set (or, with None, delete) an internal state row. Caller commits.

    Flushed at once so a ``read_row`` in the same request sees it even on a
    session with autoflush off.
    """
    if value is None:
        row = db.get(AppSetting, key)
        if row is not None:
            db.delete(row)
    else:
        db.merge(AppSetting(key=key, value=value))
    db.flush()


def store_token(db: Session, key: str, value: str) -> None:
    """``store_secret`` plus a flush, so a read later in the same request
    (connect → status, revoke → reconnect hint) sees the change immediately."""
    from app.services.secrets import store_secret

    store_secret(db, key, value)
    db.flush()


def is_backup_name(name: str) -> bool:
    """Whether a remote file is one of ours — rotation only deletes these."""
    return name.startswith("gumbinvest-") and (
        name.endswith(FILE_EXTENSION) or name.endswith(ENCRYPTED_EXTENSION)
    )


def error_detail(resp: httpx.Response) -> str:
    """The service's own explanation of a failure, trimmed for a message.

    Dropbox and Google both put the actual reason (missing scope, bad
    argument) in the body; a bare status code hides exactly the part the
    user needs to fix.
    """
    try:
        data = resp.json()
        if isinstance(data, dict):
            for key in ("error_summary", "error_description"):
                if isinstance(data.get(key), str):
                    return data[key][:300]
            error = data.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"][:300]
            if isinstance(error, str):
                return error[:300]
    except ValueError:
        pass
    return " ".join(resp.text.split())[:300]


def parse_remote_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(slots=True)
class RemoteBackup:
    """One backup file as a cloud service reports it."""

    id: str
    name: str
    size: int | None
    modified_at: datetime | None
    encrypted: bool


class CloudBackupProvider(abc.ABC):
    """What the sync/restore service needs from any cloud storage."""

    name: str
    label: str

    @abc.abstractmethod
    def configured(self, db: Session) -> bool:
        """App credentials (client id / app key) are present."""

    @abc.abstractmethod
    def connected(self, db: Session) -> bool:
        """The user completed authorization; a refresh token is stored."""

    @abc.abstractmethod
    def upload(self, db: Session, filename: str, payload: bytes) -> RemoteBackup: ...

    @abc.abstractmethod
    def list_backups(self, db: Session) -> list[RemoteBackup]: ...

    @abc.abstractmethod
    def download(self, db: Session, backup_id: str) -> bytes: ...

    @abc.abstractmethod
    def delete(self, db: Session, backup_id: str) -> None: ...

    @abc.abstractmethod
    def disconnect(self, db: Session) -> None:
        """Forget the stored tokens and any in-progress authorization."""


_REGISTRY: dict[str, CloudBackupProvider] = {}


def register(provider: CloudBackupProvider) -> None:
    _REGISTRY[provider.name] = provider


def get_provider(name: str) -> CloudBackupProvider:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise CloudBackupError("provedor de nuvem desconhecido") from None


def available_providers() -> list[CloudBackupProvider]:
    return list(_REGISTRY.values())
