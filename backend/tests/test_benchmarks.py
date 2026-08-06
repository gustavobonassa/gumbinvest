"""Benchmark series: two storage shapes, one cumulative-percentage output."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db.models import IndexRate
from app.market.benchmarks import series


def store(db: Session, code: str, points: list[tuple[str, str]]) -> None:
    for day, value in points:
        db.add(IndexRate(code=code, date=date.fromisoformat(day), value=Decimal(value)))
    db.commit()


def test_level_series_is_rebased_to_the_first_day(db: Session):
    """An index level: the return is the ratio between two closes."""
    store(
        db,
        "IBOV",
        [
            ("2026-01-02", "120000"),
            ("2026-02-02", "126000"),  # +5 %
            ("2026-03-02", "114000"),  # −5 %
        ],
    )
    days = [date(2026, 1, 2), date(2026, 2, 2), date(2026, 3, 2)]
    result = series(db, "IBOV", days)

    assert result[days[0]] == pytest.approx(Decimal(0))
    assert result[days[1]] == pytest.approx(Decimal(5))
    assert result[days[2]] == pytest.approx(Decimal(-5))


def test_level_series_holds_the_last_close_over_a_gap(db: Session):
    """Asked about a day the market was shut, it answers with the last close."""
    store(db, "IBOV", [("2026-01-02", "100000"), ("2026-01-09", "110000")])
    # 10 January is a Saturday; nothing was published, so it is still +10 %.
    result = series(db, "IBOV", [date(2026, 1, 2), date(2026, 1, 10)])
    assert result[date(2026, 1, 10)] == pytest.approx(Decimal(10))


def test_level_series_is_empty_when_the_window_opens_first(db: Session):
    """No close on or before day one means no base — and so no line.

    Rebasing to the series' own first point instead would draw a rally that
    happened before the window, next to a portfolio that never lived it.
    """
    store(db, "IBOV", [("2026-05-04", "130000")])
    assert series(db, "IBOV", [date(2026, 1, 2), date(2026, 6, 1)]) == {}


def test_rate_series_compounds_the_days_inside_the_window(db: Session):
    """CDI is a rate per business day, so the window multiplies its factors."""
    store(
        db,
        "CDI",
        [
            ("2025-12-30", "1.0"),  # before the window: must not be counted
            ("2026-01-05", "1.0"),
            ("2026-01-06", "1.0"),
            ("2026-01-07", "2.0"),
        ],
    )
    days = [date(2026, 1, 2), date(2026, 1, 6), date(2026, 1, 7)]
    result = series(db, "CDI", days)

    assert result[days[0]] == pytest.approx(Decimal(0))
    # 1,01 × 1,01 − 1
    assert result[days[1]] == pytest.approx(Decimal("2.01"))
    # × 1,02 − 1
    assert result[days[2]] == pytest.approx(Decimal("4.0502"))


def test_unknown_or_empty_series_returns_nothing(db: Session):
    assert series(db, "NOPE", [date(2026, 1, 1)]) == {}
    assert series(db, "IBOV", [date(2026, 1, 1)]) == {}
    assert series(db, "CDI", []) == {}
