"""Exchange rates from Banco Central do Brasil (PTAX).

The portfolio is kept in reais but part of it is bought in dollars, so every
offshore movement needs a rate — and not today's rate: the cost basis of a share
bought in 2021 is the dollars paid *times the rate that day*. That is also what
Brazilian tax reporting expects, so PTAX (the Banco Central reference rate) is
the right source rather than a live market quote.

Series 1 of the SGS API is "Dólar comercial (venda)", published on business
days. Weekends, holidays and any day before the series starts fall back to the
most recent published rate, which is the standard convention.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta

from app.core.dates import local_today
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import FxRate, Transaction
from app.db.upsert import dialect_insert
from app.market.indices import BASE_URL, _parse  # SGS plumbing is shared

logger = get_logger(__name__)

#: (base, quote) -> SGS series id. Series 1 is the dollar (venda), 21619 the
#: euro — the exchange history has a couple of euro-quoted trades, and a pair
#: with no series is simply left unconverted rather than guessed at.
SERIES: dict[tuple[str, str], int] = {
    ("USD", "BRL"): 1,
    ("EUR", "BRL"): 21619,
}
#: Far enough back to cover the oldest offshore statement (November 2020).
DEFAULT_START = date(2015, 1, 1)
#: SGS rejects ranges longer than ten years.
MAX_YEARS_PER_REQUEST = 9


def supported_pairs() -> list[tuple[str, str]]:
    return sorted(SERIES)


def missing_pairs(db: Session) -> list[tuple[str, str]]:
    """Supported pairs with no rows yet — what a cold start still owes.

    Per pair rather than "is the table empty": a bootstrap that fetched the
    dollar and then hit a timeout on the euro must not count as done, or the
    euro only ever arrives with the next day's scheduled sync.
    """
    present = set(
        db.execute(select(FxRate.base, FxRate.quote).distinct()).all()
    )
    return [pair for pair in supported_pairs() if pair not in present]


def fetch_series(base: str, quote: str, start: date, end: date | None = None):
    """Download a PTAX series. Returns ``[(day, rate), ...]``."""
    import httpx

    from app.core.config import settings

    series_id = SERIES.get((base.upper(), quote.upper()))
    if series_id is None:
        raise ValueError(f"no PTAX series for {base}/{quote}")
    end = end or local_today()

    points: list[tuple[date, Decimal]] = []
    window_start = start
    while window_start <= end:
        try:
            window_end = min(
                window_start.replace(year=window_start.year + MAX_YEARS_PER_REQUEST), end
            )
        except ValueError:  # 29 February
            window_end = min(
                window_start.replace(year=window_start.year + MAX_YEARS_PER_REQUEST, day=28), end
            )
        params = {
            "formato": "json",
            "dataInicial": window_start.strftime("%d/%m/%Y"),
            "dataFinal": window_end.strftime("%d/%m/%Y"),
        }
        try:
            response = httpx.get(
                BASE_URL.format(series=series_id),
                params=params,
                timeout=settings.request_timeout * 2,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 — a failed sync must not break pricing
            logger.warning("PTAX request failed for %s/%s: %s", base, quote, exc)
            break

        for item in payload:
            value = _parse(item.get("valor"))
            raw_day = item.get("data")
            if value is None or not raw_day:
                continue
            try:
                day, month, year = (int(part) for part in raw_day.split("/"))
            except ValueError:
                continue
            points.append((date(year, month, day), value))

        if window_end >= end:
            break
        window_start = window_end + timedelta(days=1)
    return points


def sync_fx(db: Session, base: str = "USD", quote: str = "BRL", start: date | None = None) -> dict:
    """Fetch and store a rate series, resuming from the last stored day."""
    base, quote = base.upper(), quote.upper()
    if start is None:
        last = db.scalar(
            select(func.max(FxRate.date)).where(FxRate.base == base, FxRate.quote == quote)
        )
        # PTAX revises the last few days; re-fetch them.
        start = (last - timedelta(days=7)) if last else _earliest_needed(db)

    points = fetch_series(base, quote, start)
    for day, rate in points:
        db.execute(
            dialect_insert(db)(FxRate)
            .values(base=base, quote=quote, date=day, rate=rate, source="bcb-ptax")
            .on_conflict_do_update(
                index_elements=[FxRate.base, FxRate.quote, FxRate.date], set_={"rate": rate}
            )
        )
    db.commit()
    logger.info("fx %s/%s synced: %s points from %s", base, quote, len(points), start)
    return {"pair": f"{base}/{quote}", "points": len(points), "start": start.isoformat()}


def sync_all_fx(db: Session) -> dict:
    """Refresh every configured pair.

    One pair failing (a series retired, an outage) must not stop the others, so
    each is attempted independently — ``sync_fx`` already swallows a failed
    download and stores whatever it managed to read.
    """
    results = []
    for base, quote in supported_pairs():
        try:
            results.append(sync_fx(db, base, quote))
        except Exception:  # noqa: BLE001 — one dead series must not break the rest
            logger.exception("fx sync failed for %s/%s", base, quote)
    return {"pairs": results, "points": sum(r["points"] for r in results)}


def _earliest_needed(db: Session) -> date:
    """Start the series a little before the oldest foreign movement."""
    first = db.scalar(
        select(func.min(Transaction.trade_date)).where(Transaction.currency != "BRL")
    )
    if first is None:
        return DEFAULT_START
    return min(first - timedelta(days=30), local_today())


class FxTable:
    """In-memory rate lookup with carry-forward for non-business days.

    Loaded once per request and reused across thousands of movements, so the
    lookup is a binary search over a sorted list rather than a query per row.
    """

    def __init__(self, days: list[date], rates: list[Decimal]) -> None:
        self._days = days
        self._rates = rates

    @property
    def is_empty(self) -> bool:
        return not self._days

    def rate_on(self, day: date) -> Decimal | None:
        """The rate published on ``day``, or the most recent one before it."""
        if not self._days:
            return None
        index = bisect_right(self._days, day)
        if index == 0:
            # Older than the series: the first known rate is the best estimate.
            return self._rates[0]
        return self._rates[index - 1]

    @property
    def latest(self) -> Decimal | None:
        return self._rates[-1] if self._rates else None

    @property
    def latest_date(self) -> date | None:
        return self._days[-1] if self._days else None


def load_table(db: Session, base: str = "USD", quote: str = "BRL") -> FxTable:
    rows = db.execute(
        select(FxRate.date, FxRate.rate)
        .where(FxRate.base == base.upper(), FxRate.quote == quote.upper())
        .order_by(FxRate.date)
    ).all()
    return FxTable([row[0] for row in rows], [row[1] for row in rows])


def rate_on(db: Session, day: date, base: str = "USD", quote: str = "BRL") -> Decimal | None:
    """One-off lookup (the importer's per-row path)."""
    return load_table(db, base, quote).rate_on(day)


def backfill_transaction_fx(db: Session) -> dict:
    """Stamp foreign movements that were imported before rates were available.

    The rate is captured at import time, but a statement imported while the
    PTAX series was empty (first run, or an outage) would store ``None`` and
    then be silently left out of every base-currency total. This fills those in
    and is a no-op once they all have one.
    """
    rows = db.scalars(
        select(Transaction).where(Transaction.fx_rate.is_(None), Transaction.currency != "BRL")
    ).all()
    if not rows:
        return {"updated": 0}

    tables: dict[str, FxTable] = {}
    updated = 0
    for transaction in rows:
        currency = (transaction.currency or "USD").upper()
        table = tables.get(currency)
        if table is None:
            table = tables[currency] = load_table(db, currency, "BRL")
        if table.is_empty:
            continue
        rate = table.rate_on(transaction.trade_date)
        if rate is None:
            continue
        transaction.fx_rate = rate
        updated += 1

    if updated:
        db.commit()
        logger.info("filled in exchange rates for %s movements", updated)
    return {"updated": updated}


def fx_status(db: Session) -> list[dict]:
    """Coverage of each rate series, plus the rate currently in force.

    The current rate rides along because every caller that wants to know how
    fresh the series is also wants to show the number — the sidebar prints it,
    the settings page reports the coverage, and neither needs a second query.
    """
    from app.importer.crypto import symbols as coins  # local import avoids a cycle

    rows = db.execute(
        select(
            FxRate.base,
            FxRate.quote,
            func.min(FxRate.date),
            func.max(FxRate.date),
            func.count(),
        ).group_by(FxRate.base, FxRate.quote)
    ).all()

    status: list[dict] = []
    for base, quote, start, end, count in rows:
        latest = db.scalar(
            select(FxRate.rate).where(
                FxRate.base == base, FxRate.quote == quote, FxRate.date == end
            )
        )
        status.append(
            {
                "pair": f"{base}/{quote}",
                "base": base,
                "quote": quote,
                "start": start,
                "end": end,
                "points": count,
                "rate": latest,
                # This table holds two different things that happen to share a
                # shape. A PTAX series is an exchange rate the whole portfolio
                # depends on; a coin's daily close is stored the same way only
                # so that the handful of trades priced in Bitcoin can reach the
                # base currency (see app.market.crypto). Both are "how many
                # reais is one X", but only the first belongs anywhere a user
                # would look for the dollar.
                "is_currency": coins.is_fiat(base),
            }
        )
    return status
