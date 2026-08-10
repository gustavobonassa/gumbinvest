"""The owner paragraph the AI prompts get from the first-run wizard's answers."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import AppSetting
from app.services.user_profile import user_intro


def _store(db: Session, key: str, value: object) -> None:
    db.merge(AppSetting(key=key, value={"value": value}))
    db.commit()


def test_empty_when_nothing_saved(db: Session) -> None:
    assert user_intro(db) == ""


def test_blank_values_stay_silent(db: Session) -> None:
    _store(db, "user_name", "   ")
    _store(db, "investor_profile", {"objetivos": [], "horizonte": "", "risco": "", "notas": " "})
    assert user_intro(db) == ""


def test_name_alone(db: Session) -> None:
    _store(db, "user_name", " Gustavo ")
    intro = user_intro(db)
    assert intro.startswith("Sobre o dono da carteira:")
    assert "se chama Gustavo" in intro


def test_full_profile(db: Session) -> None:
    _store(db, "user_name", "Gustavo")
    _store(
        db,
        "investor_profile",
        {
            "objetivos": ["Renda passiva com dividendos", "Aposentadoria"],
            "horizonte": "Longo prazo (mais de 10 anos)",
            "risco": "Moderado",
            "notas": "Prefiro fundos imobiliários.",
        },
    )
    intro = user_intro(db)
    assert "Renda passiva com dividendos, Aposentadoria" in intro
    assert "Longo prazo (mais de 10 anos)" in intro
    assert "Perfil de risco declarado: Moderado." in intro
    assert "Prefiro fundos imobiliários." in intro


def test_malformed_profile_is_ignored(db: Session) -> None:
    # An older client (or a hand edit) may leave junk here; the prompt must
    # not inherit it.
    _store(db, "investor_profile", "não sou um dicionário")
    assert user_intro(db) == ""
