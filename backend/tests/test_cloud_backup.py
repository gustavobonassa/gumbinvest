"""Cloud backup: encryption, the OAuth flows against a mock transport, and
the sync/restore orchestration with a fake provider."""
from __future__ import annotations

import gzip
import json

import httpx
import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AppSetting, Portfolio
from app.services.cloud_backup import base as cloud_base
from app.services.cloud_backup import dropbox as dropbox_mod
from app.services.cloud_backup import gdrive as gdrive_mod
from app.services.cloud_backup import service as cloud_service
from app.services.cloud_backup.base import RemoteBackup, read_row, read_value
from app.services.cloud_backup.crypto import (
    CloudBackupError,
    decrypt,
    encrypt,
    is_encrypted,
)
from app.services.full_backup import FullBackupError, export_snapshot


# --------------------------------------------------------------------------
# Encryption
# --------------------------------------------------------------------------
def test_encrypt_roundtrip():
    payload = gzip.compress(b'{"format": "gumbinvest-export"}')
    sealed = encrypt(payload, "senha secreta")
    assert is_encrypted(sealed)
    assert not is_encrypted(payload)
    assert decrypt(sealed, "senha secreta") == payload


def test_wrong_passphrase_is_a_readable_error():
    sealed = encrypt(b"dados", "certa")
    with pytest.raises(CloudBackupError, match="senha incorreta"):
        decrypt(sealed, "errada")


def test_tampering_is_detected():
    sealed = bytearray(encrypt(b"dados", "senha"))
    sealed[-1] ^= 0xFF
    with pytest.raises(CloudBackupError, match="senha incorreta ou arquivo corrompido"):
        decrypt(bytes(sealed), "senha")


def test_truncated_payload_is_refused():
    with pytest.raises(CloudBackupError):
        decrypt(b"GMBENC1\0short", "senha")


def test_missing_passphrase_asks_for_one():
    sealed = encrypt(b"dados", "senha")
    with pytest.raises(CloudBackupError, match="informe a senha"):
        decrypt(sealed, "")


def test_unicode_passphrase():
    sealed = encrypt(b"dados", "coração-ção-ü")
    assert decrypt(sealed, "coração-ção-ü") == b"dados"


def test_backup_name_detection():
    assert cloud_base.is_backup_name("gumbinvest-20260806-033000.gumbinvest")
    assert cloud_base.is_backup_name("gumbinvest-20260806-033000.gumbinvest.enc")
    assert not cloud_base.is_backup_name("ferias.jpg")
    assert not cloud_base.is_backup_name("outro-20260806.gumbinvest.zip")


# --------------------------------------------------------------------------
# Credential reads: DB row first, env fallback
# --------------------------------------------------------------------------
def test_read_value_prefers_the_stored_row(db: Session, monkeypatch):
    monkeypatch.setattr(settings, "gdrive_client_id", "do-env")
    assert read_value(db, "gdrive_client_id") == "do-env"
    db.merge(AppSetting(key="gdrive_client_id", value={"value": "da-ui"}))
    db.commit()
    assert read_value(db, "gdrive_client_id") == "da-ui"


# --------------------------------------------------------------------------
# Google Drive: device flow and token refresh against a mock transport
# --------------------------------------------------------------------------
@pytest.fixture
def gdrive(db: Session, monkeypatch):
    monkeypatch.setattr(settings, "gdrive_client_id", "cid")
    monkeypatch.setattr(settings, "gdrive_client_secret", "csec")
    # monkeypatch snapshots the original, so a store_secret() inside a test
    # cannot leak the token into the singleton for later tests.
    monkeypatch.setattr(settings, "gdrive_refresh_token", "")
    return gdrive_mod.GoogleDriveProvider()


def _mock_transport(monkeypatch, handler):
    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(gdrive_mod, "_client", factory)
    monkeypatch.setattr(dropbox_mod, "_client", factory)


