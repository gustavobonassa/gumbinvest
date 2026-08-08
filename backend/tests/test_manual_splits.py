"""Declaring a share split by hand, and the AI proposal that fills the form.

The provider supplies most ratios automatically, but two groups of users are
left without them: whoever runs brapi or yfinance (neither publishes splits),
and anyone holding a paper the provider stays silent about. For them this is
the only way the historical curve can be right, so the rules that protect it —
a hand-typed ratio outranking the provider, and a wrong ratio being refused —
are worth pinning down.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Asset, AssetSplit
from app.market.service import MANUAL_SPLIT_SOURCE, _upsert_splits
from app.services.corporate_ai import normalize_splits


def _asset(db: Session, ticker: str = "VOOG") -> Asset:
    asset = Asset(ticker=ticker, name=ticker, kind="ETF_INTL", currency="USD")
    db.add(asset)
    db.commit()
    return asset


def test_a_provider_sync_never_overwrites_a_hand_typed_ratio(db: Session):
    """The only reason to type one is that the provider was wrong or silent."""
    asset = _asset(db)
    db.add(
        AssetSplit(
            asset_id=asset.id,
            date=date(2026, 4, 21),
            ratio=Decimal(6),
            source=MANUAL_SPLIT_SOURCE,
        )
    )
    db.commit()

    _upsert_splits(db, asset.id, [(date(2026, 4, 21), Decimal(2))], "yahoo")
    db.commit()

    row = db.scalar(select(AssetSplit).where(AssetSplit.asset_id == asset.id))
    assert row.ratio == Decimal(6)
    assert row.source == MANUAL_SPLIT_SOURCE


def test_a_provider_row_is_updated_by_the_provider(db: Session):
    """A corrected feed must still be able to correct itself."""
    asset = _asset(db)
    _upsert_splits(db, asset.id, [(date(2026, 4, 21), Decimal(2))], "yahoo")
    db.commit()
    _upsert_splits(db, asset.id, [(date(2026, 4, 21), Decimal(6))], "yahoo")
    db.commit()

    row = db.scalar(select(AssetSplit).where(AssetSplit.asset_id == asset.id))
    assert row.ratio == Decimal(6)


def test_a_ratio_of_one_is_not_stored(db: Session):
    asset = _asset(db)

    assert _upsert_splits(db, asset.id, [(date(2026, 4, 21), Decimal(1))], "yahoo") == 0
    assert db.scalars(select(AssetSplit)).all() == []


def test_declaring_a_split_changes_the_chain_fingerprint(db: Session, portfolio):
    """Otherwise the correction is stored and the chart keeps the old numbers."""
    from app.portfolio.service import PortfolioService

    asset = _asset(db)
    service = PortfolioService(db, portfolio.id)
    before = service.ledger_fingerprint()

    db.add(
        AssetSplit(
            asset_id=asset.id, date=date(2026, 4, 21), ratio=Decimal(6), source=MANUAL_SPLIT_SOURCE
        )
    )
    db.commit()

    assert PortfolioService(db, portfolio.id).ledger_fingerprint() != before


def test_editing_a_ratio_changes_the_fingerprint_too(db: Session, portfolio):
    """The row count stays the same, so counting rows alone would miss it."""
    from app.portfolio.service import PortfolioService

    asset = _asset(db)
    row = AssetSplit(
        asset_id=asset.id, date=date(2026, 4, 21), ratio=Decimal(6), source=MANUAL_SPLIT_SOURCE
    )
    db.add(row)
    db.commit()
    before = PortfolioService(db, portfolio.id).ledger_fingerprint()

    row.ratio = Decimal(2)
    db.commit()

    assert PortfolioService(db, portfolio.id).ledger_fingerprint() != before


class TestAiProposals:
    """What the model returns is a form filler, so it is checked hard."""

    def test_a_usable_proposal_survives(self):
        items = normalize_splits(
            {"splits": [{"date": "2026-04-21", "ratio": 6, "source": "Vanguard"}]},
            known_dates=set(),
        )

        assert items == [
            {
                "date": "2026-04-21",
                "ratio": "6",
                "event_type": None,
                "rationale": None,
                "source": "Vanguard",
            }
        ]

    def test_an_already_known_event_is_not_proposed_again(self):
        data = {"splits": [{"date": "2023-08-02", "ratio": 10}]}

        assert normalize_splits(data, known_dates={"2023-08-02"}) == []

    def test_an_absurd_ratio_is_refused(self):
        """A model that answers with a price instead of a ratio must not be
        allowed to restate years of history."""
        data = {"splits": [{"date": "2026-04-21", "ratio": 293.12}]}

        assert normalize_splits(data, known_dates=set()) == []

    def test_a_ratio_of_one_and_a_bad_date_are_dropped(self):
        data = {
            "splits": [
                {"date": "2026-04-21", "ratio": 1},
                {"date": "não sei", "ratio": 2},
                {"ratio": 2},
            ]
        }

        assert normalize_splits(data, known_dates=set()) == []

    def test_the_same_date_is_never_proposed_twice(self):
        data = {"splits": [{"date": "2026-04-21", "ratio": 6}, {"date": "2026-04-21", "ratio": 3}]}

        assert [item["ratio"] for item in normalize_splits(data, known_dates=set())] == ["6"]

    def test_junk_is_an_empty_list_rather_than_an_error(self):
        assert normalize_splits(None, set()) == []
        assert normalize_splits({}, set()) == []
        assert normalize_splits({"splits": "nope"}, set()) == []
