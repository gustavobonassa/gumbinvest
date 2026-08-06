"""Market benchmarks a portfolio's return is measured against.

Two very different series answer the same question — "would I have done better
somewhere else?" — and they are stored and read the same way here so the chart
never has to know which is which:

* **IBOV** — the Ibovespa's daily close, downloaded from the quote provider.
  Its return over a window is the ratio between two closes.
* **CDI** — the risk-free rate, already collected by :mod:`app.market.indices`
  as a percentage *per business day*. Its return is the product of those daily
  factors, which is exactly how a CDI-linked paper accrues.

Both live in ``index_rates``: that table is keyed by code and its ``value``
column is already polymorphic (a per-business-day rate for CDI/Selic, a monthly
variation for IPCA). A daily index *level* is the third reading, and the code
that consumes it is right here, in one place. :func:`series` turns either kind
into a cumulative percentage rebased to the window's first day.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import IndexRate
from app.db.upsert import dialect_insert
from app.market.indices import forget_series

logger = get_logger(__name__)

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class Benchmark:
    code: str
    label: str
    #: ``level`` — the stored value is the index itself (Ibovespa points).
    #: ``rate`` — the stored value is a percentage earned on that day.
    kind: str
    #: Quote-provider symbol, for the ones that have to be downloaded here.
    symbol: str | None = None


BENCHMARKS: dict[str, Benchmark] = {
    "IBOV": Benchmark("IBOV", "Ibovespa", "level", "^BVSP"),
    "CDI": Benchmark("CDI", "CDI", "rate"),
}

#: Codes stored here that are index levels rather than rates. Anything reading
#: ``index_rates`` as "a rate" (fixed income accrual, the index coverage table)
#: must leave these out.
LEVEL_CODES = frozenset(code for code, item in BENCHMARKS.items() if item.kind == "level")

#: How far back to seed a level series on first sync. The portfolio history
#: starts in 2020; ten years of daily closes is one request and ~2 500 rows.
DEFAULT_START = date(2015, 1, 1)


def sync_benchmarks(db: Session, start: date | None = None) -> dict:
    """Download the level benchmarks (today: the Ibovespa) and store them."""
    from app.market.providers import get_provider  # local: avoids a cycle

    provider = get_provider()
    results: dict[str, int] = {}
    if not provider.supports_history():
        logger.info("benchmarks: provider %s has no history endpoint", provider.name)
        return {"points": 0, "series": results}

    for benchmark in BENCHMARKS.values():
        if benchmark.symbol is None:
            continue  # a rate series; app.market.indices owns it
        since = start
        if since is None:
            last = db.scalar(
                select(func.max(IndexRate.date)).where(IndexRate.code == benchmark.code)
            )
            # Re-fetch a few days: the last close can be revised or provisional.
            since = (last - timedelta(days=5)) if last else DEFAULT_START
        try:
            points = provider.get_history(benchmark.symbol, start=since)
        except Exception as exc:  # noqa: BLE001 — a benchmark must never break pricing
            logger.warning("benchmark %s failed: %s", benchmark.code, exc)
            continue
        for point in points:
            db.execute(
                dialect_insert(db)(IndexRate)
                .values(
                    code=benchmark.code,
                    date=point.day,
                    value=point.close,
                    source=f"{provider.name}:{benchmark.symbol}",
                )
                .on_conflict_do_update(
                    index_elements=[IndexRate.code, IndexRate.date],
                    set_={"value": point.close},
                )
            )
        results[benchmark.code] = len(points)
        logger.info("benchmark %s synced: %s points from %s", benchmark.code, len(points), since)
    db.commit()
    forget_series(db)
    return {"points": sum(results.values()), "series": results}


def _rows(db: Session, code: str, until: date) -> list[tuple[date, Decimal]]:
    return db.execute(
        select(IndexRate.date, IndexRate.value)
        .where(IndexRate.code == code, IndexRate.date <= until)
        .order_by(IndexRate.date)
    ).all()


def series(db: Session, code: str, days: list[date]) -> dict[date, Decimal]:
    """Cumulative return of ``code`` at each of ``days``, rebased to the first.

    The first day is always 0 %: the chart compares what the portfolio and the
    benchmark did *over the window on screen*, so both start from the same line.
    Returns an empty mapping when the series has nothing to say about the
    window — better a missing line than a flat one that reads as "no gain".
    """
    benchmark = BENCHMARKS.get(code.upper())
    if benchmark is None or not days:
        return {}
    rows = _rows(db, benchmark.code, days[-1])
    if not rows:
        return {}

    start = days[0]
    result: dict[date, Decimal] = {}

    if benchmark.kind == "level":
        # The base is the last close on or before the window opens. Without one
        # the window starts before the series does, and rebasing to its first
        # available close would draw a rally the portfolio never lived through.
        base = next((value for day, value in reversed(rows) if day <= start), None)
        if base is None or base <= ZERO:
            return {}
        index = 0
        last = base
        for day in days:
            while index < len(rows) and rows[index][0] <= day:
                last = rows[index][1]
                index += 1
            result[day] = (last / base - ONE) * HUNDRED
        return result

    # A rate series compounds: every business day in the window multiplies the
    # factor by (1 + rate). Days before the window are simply skipped.
    factor = ONE
    index = 0
    while index < len(rows) and rows[index][0] <= start:
        index += 1
    for day in days:
        while index < len(rows) and rows[index][0] <= day:
            factor *= ONE + rows[index][1] / HUNDRED
            index += 1
        result[day] = (factor - ONE) * HUNDRED
    return result


def missing(db: Session) -> list[str]:
    """Level benchmarks with nothing stored yet.

    Only the level series are considered: the rate ones are filled by
    :mod:`app.market.indices` for fixed income long before a chart asks for
    them, and counting them as "covered" is what would let an empty Ibovespa
    slip past a first-run check.
    """
    stored = {
        code
        for (code,) in db.execute(
            select(IndexRate.code).where(IndexRate.code.in_(sorted(LEVEL_CODES))).distinct()
        ).all()
    }
    return sorted(LEVEL_CODES - stored)


def latest_levels(db: Session) -> list[dict]:
    """Where each level benchmark closed, and how it moved that day.

    The move is the ratio between the last two stored closes, which is the
    session's change whenever the series is up to date — and, when it is not,
    the change of the last session actually stored. The date travels with the
    number so the reader can tell those apart.
    """
    levels: list[dict] = []
    for code in sorted(LEVEL_CODES):
        rows = db.execute(
            select(IndexRate.date, IndexRate.value)
            .where(IndexRate.code == code)
            .order_by(IndexRate.date.desc())
            .limit(2)
        ).all()
        if not rows:
            continue
        day, value = rows[0]
        previous = rows[1][1] if len(rows) > 1 else None
        change = (
            (value / previous - ONE) * HUNDRED if previous not in (None, ZERO) else None
        )
        levels.append(
            {
                "code": code,
                "label": BENCHMARKS[code].label,
                "value": value,
                "change_percent": change,
                "date": day,
            }
        )
    return levels


def coverage(db: Session) -> list[dict]:
    """What is stored for each benchmark — first day, last day, point count."""
    rows = db.execute(
        select(IndexRate.code, func.min(IndexRate.date), func.max(IndexRate.date), func.count())
        .where(IndexRate.code.in_(list(BENCHMARKS)))
        .group_by(IndexRate.code)
        .order_by(IndexRate.code)
    ).all()
    return [
        {
            "code": code,
            "label": BENCHMARKS[code].label,
            "start": start,
            "end": end,
            "points": count,
        }
        for code, start, end, count in rows
    ]