def test_gdrive_device_flow_end_to_end(db: Session, monkeypatch, gdrive):
    token_calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == gdrive_mod.DEVICE_CODE_URL:
            return httpx.Response(
                200,
                json={
                    "device_code": "dev123",
                    "user_code": "ABCD-EFGH",
                    "verification_url": "https://www.google.com/device",
                    "expires_in": 1800,
                    "interval": 5,
                },
            )
        if request.url == gdrive_mod.TOKEN_URL:
            token_calls.append(dict(httpx.QueryParams(request.read().decode())))
            script = [
                (428, {"error": "authorization_pending"}),
                (403, {"error": "slow_down"}),
                (200, {"access_token": "at", "refresh_token": "rt-final"}),
            ]
            status, body = script[min(len(token_calls), len(script)) - 1]
            return httpx.Response(status, json=body)
        raise AssertionError(f"unexpected URL {request.url}")

    _mock_transport(monkeypatch, handler)

    started = gdrive.start_device_flow(db)
    assert started["user_code"] == "ABCD-EFGH"
    assert read_row(db, gdrive_mod.FLOW_KEY)["device_code"] == "dev123"

    assert gdrive.poll_device_flow(db) == {"status": "pending", "interval": 5}
    # slow_down widens the interval the client is told to poll at
    assert gdrive.poll_device_flow(db) == {"status": "pending", "interval": 10}
    assert gdrive.poll_device_flow(db) == {"status": "connected"}

    assert token_calls[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert token_calls[0]["device_code"] == "dev123"
    assert read_value(db, "gdrive_refresh_token") == "rt-final"
    assert read_row(db, gdrive_mod.FLOW_KEY) is None


def test_gdrive_denied_authorization_clears_the_flow(db: Session, monkeypatch, gdrive):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "access_denied"})

    _mock_transport(monkeypatch, handler)
    cloud_base.write_row(db, gdrive_mod.FLOW_KEY, {"device_code": "d", "interval": 5, "expires_at": 9e12})
    with pytest.raises(CloudBackupError, match="negada"):
        gdrive.poll_device_flow(db)
    assert read_row(db, gdrive_mod.FLOW_KEY) is None


def test_gdrive_invalid_grant_disconnects(db: Session, monkeypatch, gdrive):
    from app.services.secrets import store_secret

    store_secret(db, "gdrive_refresh_token", "revogado")
    db.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    _mock_transport(monkeypatch, handler)
    with pytest.raises(CloudBackupError, match="reconecte"):
        gdrive._access_token(db)
    assert not read_value(db, "gdrive_refresh_token")


def test_gdrive_upload_recreates_a_deleted_folder(db: Session, monkeypatch, gdrive):
    from app.services.secrets import store_secret

    store_secret(db, "gdrive_refresh_token", "rt")
    # A folder id cached from a previous run, since deleted by the user.
    cloud_base.write_row(db, gdrive_mod.FOLDER_KEY, {"id": "gone"})
    uploads: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == gdrive_mod.TOKEN_URL:
            return httpx.Response(200, json={"access_token": "at"})
        if str(request.url).startswith(gdrive_mod.UPLOAD_URL):
            uploads.append(request)
            if len(uploads) == 1:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": "file1", "name": "gumbinvest-x.gumbinvest"})
        if request.method == "GET" and str(request.url).startswith(gdrive_mod.FILES_URL):
            return httpx.Response(200, json={"files": []})
        if request.method == "POST" and str(request.url).startswith(gdrive_mod.FILES_URL):
            return httpx.Response(200, json={"id": "folder-nova"})
        raise AssertionError(f"unexpected URL {request.url}")

    _mock_transport(monkeypatch, handler)
    remote = gdrive.upload(db, "gumbinvest-x.gumbinvest", b"payload")
    assert remote.id == "file1"
    assert len(uploads) == 2
    assert b"payload" in uploads[1].read()
    assert b'"parents": ["folder-nova"]' in uploads[1].read()
    assert read_row(db, gdrive_mod.FOLDER_KEY) == {"id": "folder-nova"}


