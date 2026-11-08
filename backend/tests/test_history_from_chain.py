"""The dashboard's value chart, served from the stored daily chain.

Fast only when it is also right: the chart reads the chain while it matches
the data, walks the days itself (never rebuilding the chain inside a request)
when it does not, and ends on the same live figure as the header either way.
The background refresh is what keeps it on the fast path.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.dates import local_today
from app.db.models import (
    Asset,
    FixedIncomeTerms,
    FxRate,
    IndexRate,
    PortfolioSnapshot,
    PriceHistory,
    Quote,
    Transaction,
)
from app.market.service import _upsert_price
from app.portfolio.service import PortfolioService

START = date(2024, 1, 2)


def _asset(db, ticker="XPTO3", quote="20") -> Asset:
    asset = Asset(ticker=ticker, name=ticker, kind="STOCK", currency="BRL")
    db.add(asset)
    db.commit()
    db.add(Quote(asset_id=asset.id, price=Decimal(quote), currency="BRL"))
    db.commit()
    return asset


def _buy(db, portfolio, asset, day: date, quantity=10, unit_price="10", key="a") -> None:
    amount = Decimal(quantity) * Decimal(unit_price)
    db.add(
        Transaction(
            portfolio_id=portfolio.id,
            asset_id=asset.id,
            trade_date=day,
            direction="CREDIT",
            op_type="BUY",
            effect="ACQUIRE",
            quantity=Decimal(quantity),
            unit_price=Decimal(unit_price),
            gross_amount=amount,
            net_amount=amount,
            currency="BRL",
            raw_movement="Compra",
            raw_product=asset.ticker,
            dedup_key=f"chain-test:{key}",
            occurrence=0,
        )
    )
    db.commit()


@pytest.fixture
def holding(db, portfolio):
    """Ten shares bought at 10, closes of 10 and 12 on file, quoted live at 20."""
    asset = _asset(db)
    _buy(db, portfolio, asset, START)
    db.add_all(
        [
            PriceHistory(asset_id=asset.id, date=START, close=Decimal("10"), source="test"),
            PriceHistory(asset_id=asset.id, date=date(2024, 1, 31), close=Decimal("12"), source="test"),
        ]
    )
    db.commit()
    return asset


def _snapshot_count(db) -> int:
    return db.scalar(select(func.count(PortfolioSnapshot.id)))


def test_a_stale_chain_is_walked_around_not_rebuilt_in_the_request(db, portfolio, holding):
    points = PortfolioService(db, portfolio.id).history(granularity="day")

    assert points[0]["date"] == START
    assert next(p for p in points if p["date"] == date(2024, 1, 31))["market_value"] == Decimal(120)
    # The full chain costs several times this chart: a request never pays it.
    assert _snapshot_count(db) == 0


def test_a_current_chain_serves_the_chart_with_the_walks_figures(db, portfolio, holding, monkeypatch):
    walked = PortfolioService(db, portfolio.id).history(granularity="day")
    assert PortfolioService(db, portfolio.id).refresh_chain_if_stale()["rebuilt"] is True

    def no_walk(*_args, **_kwargs):
        raise AssertionError("served from the stored chain, the chart must not walk the days")

    monkeypatch.setattr(PortfolioService, "_walk_history", no_walk)
    served = PortfolioService(db, portfolio.id).history(granularity="day")

    assert [p["date"] for p in served] == [p["date"] for p in walked]
    for mine, theirs in zip(served, walked):
        for key in ("market_value", "cost_basis", "invested", "dividends", "realized", "profit"):
            assert abs(mine[key] - theirs[key]) < Decimal("0.000001"), (mine["date"], key)


@pytest.mark.parametrize("from_chain", [False, True])
def test_today_ends_on_the_headline_value(db, portfolio, holding, from_chain):
    if from_chain:
        PortfolioService(db, portfolio.id).refresh_chain_if_stale()
    service = PortfolioService(db, portfolio.id)

    last = service.history()[-1]

    assert last["date"] == local_today()
    # The live quote (20), not the last close on file (12).
    assert last["market_value"] == service.overview()["market_value"] == Decimal(200)
    assert last["profit"] == Decimal(100)


def test_the_background_refresh_rebuilds_only_when_something_changed(db, portfolio, holding):
    assert PortfolioService(db, portfolio.id).refresh_chain_if_stale() == {
        "rebuilt": True,
        "points": (local_today() - START).days + 1,
    }
    assert PortfolioService(db, portfolio.id).refresh_chain_if_stale() == {"rebuilt": False}

    _buy(db, portfolio, holding, date(2024, 2, 1), quantity=5, key="b")

    assert PortfolioService(db, portfolio.id).refresh_chain_if_stale()["rebuilt"] is True
    served = PortfolioService(db, portfolio.id).history(granularity="day")
    assert next(p for p in served if p["date"] == date(2024, 2, 1))["cost_basis"] == Decimal(150)


def _fingerprint(db, portfolio) -> str:
    return PortfolioService(db, portfolio.id).ledger_fingerprint()


def test_a_past_close_restated_in_place_invalidates_the_chain(db, portfolio, holding):
    before = _fingerprint(db, portfolio)
    _upsert_price(db, holding.id, date(2024, 1, 31), Decimal("15"), "test")
    db.commit()
    assert _fingerprint(db, portfolio) != before


def test_todays_close_moving_with_each_quote_refresh_does_not(db, portfolio, holding):
    _upsert_price(db, holding.id, local_today(), Decimal("19"), "test")
    db.commit()
    before = _fingerprint(db, portfolio)

    # What every half-hourly quote refresh does to today's row.
    _upsert_price(db, holding.id, local_today(), Decimal("21"), "test")
    db.commit()

    assert _fingerprint(db, portfolio) == before


def test_rates_and_fixed_income_terms_invalidate_the_chain(db, portfolio, holding):
    fingerprints = {_fingerprint(db, portfolio)}

    db.add(IndexRate(code="CDI", date=START, value=Decimal("0.04"), source="test"))
    db.commit()
    fingerprints.add(_fingerprint(db, portfolio))

    db.add(FxRate(base="USD", quote="BRL", date=START, rate=Decimal("4.9"), source="test"))
    db.commit()
    fingerprints.add(_fingerprint(db, portfolio))

    cdb = Asset(ticker="CDB-X", name="CDB X", kind="FIXED_INCOME", currency="BRL")
    db.add(cdb)
    db.commit()
    fingerprints.add(_fingerprint(db, portfolio))
    terms = FixedIncomeTerms(asset_id=cdb.id, index_code="CDI", percent_of_index=Decimal(100))
    db.add(terms)
    db.commit()
    fingerprints.add(_fingerprint(db, portfolio))

    # A rate edited in place: same row count, so only the timestamp tells.
    terms.percent_of_index = Decimal(110)
    terms.updated_at = terms.updated_at + timedelta(seconds=5)
    db.commit()
    fingerprints.add(_fingerprint(db, portfolio))

    assert len(fingerprints) == 6
