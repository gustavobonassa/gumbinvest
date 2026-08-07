"""Cloud backup: sync ``.gumbinvest`` exports to the user's own cloud storage."""
from app.services.cloud_backup.base import (
    INTERNAL_KEYS,
    CloudBackupProvider,
    RemoteBackup,
    available_providers,
    get_provider,
    register,
)
from app.services.cloud_backup.crypto import CloudBackupError, decrypt, encrypt, is_encrypted
from app.services.cloud_backup.dropbox import DropboxProvider
from app.services.cloud_backup.gdrive import GoogleDriveProvider
from app.services.cloud_backup.service import (
    STATUS_KEY,
    disconnect,
    list_remote,
    restore_from_cloud,
    status_payload,
    sync_to_cloud,
)

register(GoogleDriveProvider())
register(DropboxProvider())

__all__ = [
    "INTERNAL_KEYS",
    "STATUS_KEY",
    "CloudBackupError",
    "CloudBackupProvider",
    "DropboxProvider",
    "GoogleDriveProvider",
    "RemoteBackup",
    "available_providers",
    "decrypt",
    "disconnect",
    "encrypt",
    "get_provider",
    "is_encrypted",
    "list_remote",
    "register",
    "restore_from_cloud",
    "status_payload",
    "sync_to_cloud",
]