# --------------------------------------------------------------------------
# Dropbox: PKCE flow
# --------------------------------------------------------------------------
def test_dropbox_pkce_flow(db: Session, monkeypatch):
    monkeypatch.setattr(settings, "dropbox_app_key", "appkey")
    monkeypatch.setattr(settings, "dropbox_refresh_token", "")
    provider = dropbox_mod.DropboxProvider()

    url = provider.build_authorize_url(db)
    assert "code_challenge=" in url and "token_access_type=offline" in url
    assert "code_verifier" not in url  # only the challenge travels
    verifier = read_row(db, dropbox_mod.PKCE_KEY)["verifier"]

    exchanged: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        exchanged.append(dict(httpx.QueryParams(request.read().decode())))
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "drt"})

    _mock_transport(monkeypatch, handler)
    provider.complete_authorization(db, "  o-codigo  ")
    assert exchanged[0]["code"] == "o-codigo"
    assert exchanged[0]["code_verifier"] == verifier
    assert read_value(db, "dropbox_refresh_token") == "drt"
    assert read_row(db, dropbox_mod.PKCE_KEY) is None


# --------------------------------------------------------------------------
# Orchestration with a fake provider
# --------------------------------------------------------------------------
class FakeProvider(cloud_base.CloudBackupProvider):
    name = "fake"
    label = "Fake Cloud"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.connected_flag = True
        self.fail_upload = False

    def configured(self, db) -> bool:
        return True

    def connected(self, db) -> bool:
        return self.connected_flag

    def upload(self, db, filename: str, payload: bytes) -> RemoteBackup:
        if self.fail_upload:
            raise CloudBackupError("provedor indisponível")
        self.files[filename] = payload
        return self._remote(filename)

    def list_backups(self, db) -> list[RemoteBackup]:
        return [self._remote(name) for name in sorted(self.files, reverse=True)]

    def download(self, db, backup_id: str) -> bytes:
        return self.files[backup_id]

    def delete(self, db, backup_id: str) -> None:
        self.files.pop(backup_id, None)

    def disconnect(self, db) -> None:
        self.connected_flag = False

    def _remote(self, name: str) -> RemoteBackup:
        return RemoteBackup(
            id=name,
            name=name,
            size=len(self.files.get(name, b"")),
            modified_at=None,
            encrypted=name.endswith(".enc"),
        )


@pytest.fixture
def fake_provider():
    provider = FakeProvider()
    saved = dict(cloud_base._REGISTRY)
    cloud_base._REGISTRY.clear()
    cloud_base.register(provider)
    yield provider
    cloud_base._REGISTRY.clear()
    cloud_base._REGISTRY.update(saved)


def test_sync_skips_when_nothing_is_connected(db: Session, fake_provider):
    fake_provider.connected_flag = False
    assert cloud_service.sync_to_cloud()["status"] == "skipped"


def test_sync_uploads_a_valid_export(db: Session, portfolio: Portfolio, fake_provider):
    result = cloud_service.sync_to_cloud()
    assert result["status"] == "ok"
    assert result["providers"]["fake"]["state"] == "ok"
    (name,) = fake_provider.files
    assert cloud_base.is_backup_name(name) and not name.endswith(".enc")
    document = json.loads(gzip.decompress(fake_provider.files[name]))
    assert document["format"] == "gumbinvest-export"
    db.expire_all()
    status = read_row(db, cloud_service.STATUS_KEY)
    assert status["providers"]["fake"]["state"] == "ok"


def test_sync_encrypts_when_a_passphrase_is_set(db: Session, portfolio: Portfolio, fake_provider):
    db.merge(AppSetting(key="cloud_backup_passphrase", value={"value": "senha"}))
    db.commit()
    cloud_service.sync_to_cloud()
    (name,) = fake_provider.files
    assert name.endswith(".gumbinvest.enc")
    payload = fake_provider.files[name]
    assert is_encrypted(payload)
    assert json.loads(gzip.decompress(decrypt(payload, "senha")))["format"] == "gumbinvest-export"


def test_sync_rotates_beyond_backup_keep(db: Session, portfolio: Portfolio, fake_provider, monkeypatch):
    monkeypatch.setattr(settings, "backup_keep", 3)
    for stamp in ("20200101-000000", "20200102-000000", "20200103-000000", "20200104-000000"):
        fake_provider.files[f"gumbinvest-{stamp}.gumbinvest"] = b"velho"
    fake_provider.files["ferias.gumbinvest.txt"] = b"do usuario"  # never ours to delete
    cloud_service.sync_to_cloud()
    ours = sorted(name for name in fake_provider.files if cloud_base.is_backup_name(name))
    assert len(ours) == 3
    assert "gumbinvest-20200101-000000.gumbinvest" not in fake_provider.files
    assert "gumbinvest-20200102-000000.gumbinvest" not in fake_provider.files
    assert "ferias.gumbinvest.txt" in fake_provider.files


