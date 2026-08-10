"""Who the owner is, for the AI prompts.

The first-run wizard (and the Settings page) stores the owner's name and
declared goals as plain AppSettings; every AI feature that talks *to* the user
folds them into its context through `user_intro`. One paragraph, empty when
nothing was filled in — the prompts stay unchanged for users who skipped it.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import AppSetting


def _setting(db: Session, key: str) -> object:
    row = db.get(AppSetting, key)
    if row is None:
        return None
    value = row.value
    return value.get("value") if isinstance(value, dict) else value


def user_intro(db: Session) -> str:
    """A short paragraph about the portfolio's owner, or "" when unknown."""
    parts: list[str] = []

    name = _setting(db, "user_name")
    if isinstance(name, str) and name.strip():
        parts.append(
            f"O dono da carteira se chama {name.strip()}; "
            "dirija-se a ele pelo nome quando soar natural."
        )

    profile = _setting(db, "investor_profile")
    if isinstance(profile, dict):
        goals = profile.get("objetivos")
        if isinstance(goals, list) and goals:
            parts.append("Objetivos declarados: " + ", ".join(str(goal) for goal in goals) + ".")
        horizon = profile.get("horizonte")
        if isinstance(horizon, str) and horizon.strip():
            parts.append(f"Horizonte de investimento: {horizon.strip()}.")
        risk = profile.get("risco")
        if isinstance(risk, str) and risk.strip():
            parts.append(f"Perfil de risco declarado: {risk.strip()}.")
        notes = profile.get("notas")
        if isinstance(notes, str) and notes.strip():
            parts.append(f"Nas palavras do próprio usuário: {notes.strip()}")

    if not parts:
        return ""
    return "Sobre o dono da carteira: " + " ".join(parts)
