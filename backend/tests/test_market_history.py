"""Price-history backfill.

The behaviour worth pinning is the ``only_missing`` run that follows an import:
a newly imported ticker has to fill its own history in, and re-downloading the
whole archive every night to achieve that would be absurd.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db.models import Asset, PriceHistory
from app.market import service as market_service
from app.market.base import HistoricalPoint, MarketDataProvider


class StubProvider(MarketDataProvider):
    """Records what it was asked for; serves ten closes for anything."""

    name = "stub"

    def __init__(self) -> None:
        self.asked: list[str] = []

    def get_quotes(self, symbols: list[str]) -> dict:
        return {}

    def supports_history(self) -> bool:
        return True

    def get_history(self, symbol: str, start: date | None = None) -> list[HistoricalPoint]:
        self.asked.append(symbol)
        first = date(2024, 1, 1)
        return [HistoricalPoint(day=first + timedelta(days=i), close=Decimal("10")) for i in range(10)]


@pytest.fixture
def provider(monkeypatch) -> StubProvider:
    stub = StubProvider()
    monkeypatch.setattr(market_service, "get_provider", lambda: stub)
    return stub


def _asset(db: Session, ticker: str, currency: str = "BRL") -> Asset:
    asset = Asset(ticker=ticker, name=ticker, kind="STOCK", currency=currency)
    db.add(asset)
    db.commit()
    return asset


def test_backfill_covers_foreign_assets_without_the_sa_suffix(db: Session, provider: StubProvider):
    """``VOOG.SA`` is not a thing; the currency decides, not the ticker shape."""
    _asset(db, "PETR4")
    _asset(db, "VOOG", currency="USD")

    market_service.backfill_history(db)

    assert sorted(provider.asked) == ["PETR4.SA", "VOOG"]
    assert db.query(PriceHistory).count() == 20


def test_only_missing_skips_assets_that_already_have_a_history(db: Session, provider: StubProvider):
    covered = _asset(db, "PETR4")
    _asset(db, "VOOG", currency="USD")
    for i in range(5):
        db.add(
            PriceHistory(
                asset_id=covered.id, date=date(2024, 1, 1) + timedelta(days=i), close=Decimal("30"), source="seed"
            )
        )
    db.commit()

    result = market_service.backfill_history(db, only_missing=True)

    assert provider.asked == ["VOOG"]
    assert result["assets"] == 1


def test_only_missing_treats_a_single_close_as_no_history(db: Session, provider: StubProvider):
    """The exact state a new asset is left in.

    ``refresh_quotes`` stores today's close for everything it prices, so a
    ticker imported yesterday already has one row. Reading that as "covered" is
    what left every international holding with a one-point chart.
    """
    asset = _asset(db, "VOOG", currency="USD")
    db.add(PriceHistory(asset_id=asset.id, date=date.today(), close=Decimal("85"), source="quote"))
    db.commit()

    market_service.backfill_history(db, only_missing=True)

    assert provider.asked == ["VOOG"]
    assert db.query(PriceHistory).filter_by(asset_id=asset.id).count() == 11


def test_only_missing_is_a_no_op_when_nothing_is_new(db: Session, provider: StubProvider):
    asset = _asset(db, "PETR4")
    for i in range(5):
        db.add(
            PriceHistory(
                asset_id=asset.id, date=date(2024, 1, 1) + timedelta(days=i), close=Decimal("30"), source="seed"
            )
        )
    db.commit()

    result = market_service.backfill_history(db, only_missing=True)

    assert provider.asked == []
    assert result["assets"] == 0
    assert result["detail"] == "history is complete"
