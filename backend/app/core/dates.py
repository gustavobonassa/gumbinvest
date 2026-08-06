"""Business-date helpers.

The app is B3-centric: "today" is today in the configured timezone
(America/Sao_Paulo by default), not the server's local date. In a UTC
container ``date.today()`` flips at 21:00 BRT, which would date snapshots
tomorrow and shift year-to-date windows three hours early.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.core.config import settings


def local_today() -> date:
    try:
        return datetime.now(ZoneInfo(settings.timezone)).date()
    except Exception:
        return date.today()
