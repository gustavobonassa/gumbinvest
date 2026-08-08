"""Dropbox via the no-redirect PKCE flow.

The user creates a scoped app (app-folder access) and pastes its app key —
no secret exists anywhere. Authorizing without a ``redirect_uri`` makes
Dropbox display the auth code for the user to copy back into Configurações:
the same paste-a-code experience as the Google device flow, and equally
indifferent to Docker vs the desktop build's variable port.

``token_access_type=offline`` yields a refresh token, which Dropbox never
expires; access tokens are fetched per operation and not persisted. With
app-folder access every path is relative to ``Apps/<app name>/``, so backups
live at the folder root and rotation can only ever touch our own files.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets as pysecrets
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.services.cloud_backup.base import (
    CloudBackupProvider,
    RemoteBackup,
    _client,
    error_detail,
    is_backup_name,
    parse_remote_dt,
    read_row,
    read_value,
    store_token,
    write_row,
)
from app.services.cloud_backup.crypto import ENCRYPTED_EXTENSION, CloudBackupError


AUTHORIZE_URL = "https://www.dropbox.com/oauth2/authorize"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
API_URL = "https://api.dropboxapi.com/2"
CONTENT_URL = "https://content.dropboxapi.com/2"
PKCE_KEY = "dropbox_pkce"
#: Requested explicitly so a Dropbox app whose Permissions tab was never
#: touched fails at the authorize page — not at 03:30 with an upload error.
SCOPES = "files.metadata.read files.content.read files.content.write"

_MISSING_SCOPE_HINT = (
    "o app do Dropbox não tem as permissões de arquivo — marque "
    "files.metadata.read, files.content.read e files.content.write na aba "
    "Permissions do App Console e reconecte o Dropbox"
)


def _payload(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _check(resp: httpx.Response, action: str) -> dict:
    if resp.status_code // 100 != 2:
        detail = error_detail(resp)
        if "scope" in detail:
            raise CloudBackupError(_MISSING_SCOPE_HINT)
        suffix = f": {detail}" if detail else ""
        raise CloudBackupError(f"falha ao {action} (HTTP {resp.status_code}{suffix})")
    return _payload(resp)


def _entry(item: dict) -> RemoteBackup:
    name = item.get("name", "")
    return RemoteBackup(
        id=item.get("path_lower") or f"/{name}",
        name=name,
        size=item.get("size"),
        modified_at=parse_remote_dt(item.get("server_modified")),
        encrypted=name.endswith(ENCRYPTED_EXTENSION),
    )


class DropboxProvider(CloudBackupProvider):
    name = "dropbox"
    label = "Dropbox"

    def _app_key(self, db: Session) -> str:
        app_key = read_value(db, "dropbox_app_key")
        if not app_key:
            raise CloudBackupError("configure a app key do Dropbox antes de conectar")
        return app_key

    def configured(self, db: Session) -> bool:
        return bool(read_value(db, "dropbox_app_key"))

    def connected(self, db: Session) -> bool:
        return bool(read_value(db, "dropbox_refresh_token"))

    # -- authorization -----------------------------------------------------
    def build_authorize_url(self, db: Session) -> str:
        app_key = self._app_key(db)
        verifier = pysecrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        write_row(db, PKCE_KEY, {"verifier": verifier})
        query = urlencode(
            {
                "client_id": app_key,
                "response_type": "code",
                "token_access_type": "offline",
                "scope": SCOPES,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    def complete_authorization(self, db: Session, code: str) -> None:
        app_key = self._app_key(db)
        pkce = read_row(db, PKCE_KEY)
        if not pkce:
            raise CloudBackupError(
                "nenhuma conexão em andamento; clique em Conectar para gerar um novo código"
            )
        with _client() as client:
            resp = client.post(
                TOKEN_URL,
                data={
                    "code": code.strip(),
                    "grant_type": "authorization_code",
                    "client_id": app_key,
                    "code_verifier": pkce["verifier"],
                },
            )
        data = _payload(resp)
        if resp.status_code != 200 or not data.get("refresh_token"):
            raise CloudBackupError("o Dropbox recusou o código; gere um novo e tente de novo")
        store_token(db, "dropbox_refresh_token", data["refresh_token"])
        write_row(db, PKCE_KEY, None)

    def disconnect(self, db: Session) -> None:
        store_token(db, "dropbox_refresh_token", "")
        write_row(db, PKCE_KEY, None)

    def _access_token(self, db: Session) -> str:
        app_key = self._app_key(db)
        refresh_token = read_value(db, "dropbox_refresh_token")
        if not refresh_token:
            raise CloudBackupError("o Dropbox não está conectado")
        with _client() as client:
            resp = client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": app_key,
                },
            )
        data = _payload(resp)
        if resp.status_code in (400, 401) and "invalid_grant" in resp.text:
            # Revoked upstream: forget it so the UI shows "desconectado"
            # instead of failing the same way every night.
            store_token(db, "dropbox_refresh_token", "")
            raise CloudBackupError("a conexão com o Dropbox expirou; reconecte na aba Backup")
        if resp.status_code != 200 or "access_token" not in data:
            raise CloudBackupError("falha ao renovar o acesso ao Dropbox; tente de novo")
        return data["access_token"]

    # -- file operations ---------------------------------------------------
    def upload(self, db: Session, filename: str, payload: bytes) -> RemoteBackup:
        token = self._access_token(db)
        with _client() as client:
            resp = client.post(
                f"{CONTENT_URL}/files/upload",
                content=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Dropbox-API-Arg": json.dumps(
                        {"path": f"/{filename}", "mode": "add", "autorename": True, "mute": True}
                    ),
                    "Content-Type": "application/octet-stream",
                },
            )
        data = _check(resp, "enviar o backup para o Dropbox")
        remote = _entry(data)
        return RemoteBackup(
            id=remote.id or f"/{filename}",
            name=remote.name or filename,
            size=remote.size if remote.size is not None else len(payload),
            modified_at=remote.modified_at,
            encrypted=filename.endswith(ENCRYPTED_EXTENSION),
        )

    def list_backups(self, db: Session) -> list[RemoteBackup]:
        token = self._access_token(db)
        headers = {"Authorization": f"Bearer {token}"}
        entries: list[dict] = []
        with _client() as client:
            resp = client.post(f"{API_URL}/files/list_folder", json={"path": ""}, headers=headers)
            data = _check(resp, "listar os backups no Dropbox")
            entries.extend(data.get("entries", []))
            while data.get("has_more"):
                resp = client.post(
                    f"{API_URL}/files/list_folder/continue",
                    json={"cursor": data["cursor"]},
                    headers=headers,
                )
                data = _check(resp, "listar os backups no Dropbox")
                entries.extend(data.get("entries", []))
        backups = [
            _entry(item)
            for item in entries
            if item.get(".tag") == "file" and is_backup_name(item.get("name", ""))
        ]
        backups.sort(key=lambda backup: backup.name, reverse=True)
        return backups

    def download(self, db: Session, backup_id: str) -> bytes:
        token = self._access_token(db)
        with _client() as client:
            resp = client.post(
                f"{CONTENT_URL}/files/download",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Dropbox-API-Arg": json.dumps({"path": backup_id}),
                },
            )
        if resp.status_code // 100 != 2:
            _check(resp, "baixar o backup do Dropbox")
        return resp.content

    def delete(self, db: Session, backup_id: str) -> None:
        token = self._access_token(db)
        with _client() as client:
            resp = client.post(
                f"{API_URL}/files/delete_v2",
                json={"path": backup_id},
                headers={"Authorization": f"Bearer {token}"},
            )
        # 409 is Dropbox's "not found" for path operations — already gone.
        if resp.status_code // 100 != 2 and resp.status_code != 409:
            raise CloudBackupError(
                f"falha ao apagar um backup antigo no Dropbox (HTTP {resp.status_code})"
            )
