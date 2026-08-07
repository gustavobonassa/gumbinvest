"""The /api/cloud-backup HTTP layer: status shape, job lifecycle, restore."""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import current_portfolio
from app.db.session import get_db
from app.main import app
from app.services.cloud_backup import base as cloud_base
from app.services.cloud_backup import encrypt
from app.services.full_backup import export_snapshot
from app.services.portfolio_registry import get_default_portfolio
from tests.test_cloud_backup import FakeProvider


@pytest.fixture
def client(engine, db: Session):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    portfolio = get_default_portfolio(db)

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_portfolio] = lambda: portfolio
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def fake_provider():
    provider = FakeProvider()
    saved = dict(cloud_base._REGISTRY)
    cloud_base._REGISTRY.clear()
    cloud_base.register(provider)
    yield provider
    cloud_base._REGISTRY.clear()
    cloud_base._REGISTRY.update(saved)


def test_status_lists_the_real_providers(client: TestClient):
    body = client.get("/api/cloud-backup/status").json()
    names = {p["name"] for p in body["providers"]}
    assert names == {"gdrive", "dropbox"}
    for provider in body["providers"]:
        assert set(provider) == {"name", "label", "configured", "connected", "last"}
        assert provider["last"] is None
    assert body["encryption"] == {"passphrase_set": False}
    assert body["job"]["active"] is False
    assert body["backup_time"]


def test_send_requires_a_connected_provider(client: TestClient, fake_provider):
    fake_provider.connected_flag = False
    response = client.post("/api/cloud-backup/send")
    assert response.status_code == 422
    assert "nenhum provedor" in response.json()["detail"]


def test_send_runs_as_a_job_and_conflicts_while_active(client: TestClient, fake_provider, monkeypatch):
    from app.services.cloud_backup import service as cloud_service

    started, release = threading.Event(), threading.Event()

    def slow_sync() -> dict:
        started.set()
        release.wait(5)
        return {"status": "ok", "providers": {"fake": {"state": "ok"}}}

    monkeypatch.setattr(cloud_service, "sync_to_cloud", slow_sync)

    first = client.post("/api/cloud-backup/send")
    assert first.status_code == 200 and first.json()["active"] is True
    assert started.wait(5)
    assert client.post("/api/cloud-backup/send").status_code == 409
    release.set()

    job = None
    for _ in range(100):
        job = client.get("/api/cloud-backup/status").json()["job"]
        if not job["active"]:
            break
        time.sleep(0.05)
    assert job and job["active"] is False and job["error"] is None
    assert job["result"]["status"] == "ok"


def test_remote_backups_lists_files(client: TestClient, db: Session, fake_provider):
    fake_provider.files["gumbinvest-20260101-000000.gumbinvest"] = b"x"
    body = client.get("/api/cloud-backup/backups").json()
    items = body["providers"]["fake"]["items"]
    assert items[0]["name"] == "gumbinvest-20260101-000000.gumbinvest"
    assert items[0]["encrypted"] is False


def test_restore_via_api(client: TestClient, db: Session, fake_provider):
    payload = export_snapshot(db)
    fake_provider.files["gumbinvest-x.gumbinvest.enc"] = encrypt(payload, "senha")

    wrong = client.post(
        "/api/cloud-backup/restore",
        json={
            "provider": "fake",
            "backup_id": "gumbinvest-x.gumbinvest.enc",
            "name": "gumbinvest-x.gumbinvest.enc",
            "passphrase": "errada",
        },
    )
    assert wrong.status_code == 422
    assert "senha incorreta" in wrong.json()["detail"]

    ok = client.post(
        "/api/cloud-backup/restore",
        json={
            "provider": "fake",
            "backup_id": "gumbinvest-x.gumbinvest.enc",
            "name": "gumbinvest-x.gumbinvest.enc",
            "passphrase": "senha",
        },
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "COMPLETED"


def test_disconnect_endpoint(client: TestClient, fake_provider):
    response = client.post("/api/cloud-backup/fake/disconnect")
    assert response.status_code == 200
    assert fake_provider.connected_flag is False
    assert client.post("/api/cloud-backup/nope/disconnect").status_code == 422


def test_encrypted_file_on_the_import_page_points_at_settings(client: TestClient):
    sealed = encrypt(b"qualquer coisa", "senha")
    response = client.post(
        "/api/imports",
        files={"file": ("backup.gumbinvest.enc", sealed, "application/octet-stream")},
    )
    assert response.status_code == 422
    assert "aba Backup" in response.json()["detail"]


def test_device_start_without_credentials_is_a_422(client: TestClient, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "gdrive_client_id", "")
    monkeypatch.setattr(settings, "gdrive_client_secret", "")
    response = client.post("/api/cloud-backup/gdrive/device/start")
    assert response.status_code == 422
    assert "client ID" in response.json()["detail"]