def test_one_provider_failing_does_not_stop_the_other(db: Session, portfolio: Portfolio):
    good, bad = FakeProvider(), FakeProvider()
    bad.name, bad.label = "bad", "Bad Cloud"
    bad.fail_upload = True
    saved = dict(cloud_base._REGISTRY)
    cloud_base._REGISTRY.clear()
    cloud_base.register(good)
    cloud_base.register(bad)
    try:
        result = cloud_service.sync_to_cloud()
    finally:
        cloud_base._REGISTRY.clear()
        cloud_base._REGISTRY.update(saved)
    assert result["status"] == "partial"
    assert result["providers"]["fake"]["state"] == "ok"
    assert result["providers"]["bad"] == {
        "state": "error",
        "file": None,
        "size": None,
        "at": result["providers"]["bad"]["at"],
        "message": "provedor indisponível",
    }
    assert len(good.files) == 1 and not bad.files


def test_failed_upload_reaches_the_notification_bell(db: Session, portfolio: Portfolio, fake_provider):
    from app.services.notifications import feed

    fake_provider.fail_upload = True
    cloud_service.sync_to_cloud()
    db.expire_all()
    items = [item for item in feed(db, portfolio.id) if item["kind"] == "cloud_backup"]
    assert len(items) == 1
    assert items[0]["level"] == "warning"
    assert "Fake Cloud: provedor indisponível" in items[0]["body"]


def test_restore_roundtrip_including_encryption(db: Session, portfolio: Portfolio, fake_provider):
    payload = export_snapshot(db)
    fake_provider.files["gumbinvest-x.gumbinvest.enc"] = encrypt(payload, "senha")

    with pytest.raises(CloudBackupError, match="senha incorreta"):
        cloud_service.restore_from_cloud(db, "fake", "gumbinvest-x.gumbinvest.enc", "errada")

    result = cloud_service.restore_from_cloud(db, "fake", "gumbinvest-x.gumbinvest.enc", "senha")
    assert result["status"] == "COMPLETED"
    # the .enc suffix is dropped once decrypted — the batch records a .gumbinvest
    assert result["filename"] == "gumbinvest-x.gumbinvest"


def test_restore_still_refuses_a_non_empty_installation(db: Session, portfolio: Portfolio, fake_provider):
    from datetime import date
    from decimal import Decimal

    from app.db.models import Asset, Transaction

    payload = export_snapshot(db)
    fake_provider.files["gumbinvest-x.gumbinvest"] = payload
    asset = Asset(ticker="PETR4", name="Petrobras", kind="stock")
    db.add(asset)
    db.flush()
    db.add(
        Transaction(
            portfolio_id=portfolio.id,
            asset_id=asset.id,
            trade_date=date(2024, 3, 1),
            direction="CREDIT",
            op_type="BUY",
            effect="POSITION",
            quantity=Decimal("1"),
            unit_price=Decimal("10"),
            gross_amount=Decimal("10"),
            dedup_key="petr4|2024-03-01|buy|1",
            raw_movement="Compra",
            raw_product="PETR4 - PETROBRAS",
        )
    )
    db.commit()
    with pytest.raises(FullBackupError, match="instalação vazia"):
        cloud_service.restore_from_cloud(db, "fake", "gumbinvest-x.gumbinvest")


def test_disconnect_forgets_tokens_and_status(db: Session, portfolio: Portfolio, fake_provider):
    fake_provider.fail_upload = True
    cloud_service.sync_to_cloud()
    db.expire_all()
    assert read_row(db, cloud_service.STATUS_KEY)["providers"]["fake"]
    cloud_service.disconnect(db, "fake")
    assert not fake_provider.connected_flag
    assert "fake" not in read_row(db, cloud_service.STATUS_KEY)["providers"]
