"""The B3 automation profile must never offer to translate B3's pt-BR pages."""

from __future__ import annotations

import json

from app.pipelines.b3 import _silence_translate


def _prefs(profile):
    return json.loads((profile / "Default" / "Preferences").read_text(encoding="utf-8"))


def test_fresh_profile_gets_translate_disabled(tmp_path):
    _silence_translate(tmp_path)
    prefs = _prefs(tmp_path)
    assert prefs["translate"]["enabled"] is False
    assert "pt" in prefs["translate_blocked_languages"]


def test_existing_prefs_are_preserved(tmp_path):
    default = tmp_path / "Default"
    default.mkdir()
    (default / "Preferences").write_text(
        json.dumps(
            {
                "intl": {"selected_languages": "en-US,en"},
                "translate": {"some_flag": 1},
                "translate_blocked_languages": ["fr"],
            }
        ),
        encoding="utf-8",
    )
    _silence_translate(tmp_path)
    prefs = _prefs(tmp_path)
    assert prefs["intl"] == {"selected_languages": "en-US,en"}
    assert prefs["translate"] == {"some_flag": 1, "enabled": False}
    assert prefs["translate_blocked_languages"] == ["fr", "pt"]


def test_corrupt_prefs_are_left_alone(tmp_path):
    default = tmp_path / "Default"
    default.mkdir()
    (default / "Preferences").write_text("{not json", encoding="utf-8")
    _silence_translate(tmp_path)  # must not raise: the run goes on
    assert (default / "Preferences").read_text(encoding="utf-8") == "{not json"
