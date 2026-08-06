"""SQLite stores ``Numeric`` through float — pin down exactly what survives.

The desktop build keeps money in SQLite, whose only numeric storage is a
64-bit float (15 significant digits round-trip exactly). The invariant
"money is Decimal" holds in Python either way; these tests pin the storage
boundary so it is a documented fact rather than a surprise:

* every value the app realistically stores round-trips exactly — quotes,
  amounts and quantities of a personal portfolio are ≤ 14 significant digits;
* past 15 significant digits SQLite quantizes — asserted here on purpose, so
  if that boundary ever starts to matter the failure points straight at it.

A TEXT-storing TypeDecorator was considered and rejected: SQLite compares
TEXT columns against numbers by *type order* (TEXT > any number), which would
silently break every ``WHERE amount > 0`` and ``func.sum`` in the codebase.
PostgreSQL (Docker) is exact at any width and unaffected by all of this.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.db.models import Asset, PortfolioSnapshot, Quote
from tests.conftest import IS_POSTGRES

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="exercises SQLite float storage")


def _store_price(db, price: Decimal) -> Decimal:
    asset = Asset(ticker="TST3", name="Teste", kind="stock")
    db.add(asset)
    db.flush()
    db.add(Quote(asset_id=asset.id, price=price, source="test"))
    db.commit()
    db.expire_all()
    return db.query(Quote).one().price


@pytest.mark.parametrize(
    "price",
    [
        Decimal("0.000001"),            # smallest MONEY step
        Decimal("0.07"),                # the classic float trap
        Decimal("31.415926"),           # ordinary quote
        Decimal("98765432.109876"),     # 14 sig digits: a large portfolio value
        Decimal("123456789.012345"),    # 15 sig digits: the float64 limit
    ],
)
def test_realistic_money_round_trips_exactly(db, portfolio, price: Decimal) -> None:
    stored = _store_price(db, price)
    assert isinstance(stored, Decimal)
    assert stored == price, f"MONEY drifted in SQLite storage: {price} -> {stored}"


def test_beyond_float64_quantizes_and_this_is_known(db, portfolio) -> None:
    """18 significant digits (full MONEY width) does NOT survive SQLite.

    Kept as a positive assertion: if SQLAlchemy or SQLite ever change this
    behavior, or if such values start appearing in real data, this failure is
    the signal to revisit storage (likely a custom type) rather than a flake.
    """
    price = Decimal("123456789012.345678")
    stored = _store_price(db, price)
    assert stored != price
    assert abs(stored - price) < Decimal("0.001")  # micro-drift, not corruption


def test_return_factor_round_trips_at_engine_precision(db, portfolio) -> None:
    """Numeric(28,16) return factors: 15 significant digits round-trip."""
    factor = Decimal("1.00123456789012")
    snapshot = PortfolioSnapshot(
        portfolio_id=portfolio.id, date=date(2026, 1, 2), return_factor=factor
    )
    db.add(snapshot)
    db.commit()
    db.expire_all()

    stored = db.query(PortfolioSnapshot).one().return_factor
    assert isinstance(stored, Decimal)
    assert stored == factor
