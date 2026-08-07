"""AI-found corporate events: validation, dedupe against decisions, apply."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.db.models import Asset, AssetSuccession, SuccessionAiSuggestion, Transaction
from app.services import corporate_ai


def seed_asset(db, ticker, kind="STOCK") -> Asset:
    asset = Asset(ticker=ticker, name=ticker, kind=kind, currency="BRL")
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def seed_trade(db, portfolio, asset, day="2024-03-01") -> None:
    db.add(
        Transaction(
            portfolio_id=portfolio.id,
            asset_id=asset.id,
            trade_date=date.fromisoformat(day),
            direction="CREDIT",
            op_type="BUY",
            effect="ACQUIRE",
            quantity=Decimal(10),
            unit_price=Decimal(10),
            gross_amount=Decimal(100),
            net_amount=Decimal(100),
            raw_movement="Compra",
            raw_product=asset.ticker,
            dedup_key=f"test:{asset.ticker}:{day}",
            occurrence=0,
        )
    )
    db.commit()


def event(from_ticker="AAAA3", to_ticker="BBBB3", day="2024-06-28", **extra) -> dict:
    return {
        "from_ticker": from_ticker,
        "to_ticker": to_ticker,
        "effective_date": day,
        "cash_amount": 0,
        "event_type": "rename",
        "rationale": "mudança de código",
        "source": "b3.com.br",
        **extra,
    }


def test_scan_context_covers_transacted_listed_assets(db, portfolio):
    stock = seed_asset(db, "AAAA3")
    cdb = seed_asset(db, "CDB X", kind="FIXED_INCOME")
    seed_trade(db, portfolio, stock)
    seed_trade(db, portfolio, cdb)
    seed_asset(db, "ZZZZ3")  # never traded: not the scan's business

    context, known = corporate_ai.scan_context(db, portfolio.id)
    assert known == {"AAAA3"}
    assert context[0]["ticker"] == "AAAA3"
    assert context[0]["primeiro_movimento"] == "2024-03-01"
    assert context[0]["evento_ja_declarado"] is False


def test_normalize_events_validates_and_dedupes():
    known = {"AAAA3", "CCCC4"}
    data = {
        "events": [
            event(),                                   # ok
            event(from_ticker="AAAA3"),                # duplicate from_ticker
            event(from_ticker="MMMM3"),                # not the user's ticker
            event(from_ticker="CCCC4", day="not-a-date"),  # bad date
            event(from_ticker="CCCC4", to_ticker="CCCC4"),  # self-succession
            {"garbage": True},
        ]
    }
    items = corporate_ai.normalize_events(data, known)
    assert len(items) == 1
    assert items[0]["from_ticker"] == "AAAA3"
    assert items[0]["effective_date"] == date(2024, 6, 28)
    assert corporate_ai.normalize_events(None, known) == []


def test_store_suggestions_skips_settled_decisions(db, portfolio):
    old = seed_asset(db, "AAAA3")
    new = seed_asset(db, "BBBB3")
    declared_old = seed_asset(db, "DDDD3")
    db.add(
        AssetSuccession(
            portfolio_id=portfolio.id,
            from_asset_id=declared_old.id,
            to_asset_id=new.id,
            effective_date=date(2024, 1, 2),
        )
    )
    db.add(
        SuccessionAiSuggestion(
            portfolio_id=portfolio.id,
            from_ticker="EEEE3",
            to_ticker="FFFF3",
            effective_date=date(2024, 5, 6),
            cash_amount=Decimal(0),
            event_type="rename",
            status="declined",
            provider="anthropic",
            model="claude-sonnet-5",
        )
    )
    db.commit()

    items = [
        corporate_ai.normalize_events({"events": [event()]}, {"AAAA3"})[0],
        corporate_ai.normalize_events(
            {"events": [event(from_ticker="DDDD3")]}, {"DDDD3"}
        )[0],  # already declared
        corporate_ai.normalize_events(
            {"events": [event(from_ticker="EEEE3", to_ticker="FFFF3", day="2024-05-06")]},
            {"EEEE3"},
        )[0],  # previously declined, identical
    ]
    stored = corporate_ai.store_suggestions(
        db, portfolio.id, items, provider="anthropic", model="claude-sonnet-5"
    )
    db.commit()
    assert [row.from_ticker for row in stored] == ["AAAA3"]

    # A second scan proposing the same pending event stores nothing new.
    again = corporate_ai.store_suggestions(
        db, portfolio.id, items[:1], provider="anthropic", model="claude-sonnet-5"
    )
    assert again == []


def test_accept_creates_succession_and_resolves(db, portfolio):
    seed_asset(db, "AAAA3")
    target = seed_asset(db, "BBBB3")
    item = corporate_ai.normalize_events({"events": [event(cash_amount=12.5)]}, {"AAAA3"})[0]
    (row,) = corporate_ai.store_suggestions(
        db, portfolio.id, [item], provider="anthropic", model="claude-sonnet-5"
    )
    db.commit()

    succession = corporate_ai.accept_suggestion(db, portfolio.id, row)
    db.commit()
    assert succession.to_asset_id == target.id
    assert succession.source == "ai"
    assert succession.cash_amount == Decimal("12.50")
    assert row.status == "accepted" and row.resolved_at is not None

    stored = db.scalar(select(AssetSuccession).where(AssetSuccession.portfolio_id == portfolio.id))
    assert stored is not None and stored.effective_date == date(2024, 6, 28)
