"""User-supplied API keys: stored, applied, never echoed, never exported."""
from __future__ import annotations

import gzip
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import current_portfolio
from app.core.config import settings
from app.db.models import AppSetting
from app.db.session import get_db
from app.main import app
from app.services import secrets as secrets_service
from app.services.full_backup import export_snapshot
from app.services.portfolio_registry import get_default_portfolio


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


@pytest.fixture(autouse=True)
def _restore_settings():
    """Every test mutates the settings singleton; put the env values back."""
    yield
    for key, value in secrets_service._ENV_VALUES.items():
        setattr(settings, key, value)


def test_store_apply_and_clear(db: Session) -> None:
    secrets_service.store_secret(db, "anthropic_api_key", "sk-test-123")
    db.commit()
    assert settings.anthropic_api_key == "sk-test-123"
    assert secrets_service.secret_status()["anthropic_api_key"] is True

    # A fresh process learns the stored value at startup.
    settings.anthropic_api_key = secrets_service._ENV_VALUES["anthropic_api_key"]
    secrets_service.apply_stored_secrets(db)
    assert settings.anthropic_api_key == "sk-test-123"

    # Clearing removes the row and falls back to the env value.
    secrets_service.store_secret(db, "anthropic_api_key", "")
    db.commit()
    assert db.get(AppSetting, "anthropic_api_key") is None
    assert settings.anthropic_api_key == secrets_service._ENV_VALUES["anthropic_api_key"]


def test_non_secret_key_is_rejected(db: Session) -> None:
    with pytest.raises(ValueError):
        secrets_service.store_secret(db, "theme", "dark")


def test_settings_endpoint_never_echoes_the_key(client, db: Session) -> None:
    saved = client.put("/api/settings", json={"values": {"anthropic_api_key": "sk-test-456"}})
    assert saved.status_code == 200
    body = saved.json()
    assert body["secrets"]["anthropic_api_key"] is True
    assert "anthropic_api_key" not in body["values"]
    assert "sk-test-456" not in saved.text

    fetched = client.get("/api/settings")
    assert "sk-test-456" not in fetched.text


def test_full_export_never_carries_keys(db: Session) -> None:
    secrets_service.store_secret(db, "anthropic_api_key", "sk-test-789")
    db.merge(AppSetting(key="theme", value={"value": "dark"}))
    db.commit()

    document = json.loads(gzip.decompress(export_snapshot(db)))
    stored_keys = [row["key"] for row in document["tables"]["app_settings"]]
    assert "theme" in stored_keys
    assert "anthropic_api_key" not in stored_keys
    assert "sk-test-789" not in json.dumps(document)
