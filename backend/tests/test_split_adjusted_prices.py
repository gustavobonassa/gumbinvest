"""Historical value across a split: prices and quantities must agree on shares.

The bug: a provider's daily closes are stated in *today's* shares — Yahoo
divides the whole pre-split series by the split ratio — while the ledger counts
the shares that existed on each date. Multiplying one by the other valued every
day before a split at a fraction of the truth, and the fraction was the ratio.

The symptom that found it: an ETF bought for R$573 showed R$96 of value on the
day it was bought, a R$477 "loss" out of thin air, which a per-class return then
printed as -4300%.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.portfolio.service import _in_ledger_shares

D = Decimal


def test_an_asset_that_never_split_keeps_its_series_untouched():
    series = [(date(2024, 1, 2), D("31.50")), (date(2024, 1, 3), D("32.10"))]

    assert _in_ledger_shares(series, []) == series


def test_closes_before_a_split_are_restated_into_that_day_s_shares():
    """The point of the whole exercise: value the day as it was worth."""
    series = [
        (date(2021, 12, 21), D("49.1167")),  # provider: already divided by 6
        (date(2026, 4, 20), D("80")),
        (date(2026, 4, 21), D("81")),  # the split day itself
        (date(2026, 8, 5), D("84.88")),
    ]

    restated = dict(_in_ledger_shares(series, [(date(2026, 4, 21), D(6))]))

    # What the holder actually paid that day was ~US$293 a share, and that is
    # what the curve has to value the position at.
    assert restated[date(2021, 12, 21)] == D("294.7002")
    assert restated[date(2026, 4, 20)] == D("480")
    # From the split day on, provider and ledger already agree.
    assert restated[date(2026, 4, 21)] == D("81")
    assert restated[date(2026, 8, 5)] == D("84.88")


def test_successive_events_compound_backwards():
    """Five yearly bonuses leave the oldest close needing all five factors."""
    series = [(date(2020 + n, 6, 1), D(100)) for n in range(6)]
    events = [(date(2021 + n, 12, 1), D("1.05")) for n in range(5)]

    restated = dict(_in_ledger_shares(series, events))

    # Each close carries every factor dated after it, and only those.
    assert restated[date(2020, 6, 1)] == D(100) * D("1.05") ** 5
    assert restated[date(2023, 6, 1)] == D(100) * D("1.05") ** 3
    assert restated[date(2025, 6, 1)] == D(100) * D("1.05")


# --- Declared splits the ledger's own prices contradict ---------------------
#
# A provider's event feed can disagree with the provider's own price series.
# It happened: a 10-for-1 was declared for a fund whose closes never moved, and
# applying it multiplied every earlier day of that class by ten — a 419% return
# in a month nobody earned. Every trade carries the price actually paid, which
# is the figure to reconcile against.

from app.db.models import Asset, AssetSplit, PriceHistory, Transaction
from app.portfolio.service import PortfolioService


def _priced_asset(db, portfolio, ticker: str, closes: dict[str, str], trades: dict[str, str]):
    """An asset with a stored close series and trades at known prices."""
    asset = Asset(ticker=ticker, name=ticker, kind="FII", currency="BRL")
    db.add(asset)
    db.commit()
    for day, close in closes.items():
        db.add(PriceHistory(asset_id=asset.id, date=date.fromisoformat(day), close=D(close), source="stub"))
    for index, (day, price) in enumerate(trades.items()):
        db.add(
            Transaction(
                portfolio_id=portfolio.id, asset_id=asset.id, broker_id=None, import_batch_id=None,
                trade_date=date.fromisoformat(day), direction="CREDIT", op_type="BUY",
                effect="ACQUIRE", quantity=D(1), unit_price=D(price), gross_amount=D(price),
                fees=D(0), taxes=D(0), net_amount=D(price), currency="BRL", fx_rate=None,
                raw_movement="Compra", raw_product=ticker, raw_institution="i", source_line=None,
                occurrence=0, dedup_key=f"{ticker}-{index}",
            )
        )
    db.commit()
    return asset


def test_a_split_the_prices_never_reflected_is_ignored(db, portfolio):
    """The phantom 10-for-1: closes and executions unchanged across the date."""
    asset = _priced_asset(
        db, portfolio, "VINO11",
        closes={"2023-12-05": "7.59", "2023-12-20": "7.62", "2024-02-02": "7.94", "2024-03-14": "7.41"},
        trades={"2023-12-05": "7.56", "2023-12-20": "7.58", "2024-02-02": "7.65", "2024-03-14": "7.43"},
    )
    db.add(AssetSplit(asset_id=asset.id, date=date(2024, 1, 31), ratio=D(10), source="yahoo"))
    db.commit()

    service = PortfolioService(db, portfolio.id)

    assert service.share_splits().get(asset.id, []) == []
    assert list(service.rejected_splits()) == [(asset.id, date(2024, 1, 31))]


def test_a_split_the_prices_do_reflect_is_kept(db, portfolio):
    """The same shape with a real 5-for-1: executions drop to the close."""
    asset = _priced_asset(
        db, portfolio, "SNAG11",
        closes={"2023-07-19": "9.50", "2023-08-01": "9.60", "2023-09-08": "9.17", "2023-10-02": "9.30"},
        trades={"2023-07-19": "46.67", "2023-08-01": "47.90", "2023-09-08": "9.09", "2023-10-02": "9.25"},
    )
    db.add(AssetSplit(asset_id=asset.id, date=date(2023, 8, 7), ratio=D(5), source="yahoo"))
    db.commit()

    service = PortfolioService(db, portfolio.id)

    assert service.share_splits()[asset.id] == [(date(2023, 8, 7), D(5))]
    assert service.rejected_splits() == {}


def test_a_hand_declared_ratio_is_never_second_guessed(db, portfolio):
    """It exists precisely because the provider was wrong or silent."""
    asset = _priced_asset(
        db, portfolio, "XPTO11",
        closes={"2023-12-05": "7.59", "2024-02-02": "7.94"},
        trades={"2023-12-05": "7.56", "2024-02-02": "7.65"},
    )
    db.add(AssetSplit(asset_id=asset.id, date=date(2024, 1, 31), ratio=D(10), source="manual"))
    db.commit()

    service = PortfolioService(db, portfolio.id)

    assert service.share_splits()[asset.id] == [(date(2024, 1, 31), D(10))]


def test_a_split_with_no_trade_after_it_is_trusted(db, portfolio):
    """Absence of evidence is not evidence: the provider is usually right."""
    asset = _priced_asset(
        db, portfolio, "VOOG",
        closes={"2025-10-09": "73.40", "2026-05-04": "77.00"},
        trades={"2025-10-09": "439.88"},
    )
    db.add(AssetSplit(asset_id=asset.id, date=date(2026, 4, 21), ratio=D(6), source="yahoo"))
    db.commit()

    service = PortfolioService(db, portfolio.id)

    assert service.share_splits()[asset.id] == [(date(2026, 4, 21), D(6))]


def test_a_small_bonus_is_never_judged(db, portfolio):
    """A 5% ratio cannot be told from the gap between a fill and a close."""
    asset = _priced_asset(
        db, portfolio, "ITSA4",
        closes={"2021-11-01": "10.00", "2022-01-05": "10.20"},
        trades={"2021-11-01": "10.05", "2022-01-05": "10.15"},
    )
    db.add(AssetSplit(asset_id=asset.id, date=date(2021, 12, 21), ratio=D("1.05"), source="yahoo"))
    db.commit()

    service = PortfolioService(db, portfolio.id)

    assert service.share_splits()[asset.id] == [(date(2021, 12, 21), D("1.05"))]
