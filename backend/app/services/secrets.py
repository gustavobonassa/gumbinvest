"""User-supplied API keys: stored in ``app_settings``, never echoed back.

Environment variables remain the primary source (the Docker deployment). A
key saved through the UI overrides the env for this instance — which is what
lets a desktop user enable the AI chat or the brapi provider with their own
key, no ``.env`` involved. The override works by mutating the settings
singleton, so every consumer (``ai.py`` reads it per request, the brapi
provider at construction) picks it up without a restart.

The stored value never travels back to the frontend: the settings endpoint
reports only ``{key: configured?}`` booleans.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AppSetting

SECRET_KEYS = (
    "anthropic_api_key",
    "openai_api_key",
    "gemini_api_key",
    "grok_api_key",
    "groq_api_key",
    "brapi_token",
    # Cloud backup: tokens and the passphrase must never ride along in a
    # .gumbinvest export — restoring on another machine means reconnecting.
    "gdrive_client_secret",
    "gdrive_refresh_token",
    "dropbox_refresh_token",
    "cloud_backup_passphrase",
)

#: What the environment provided, captured before any UI override mutates the
#: singleton — clearing a stored key falls back to this, not to empty.
_ENV_VALUES = {key: getattr(settings, key) for key in SECRET_KEYS}


def apply_stored_secrets(db: Session) -> None:
    """Load UI-saved keys over the env values. Called once at startup."""
    for key in SECRET_KEYS:
        row = db.get(AppSetting, key)
        value = (row.value or {}).get("value") if row is not None else None
        if value:
            setattr(settings, key, str(value))


def store_secret(db: Session, key: str, value: str) -> None:
    """Save (or, with an empty value, clear) one key. Caller commits."""
    if key not in SECRET_KEYS:
        raise ValueError(f"not a secret key: {key}")
    value = (value or "").strip()
    if value:
        db.merge(AppSetting(key=key, value={"value": value}))
        setattr(settings, key, value)
    else:
        row = db.get(AppSetting, key)
        if row is not None:
            db.delete(row)
        setattr(settings, key, _ENV_VALUES[key])


def secret_status() -> dict[str, bool]:
    """Which keys are effectively configured (stored or env) — never values."""
    return {key: bool(getattr(settings, key)) for key in SECRET_KEYS}
