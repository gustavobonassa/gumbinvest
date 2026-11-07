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

#: Keys that only ever come from the UI: no settings field, so no environment
#: variable can set them. The desktop app is the target, and its users type
#: credentials into the Automações card; there is no ``.env`` to edit. They are
#: read from ``app_settings`` on each use (:func:`read_secret`), which also
#: means the Celery worker sees a key the moment it is saved.
UI_ONLY_KEYS = (
    "binance_api_key",
    "binance_api_secret",
)

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
    # Broker credentials for the automated collectors (app/pipelines). The CPF
    # is not exactly a secret, but write-only treatment is the right shape for
    # anything typed into a credential slot.
    "b3_cpf",
    "b3_password",
    *UI_ONLY_KEYS,
)

#: What the environment provided, captured before any UI override mutates the
#: singleton — clearing a stored key falls back to this, not to empty.
_ENV_VALUES = {key: getattr(settings, key, "") for key in SECRET_KEYS if key not in UI_ONLY_KEYS}


def _stored(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    value = (row.value or {}).get("value") if row is not None else None
    return str(value) if value else ""


def read_secret(key: str, db: Session | None = None) -> str:
    """A key's effective value: the saved one, else (for env-backed keys) the env.

    Opens its own short session when none is given, so a pipeline thread can
    call it between steps without holding a transaction.
    """
    if db is None:
        from app.db.session import session_scope

        with session_scope() as own:
            return read_secret(key, own)
    return _stored(db, key) or (str(getattr(settings, key, "") or "") if key not in UI_ONLY_KEYS else "")


def apply_stored_secrets(db: Session) -> None:
    """Load UI-saved keys over the env values. Called once at startup."""
    for key in SECRET_KEYS:
        if key in UI_ONLY_KEYS:
            continue
        value = _stored(db, key)
        if value:
            setattr(settings, key, value)


def store_secret(db: Session, key: str, value: str) -> None:
    """Save (or, with an empty value, clear) one key. Caller commits."""
    if key not in SECRET_KEYS:
        raise ValueError(f"not a secret key: {key}")
    value = (value or "").strip()
    if value:
        db.merge(AppSetting(key=key, value={"value": value}))
        if key not in UI_ONLY_KEYS:
            setattr(settings, key, value)
    else:
        row = db.get(AppSetting, key)
        if row is not None:
            db.delete(row)
        if key not in UI_ONLY_KEYS:
            setattr(settings, key, _ENV_VALUES[key])


def secret_status(db: Session) -> dict[str, bool]:
    """Which keys are effectively configured (stored or env) — never values."""
    return {
        key: bool(_stored(db, key)) if key in UI_ONLY_KEYS else bool(getattr(settings, key))
        for key in SECRET_KEYS
    }
