"""Market index series from Banco Central do Brasil (SGS API).

The SGS API is public, free and needs no key:

    https://api.bcb.gov.br/dados/serie/bcdata.sgs.{series}/dados?formato=json

Series used:

===== ============================================ ==========================
Code  SGS series                                   Meaning
===== ============================================ ==========================
CDI   12    Taxa DI (CDI)                          % per business day
SELIC 11    Taxa Selic                             % per business day
IPCA  433   IPCA                                   % per month
===== ============================================ ==========================
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.core.dates import local_today
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import IndexRate
from app.db.upsert import dialect_insert

logger = get_logger(__name__)

BASE_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{series}/dados"

#: Index code -> (SGS series id, frequency)
SERIES: dict[str, tuple[int, str]] = {
    "CDI": (12, "daily"),
    "SELIC": (11, "daily"),
    "IPCA": (433, "monthly"),
}

#: Earliest date worth fetching when a series is empty.
DEFAULT_START = date(2015, 1, 1)


def available_indices() -> list[str]:
    return sorted(SERIES)


def _parse(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return None


#: SGS rejects ranges longer than ten years with HTTP 406, so requests are
#: chunked. Nine keeps a safety margin around leap days.
MAX_YEARS_PER_REQUEST = 9


def _fetch_window(series_id: int, start: date, end: date) -> list[dict]:
    params = {
        "formato": "json",
        "dataInicial": start.strftime("%d/%m/%Y"),
        "dataFinal": end.strftime("%d/%m/%Y"),
    }
    response = httpx.get(
        BASE_URL.format(series=series_id), params=params, timeout=settings.request_timeout * 2
    )
    response.raise_for_status()
    return response.json()


def fetch_series(code: str, start: date, end: date | None = None) -> list[tuple[date, Decimal]]:
    """Download one index series from SGS. Returns [(day, percent), ...]."""
    entry = SERIES.get(code.upper())
    if entry is None:
        raise ValueError(f"unknown index {code!r}; known: {', '.join(available_indices())}")
    series_id, _ = entry
    end = end or local_today()

    points: list[tuple[date, Decimal]] = []
    window_start = start
    while window_start <= end:
        try:
            window_end = min(window_start.replace(year=window_start.year + MAX_YEARS_PER_REQUEST), end)
        except ValueError:  # 29 February
            window_end = min(
                window_start.replace(year=window_start.year + MAX_YEARS_PER_REQUEST, day=28), end
            )
        try:
            payload = _fetch_window(series_id, window_start, window_end)
        except Exception as exc:  # noqa: BLE001 — a failed sync must not break pricing
            logger.warning("BCB SGS request failed for %s (%s..%s): %s", code, window_start, window_end, exc)
            break

        for item in payload:
            value = _parse(item.get("valor"))
            raw_day = item.get("data")
            if value is None or not raw_day:
                continue
            try:
                day = datetime.strptime(raw_day, "%d/%m/%Y").date()
            except ValueError:
                continue
            points.append((day, value))

        if window_end >= end:
            break
        window_start = window_end + timedelta(days=1)
    return points


def sync_index(db: Session, code: str, start: date | None = None) -> dict:
    """Fetch and store a series, resuming from the last stored date."""
    code = code.upper()
    if start is None:
        last = db.scalar(select(func.max(IndexRate.date)).where(IndexRate.code == code))
        # Re-fetch the last few days: SGS revises recent values.
        start = (last - timedelta(days=7)) if last else DEFAULT_START

    points = fetch_series(code, start)
    for day, value in points:
        db.execute(
            dialect_insert(db)(IndexRate)
            .values(code=code, date=day, value=value, source="bcb-sgs")
            .on_conflict_do_update(
                index_elements=[IndexRate.code, IndexRate.date], set_={"value": value}
            )
        )
    db.commit()
    forget_series(db)
    logger.info("index %s synced: %s points from %s", code, len(points), start)
    return {"index": code, "points": len(points), "start": start.isoformat()}


def sync_all_indices(db: Session) -> dict:
    return {code: sync_index(db, code) for code in available_indices()}


def forget_series(db: Session) -> None:
    """Drop the cached series after new rates are stored in this session."""
    db.info.pop("index_series", None)
    db.info.pop("fi_factor_tables", None)


def load_series(db: Session, code: str) -> list[tuple[date, Decimal]]:
    """All stored values for an index, ordered by date.

    Cached on the session: valuing fixed income *through history* asks for the
    same CDI series once per sampled day per paper, and the series is thousands
    of rows. The cache lives and dies with the request's session, so a sync
    that writes new rates is never read against a stale copy.
    """
    key = code.upper()
    cache = db.info.setdefault("index_series", {})
    if key not in cache:
        rows = db.execute(
            select(IndexRate.date, IndexRate.value)
            .where(IndexRate.code == key)
            .order_by(IndexRate.date)
        ).all()
        cache[key] = [(day, value) for day, value in rows]
    return cache[key]


def index_status(db: Session) -> list[dict]:
    """Coverage of the *rate* series — the ones fixed income accrues against.

    Restricted to :data:`SERIES` on purpose: ``index_rates`` also holds daily
    index **levels** for the chart benchmarks (see
    :mod:`app.market.benchmarks`), and listing the Ibovespa among the indices a
    CDB can be pegged to would be nonsense.
    """
    rows = db.execute(
        select(IndexRate.code, func.min(IndexRate.date), func.max(IndexRate.date), func.count())
        .where(IndexRate.code.in_(list(SERIES)))
        .group_by(IndexRate.code)
        .order_by(IndexRate.code)
    ).all()
    return [
        {"code": code, "start": start, "end": end, "points": count, "checked_at": datetime.now(UTC)}
        for code, start, end, count in rows
    ]
