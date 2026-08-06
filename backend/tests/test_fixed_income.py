"""Fixed income accrual: CDI compounding, spreads, prefixed papers, valuation."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.db.models import Asset, FixedIncomeTerms, IndexRate, Transaction
from app.domain.enums import AssetKind, OperationType, PositionEffect
from app.market.fixed_income import (
    accrual_factor,
    accrued_prices,
    implied_percent_of_index,
    value_position,
)

D = Decimal


@pytest.fixture
def cdi(db):
    """A synthetic CDI series: 250 business days at exactly 0,04 % a.d.

    Returns ``(first_day, last_day)`` so tests can assert exact day counts.
    """
    start = date(2025, 1, 6)  # a Monday
    day = start
    added = 0
    last = start
    while added < 250:
        if day.weekday() < 5:
            db.add(IndexRate(code="CDI", date=day, value=D("0.04")))
            last = day
            added += 1
        day += timedelta(days=1)
    db.commit()
    return start, last


@pytest.fixture
def cdb(db, portfolio):
    asset = Asset(ticker="CDBTEST01", name="CDB Teste", kind=AssetKind.FIXED_INCOME.value)
    db.add(asset)
    db.flush()
    db.add(
        Transaction(
            portfolio_id=portfolio.id,
            asset_id=asset.id,
            trade_date=date(2025, 1, 6),
            direction="CREDIT",
            op_type=OperationType.BUY.value,
            effect=PositionEffect.ACQUIRE.value,
            quantity=D("10000"),
            unit_price=D("1"),
            gross_amount=D("10000"),
            net_amount=D("-10000"),
            raw_movement="COMPRA / VENDA",
            raw_product="CDB - CDBTEST01 - BANCO TESTE",
            dedup_key="test:0",
        )
    )
    db.commit()
    return asset


def terms(**kwargs) -> FixedIncomeTerms:
    base = {
        "asset_id": 1,
        "index_code": "CDI",
        "percent_of_index": D(100),
        "spread_annual": D(0),
        "fixed_rate_annual": D(0),
        "maturity_date": None,
    }
    base.update(kwargs)
    return FixedIncomeTerms(**base)


def test_cdi_compounds_daily(db, cdi):
    """100 % of CDI over 250 days at 0,04 % a.d. = 1,0004 ** 250."""
    start, last = cdi
    factor, days, stale = accrual_factor(db, terms(), start - timedelta(days=1), last)
    assert days == 250
    assert factor == pytest.approx(D("1.0004") ** 250, abs=D("0.000001"))
    assert not stale


def test_percentage_of_cdi_scales_the_daily_rate(db, cdi):
    """The CETIP convention scales each daily factor, not the final result."""
    start, last = cdi
    full, _, _ = accrual_factor(db, terms(), start - timedelta(days=1), last)
    half, _, _ = accrual_factor(db, terms(percent_of_index=D(50)), start - timedelta(days=1), last)
    assert half == pytest.approx(D("1.0002") ** 250, abs=D("0.000001"))
    assert half < full
    # Compounding is superlinear in the rate, so halving the daily rate yields
    # slightly *less* than half the interest — not exactly half.
    assert (half - 1) < (full - 1) / 2
    assert (half - 1) > (full - 1) / 2 * D("0.95")


def test_higher_percentage_yields_more(db, cdi):
    start, last = cdi
    low, _, _ = accrual_factor(db, terms(percent_of_index=D(100)), start, last)
    high, _, _ = accrual_factor(db, terms(percent_of_index=D("119.6")), start, last)
    assert high > low


def test_spread_compounds_over_business_days(db, cdi):
    start, last = cdi
    plain, days, _ = accrual_factor(db, terms(), start, last)
    with_spread, _, _ = accrual_factor(db, terms(spread_annual=D(2)), start, last)
    expected = plain * Decimal(str(1.02 ** (days / 252)))
    assert with_spread == pytest.approx(expected, abs=D("0.0001"))


def test_prefixed_paper_ignores_the_index(db, cdi):
    start, last = cdi
    factor, days, _ = accrual_factor(
        db, terms(index_code="PRE", fixed_rate_annual=D(12)), start, last
    )
    assert factor == pytest.approx(Decimal(str(1.12 ** (days / 252))), abs=D("0.0001"))


def test_accrual_stops_at_maturity(db, cdi):
    start, last = cdi
    early = accrual_factor(db, terms(maturity_date=date(2025, 6, 30)), start, last)[0]
    late = accrual_factor(db, terms(), start, last)[0]
    assert early < late


def test_missing_index_data_is_flagged_not_guessed(db):
    factor, days, stale = accrual_factor(db, terms(), date(2025, 1, 1), date(2026, 1, 1))
    assert factor == D(1)
    assert days == 0
    assert stale is True


def test_position_is_valued_from_its_own_purchase_date(db, portfolio, cdi, cdb):
    terms_row = terms(asset_id=cdb.id)
    db.add(terms_row)
    db.commit()

    # Bought on the series' first day, so that day does not yield: 249 days.
    accrual = value_position(db, cdb, terms_row, portfolio.id, through=cdi[1])
    assert accrual is not None
    assert accrual.principal == D("10000")
    assert accrual.value == pytest.approx(D("10000") * D("1.0004") ** 249, abs=D("0.01"))
    assert accrual.business_days == 249
    assert accrual.interest > 0
    assert accrual.yield_percent == pytest.approx(D("10.5"), abs=D("0.5"))


def test_each_purchase_accrues_from_its_own_date(db, portfolio, cdi, cdb):
    """A position built in tranches must not use a single average date."""
    db.add(
        Transaction(
            portfolio_id=portfolio.id,
            asset_id=cdb.id,
            trade_date=date(2025, 7, 1),
            direction="CREDIT",
            op_type=OperationType.BUY.value,
            effect=PositionEffect.ACQUIRE.value,
            quantity=D("10000"),
            unit_price=D("1"),
            gross_amount=D("10000"),
            net_amount=D("-10000"),
            raw_movement="COMPRA / VENDA",
            raw_product="CDB - CDBTEST01 - BANCO TESTE",
            dedup_key="test:1",
        )
    )
    terms_row = terms(asset_id=cdb.id)
    db.add(terms_row)
    db.commit()

    accrual = value_position(db, cdb, terms_row, portfolio.id, through=cdi[1])
    assert accrual.principal == D("20000")
    single = D("10000") * D("1.0004") ** 249
    # The later tranche accrued for less time, so the total is below 2x the first.
    assert accrual.value < single * 2
    assert accrual.value > D("20000")


def test_accrued_prices_feed_the_portfolio_as_a_unit_price(db, portfolio, cdi, cdb):
    db.add(terms(asset_id=cdb.id))
    db.commit()
    prices = accrued_prices(db, portfolio.id, {cdb.id: D("10000")})
    assert cdb.id in prices
    assert prices[cdb.id] > D("1")  # accrued above par
    assert prices[cdb.id] < D("1.2")


def test_papers_without_terms_are_left_alone(db, portfolio, cdi, cdb):
    assert accrued_prices(db, portfolio.id, {cdb.id: D("10000")}) == {}


def test_implied_rate_is_solved_from_what_the_paper_actually_paid(db, portfolio, cdi, cdb):
    """A redeemed paper is a closed experiment: its cash flows reveal the rate.

    The B3 export never states the contracted rate, and real CDBs are rarely at
    exactly 100 % of CDI — this recovers it.
    """
    start, last = cdi
    terms_row = terms(asset_id=cdb.id)
    db.add(terms_row)

    # Redeem at 130 % of CDI: factor = 1.00052 ** 249 over the same window.
    factor = D("1.00052") ** 249
    db.add(
        Transaction(
            portfolio_id=portfolio.id,
            asset_id=cdb.id,
            trade_date=last,
            direction="DEBIT",
            op_type=OperationType.REDEMPTION.value,
            effect=PositionEffect.DISPOSE.value,
            quantity=D("10000"),
            unit_price=D("1"),
            gross_amount=(D("10000") * factor).quantize(D("0.01")),
            net_amount=(D("10000") * factor).quantize(D("0.01")),
            raw_movement="VENCIMENTO",
            raw_product="CDB - CDBTEST01 - BANCO TESTE",
            dedup_key="test:redeem",
        )
    )
    db.commit()

    implied = implied_percent_of_index(db, cdb, terms_row, portfolio.id)
    assert implied is not None
    assert implied["index_code"] == "CDI"
    assert implied["percent_of_index"] == pytest.approx(D("130"), abs=D("0.5"))


def test_no_implied_rate_without_a_redemption(db, portfolio, cdi, cdb):
    terms_row = terms(asset_id=cdb.id)
    db.add(terms_row)
    db.commit()
    assert implied_percent_of_index(db, cdb, terms_row, portfolio.id) is None
