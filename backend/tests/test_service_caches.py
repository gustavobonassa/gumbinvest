"""Cross-request caches in PortfolioService: shared while nothing changed,
re-read the moment anything they were built from does.

Each test plays two "requests" (two service instances) without clearing the
cache in between — the conftest clears it per test, and production never does,
so the keys alone have to notice every change. A fast stale chart is the
failure mode these exist to rule out.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.db.models import Asset, PriceHistory, Quote, Transaction
from app.market.service import _upsert_price
from app.portfolio.service import PortfolioService


def _asset(db, ticker="XPTO3", price="10") -> Asset:
    asset = Asset(ticker=ticker, name=ticker, kind="STOCK", currency="BRL")
    db.add(asset)
    db.commit()
    db.add(Quote(asset_id=asset.id, price=Decimal(price), currency="BRL"))
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
            dedup_key=f"cache-test:{key}",
            occurrence=0,
        )
    )
    db.commit()


def _closes(db, asset, closes: dict[date, str]) -> None:
    db.add_all(
        PriceHistory(asset_id=asset.id, date=day, close=Decimal(value), source="test")
        for day, value in closes.items()
    )
    db.commit()


def _value_on(points: list[dict], day: date) -> Decimal:
    return next(point["market_value"] for point in points if point["date"] == day)


def test_price_series_is_shared_until_a_close_is_corrected_in_place(db, portfolio):
    asset = _asset(db)
    _buy(db, portfolio, asset, date(2024, 1, 2))
    _closes(db, asset, {date(2024, 1, 2): "10", date(2024, 1, 31): "12"})

    first = PortfolioService(db, portfolio.id)
    assert _value_on(first.history(granularity="day"), date(2024, 1, 31)) == Decimal(120)
    second = PortfolioService(db, portfolio.id)
    second.history(granularity="day")
    # Nothing changed: the second request reused the first one's series.
    assert second._raw_price_matrix()[asset.id] is first._raw_price_matrix()[asset.id]

    # The provider restates a close: same row, same date, same count.
    _upsert_price(db, asset.id, date(2024, 1, 31), Decimal("15"), "test")
    db.commit()

    third = PortfolioService(db, portfolio.id)
    assert _value_on(third.history(granularity="day"), date(2024, 1, 31)) == Decimal(150)


def test_timeline_is_shared_until_the_ledger_changes(db, portfolio):
    asset = _asset(db)
    _buy(db, portfolio, asset, date(2024, 1, 2))

    first = PortfolioService(db, portfolio.id).timeline()
    assert PortfolioService(db, portfolio.id).timeline() is first

    _buy(db, portfolio, asset, date(2024, 2, 1), quantity=5, key="b")

    after = PortfolioService(db, portfolio.id).timeline()
    assert after is not first
    assert after[-1].day == date(2024, 2, 1)
    assert after[-1].quantities[asset.id] == Decimal(15)


def test_stored_chain_is_reread_when_it_is_rematerialised(db, portfolio):
    """Same ledger fingerprint, different rows: a rebuild after a quote moved.

    The fingerprint covers the ledger, not today's quote, so a re-materialised
    chain can carry new figures under the same fingerprint — the decoded copy
    must not outlive the rows it was decoded from.
    """
    asset = _asset(db, price="10")
    _buy(db, portfolio, asset, date(2024, 1, 2))
    _closes(db, asset, {date(2024, 1, 2): "10"})

    PortfolioService(db, portfolio.id).chain()  # materialises
    cached = PortfolioService(db, portfolio.id).chain()
    assert PortfolioService(db, portfolio.id).chain() is cached
    assert cached[-1]["market_value"] == Decimal(100)

    quote = db.scalar(select(Quote).where(Quote.asset_id == asset.id))
    quote.price = Decimal(20)
    db.commit()
    PortfolioService(db, portfolio.id).rebuild_snapshots()

    fresh = PortfolioService(db, portfolio.id).chain()
    assert fresh is not cached
    assert fresh[-1]["market_value"] == Decimal(200)


def test_stored_chain_notices_a_rewrite_by_another_process(db, portfolio):
    """The Celery worker rebuilds snapshots in its own process, so the web
    process's copy is never dropped by hand — the key itself must move."""
    from sqlalchemy import update

    from app.db.models import PortfolioSnapshot

    asset = _asset(db, price="10")
    _buy(db, portfolio, asset, date(2024, 1, 2))
    _closes(db, asset, {date(2024, 1, 2): "10"})
    PortfolioService(db, portfolio.id).chain()
    cached = PortfolioService(db, portfolio.id).chain()

    # What another process's rebuild leaves behind: same rows, same ids
    # (SQLite reuses them), different figures.
    last = max(row["date"] for row in cached)
    db.execute(
        update(PortfolioSnapshot)
        .where(PortfolioSnapshot.portfolio_id == portfolio.id, PortfolioSnapshot.date == last)
        .values(market_value=Decimal(321))
    )
    db.commit()

    fresh = PortfolioService(db, portfolio.id).chain()
    assert fresh[-1]["market_value"] == Decimal(321)
