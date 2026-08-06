"""AI provider selection: stored choice wins, defaults fall back, keys gate."""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AppSetting
from app.services import secrets as secrets_service
from app.services.ai_providers import AI_PROVIDERS, active_ai, providers_public


@pytest.fixture(autouse=True)
def _restore_settings():
    yield
    for key, value in secrets_service._ENV_VALUES.items():
        setattr(settings, key, value)


def test_defaults_to_anthropic_and_its_model(db: Session) -> None:
    provider_id, provider, model, _ = active_ai(db)
    assert provider_id == "anthropic"
    assert provider["kind"] == "anthropic"
    assert model == AI_PROVIDERS["anthropic"]["default_model"]


def test_stored_choice_wins_and_unknown_falls_back(db: Session) -> None:
    db.merge(AppSetting(key="ai_provider", value={"value": "gemini"}))
    db.merge(AppSetting(key="ai_model", value={"value": "gemini-2.5-pro"}))
    db.commit()
    provider_id, provider, model, _ = active_ai(db)
    assert (provider_id, model) == ("gemini", "gemini-2.5-pro")
    assert provider["kind"] == "openai"
    assert "generativelanguage" in provider["base_url"]

    db.merge(AppSetting(key="ai_provider", value={"value": "skynet"}))
    db.merge(AppSetting(key="ai_model", value={"value": ""}))
    db.commit()
    provider_id, _, model, _ = active_ai(db)
    assert provider_id == "anthropic"
    assert model == AI_PROVIDERS["anthropic"]["default_model"]


def test_key_comes_from_the_selected_provider(db: Session) -> None:
    db.merge(AppSetting(key="ai_provider", value={"value": "groq"}))
    db.commit()
    secrets_service.store_secret(db, "groq_api_key", "gsk-test")
    db.commit()
    _, _, _, key = active_ai(db)
    assert key == "gsk-test"


def test_public_shape_never_carries_values(db: Session) -> None:
    secrets_service.store_secret(db, "openai_api_key", "sk-oai-test")
    db.commit()
    public = providers_public(db)
    assert {p["id"] for p in public["providers"]} == set(AI_PROVIDERS)
    openai = next(p for p in public["providers"] if p["id"] == "openai")
    assert openai["key_configured"] is True
    assert "sk-oai-test" not in str(public)
    # The catalogue says where to get a key and nothing about what it costs:
    # whose plan is free changes faster than a released build can.
    assert not any("gratuit" in str(p).lower() for p in public["providers"])
