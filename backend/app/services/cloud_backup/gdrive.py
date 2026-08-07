"""Google Drive via the OAuth device-code flow.

The user creates their own OAuth client (type "TVs and Limited Input
devices") and pastes its id and secret; authorization is a short code typed
at google.com/device, so no redirect URI has to exist — the same flow works
under Docker and on the desktop build's variable port. Scope is
``drive.file``: the app only ever sees files it created, which also makes
rotation safe by construction.

The device_code stays server-side (an app_settings row); the browser only
ever sees the user_code. Access tokens are refreshed per operation and never
persisted — two token calls a day cost nothing and remove every expiry edge
case.
"""
from __future__ import annotations

import json
import time
import uuid

import httpx
from sqlalchemy.orm import Session

from app.services.cloud_backup.base import (
    CloudBackupProvider,
    RemoteBackup,
    _client,
    is_backup_name,
    parse_remote_dt,
    read_row,
    read_value,
    store_token,
    write_row,
)
from app.services.cloud_backup.crypto import ENCRYPTED_EXTENSION, CloudBackupError


DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.file"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_NAME = "GumbInvest"
FLOW_KEY = "gdrive_device_flow"
FOLDER_KEY = "gdrive_folder_id"


def _payload(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _check(resp: httpx.Response, action: str) -> dict:
    if resp.status_code // 100 != 2:
        raise CloudBackupError(f"falha ao {action} (HTTP {resp.status_code})")
    return _payload(resp)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _multipart(filename: str, folder_id: str, payload: bytes) -> tuple[bytes, str]:
    """Drive's multipart/related upload body — httpx only speaks form-data."""
    boundary = "gumbinvest-" + uuid.uuid4().hex
    meta = json.dumps(
        {"name": filename, "parents": [folder_id], "appProperties": {"app": "gumbinvest"}}
    )
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
        f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/related; boundary={boundary}"


class GoogleDriveProvider(CloudBackupProvider):
    name = "gdrive"
    label = "Google Drive"

    def _credentials(self, db: Session) -> tuple[str, str]:
        client_id = read_value(db, "gdrive_client_id")
        client_secret = read_value(db, "gdrive_client_secret")
        if not client_id or not client_secret:
            raise CloudBackupError(
                "configure o client ID e o client secret do Google Drive antes de conectar"
            )
        return client_id, client_secret

    def configured(self, db: Session) -> bool:
        return bool(read_value(db, "gdrive_client_id") and read_value(db, "gdrive_client_secret"))

    def connected(self, db: Session) -> bool:
        return bool(read_value(db, "gdrive_refresh_token"))

    # -- authorization -----------------------------------------------------
    def start_device_flow(self, db: Session) -> dict:
        client_id, _ = self._credentials(db)
        with _client() as client:
            resp = client.post(DEVICE_CODE_URL, data={"client_id": client_id, "scope": SCOPE})
        data = _payload(resp)
        if resp.status_code != 200 or "device_code" not in data:
            raise CloudBackupError(
                "o Google recusou o início da conexão; confira o client ID "
                f"(HTTP {resp.status_code})"
            )
        interval = int(data.get("interval", 5))
        write_row(
            db,
            FLOW_KEY,
            {
                "device_code": data["device_code"],
                "interval": interval,
                "expires_at": time.time() + int(data.get("expires_in", 1800)),
            },
        )
        return {
            "verification_url": data.get("verification_url")
            or data.get("verification_uri")
            or "https://google.com/device",
            "user_code": data["user_code"],
            "expires_in": int(data.get("expires_in", 1800)),
            "interval": interval,
        }

    def poll_device_flow(self, db: Session) -> dict:
        client_id, client_secret = self._credentials(db)
        flow = read_row(db, FLOW_KEY)
        if not flow:
            raise CloudBackupError("nenhuma conexão em andamento; clique em Conectar para começar")
        if time.time() > float(flow.get("expires_at", 0)):
            write_row(db, FLOW_KEY, None)
            raise CloudBackupError("o código expirou; inicie a conexão de novo")
        with _client() as client:
            resp = client.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "device_code": flow["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
        data = _payload(resp)
        error = data.get("error")
        if error == "authorization_pending":
            return {"status": "pending", "interval": int(flow.get("interval", 5))}
        if error == "slow_down":
            flow["interval"] = int(flow.get("interval", 5)) + 5
            write_row(db, FLOW_KEY, flow)
            return {"status": "pending", "interval": flow["interval"]}
        if error == "access_denied":
            write_row(db, FLOW_KEY, None)
            raise CloudBackupError("autorização negada no Google; inicie de novo se mudou de ideia")
        if error == "expired_token":
            write_row(db, FLOW_KEY, None)
            raise CloudBackupError("o código expirou; inicie a conexão de novo")
        if resp.status_code != 200 or not data.get("refresh_token"):
            raise CloudBackupError("o Google não devolveu a autorização esperada; tente de novo")
        store_token(db, "gdrive_refresh_token", data["refresh_token"])
        write_row(db, FLOW_KEY, None)
        return {"status": "connected"}

    def disconnect(self, db: Session) -> None:
        store_token(db, "gdrive_refresh_token", "")
        write_row(db, FLOW_KEY, None)
        write_row(db, FOLDER_KEY, None)

    def _access_token(self, db: Session) -> str:
        client_id, client_secret = self._credentials(db)
        refresh_token = read_value(db, "gdrive_refresh_token")
        if not refresh_token:
            raise CloudBackupError("o Google Drive não está conectado")
        with _client() as client:
            resp = client.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        data = _payload(resp)
        if data.get("error") == "invalid_grant":
            # Revoked or expired upstream: forget it now so the UI shows
            # "desconectado" instead of failing the same way every night.
            store_token(db, "gdrive_refresh_token", "")
            raise CloudBackupError("a conexão com o Google Drive expirou; reconecte na aba Backup")
        if resp.status_code != 200 or "access_token" not in data:
            raise CloudBackupError("falha ao renovar o acesso ao Google Drive; tente de novo")
        return data["access_token"]

    # -- file operations ---------------------------------------------------
    def _folder_id(self, db: Session, client: httpx.Client, token: str, *, recreate: bool = False) -> str:
        cached = read_row(db, FOLDER_KEY)
        if cached and cached.get("id") and not recreate:
            return cached["id"]
        resp = client.get(
            FILES_URL,
            params={
                "q": (
                    f"name = '{FOLDER_NAME}' and "
                    "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
                ),
                "fields": "files(id)",
            },
            headers=_auth(token),
        )
        files = _check(resp, "localizar a pasta GumbInvest no Drive").get("files") or []
        if files:
            folder_id = files[0]["id"]
        else:
            resp = client.post(
                FILES_URL,
                json={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
                headers=_auth(token),
            )
            data = _check(resp, "criar a pasta GumbInvest no Drive")
            if "id" not in data:
                raise CloudBackupError("não foi possível criar a pasta GumbInvest no Drive")
            folder_id = data["id"]
        write_row(db, FOLDER_KEY, {"id": folder_id})
        return folder_id

    def upload(self, db: Session, filename: str, payload: bytes) -> RemoteBackup:
        token = self._access_token(db)
        with _client() as client:
            for recreate in (False, True):
                folder_id = self._folder_id(db, client, token, recreate=recreate)
                body, content_type = _multipart(filename, folder_id, payload)
                resp = client.post(
                    UPLOAD_URL,
                    params={"uploadType": "multipart"},
                    content=body,
                    headers={**_auth(token), "Content-Type": content_type},
                )
                # 404 here means the cached folder was deleted or trashed —
                # recreate it once and retry before giving up.
                if resp.status_code != 404:
                    break
        data = _check(resp, "enviar o backup para o Google Drive")
        return RemoteBackup(
            id=data.get("id", ""),
            name=data.get("name", filename),
            size=len(payload),
            modified_at=None,
            encrypted=filename.endswith(ENCRYPTED_EXTENSION),
        )

    def list_backups(self, db: Session) -> list[RemoteBackup]:
        token = self._access_token(db)
        with _client() as client:
            for recreate in (False, True):
                folder_id = self._folder_id(db, client, token, recreate=recreate)
                resp = client.get(
                    FILES_URL,
                    params={
                        "q": f"'{folder_id}' in parents and trashed = false",
                        "fields": "files(id,name,size,modifiedTime)",
                        "orderBy": "modifiedTime desc",
                        "pageSize": 100,
                    },
                    headers=_auth(token),
                )
                if resp.status_code != 404:
                    break
        data = _check(resp, "listar os backups no Google Drive")
        return [
            RemoteBackup(
                id=item["id"],
                name=item["name"],
                size=int(item["size"]) if item.get("size") else None,
                modified_at=parse_remote_dt(item.get("modifiedTime")),
                encrypted=item["name"].endswith(ENCRYPTED_EXTENSION),
            )
            for item in data.get("files", [])
            if is_backup_name(item.get("name", ""))
        ]

    def download(self, db: Session, backup_id: str) -> bytes:
        token = self._access_token(db)
        with _client() as client:
            resp = client.get(
                f"{FILES_URL}/{backup_id}", params={"alt": "media"}, headers=_auth(token)
            )
        if resp.status_code // 100 != 2:
            raise CloudBackupError(
                f"falha ao baixar o backup do Google Drive (HTTP {resp.status_code})"
            )
        return resp.content

    def delete(self, db: Session, backup_id: str) -> None:
        token = self._access_token(db)
        with _client() as client:
            resp = client.delete(f"{FILES_URL}/{backup_id}", headers=_auth(token))
        if resp.status_code // 100 != 2 and resp.status_code != 404:
            raise CloudBackupError(
                f"falha ao apagar um backup antigo no Google Drive (HTTP {resp.status_code})"
            )
