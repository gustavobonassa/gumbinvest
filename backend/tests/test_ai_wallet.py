"""AI wallet: trade math, valuation, suggestions, snapshots.

Everything runs against the service layer directly (no HTTP, no SSE) with
assets and quotes seeded by hand — the same style as test_ai_portfolio_chat.
Network-touching seams (lookup.resolve, ensure_market_data, providers) are
monkeypatched or avoided by seeding the Asset rows the resolver would find.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.dates import local_today
from app.db.models import (
    AiWallet,
    AiWalletCategory,
    AiWalletEvent,
    AiWalletPosition,
    AiWalletSnapshot,
    AiWalletSuggestion,
    Asset,
    AuditLog,
    FxRate,
    IndexRate,
    PriceHistory,
    Quote,
    WatchlistItem,
)
from app.market.lookup import MarketHit
from app.services import ai_wallet as svc
from app.services.ai_research import extract_json


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    """Resolution retries sleep in production; tests must not."""
    monkeypatch.setattr(svc, "RESOLVE_RETRY_DELAY", 0)


def make_wallet(db, name="Concorrente A") -> AiWallet:
    wallet = AiWallet(name=name, provider="anthropic", model="claude-sonnet-5")
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    return wallet


def seed_asset(db, ticker, kind="STOCK", currency="BRL", price="10") -> Asset:
    asset = Asset(ticker=ticker, name=ticker, kind=kind, currency=currency)
    db.add(asset)
    db.commit()
    db.refresh(asset)
    if price is not None:
        db.add(
            Quote(
                asset_id=asset.id,
                price=Decimal(price),
                currency=currency,
                source="test",
                fetched_at=datetime.now(UTC),
            )
        )
        db.commit()
    return asset


def seed_fx(db, rate="5.00") -> None:
    db.add(FxRate(base="USD", quote="BRL", date=local_today(), rate=Decimal(rate)))
    db.commit()


def set_price(db, asset, price) -> None:
    quote = db.get(Quote, asset.id)
    quote.price = Decimal(price)
    db.commit()


def generate(db, wallet, category, items, used_search=False) -> dict:
    return svc.apply_generation(db, wallet, category, items, used_search=used_search)


def stock_item(ticker, pct, rationale=None) -> dict:
    return {"ticker": ticker, "name": ticker, "allocation_pct": Decimal(pct), "rationale": rationale}


def make_suggestion(db, wallet, category, action, **kw) -> AiWalletSuggestion:
    suggestion = AiWalletSuggestion(
        wallet_id=wallet.id,
        category=category,
        batch_id="batch-1",
        action=action,
        name="",
        payload=kw.pop("payload", {}),
        status="pending",
        provider=wallet.provider,
        model=wallet.model,
        **kw,
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    return suggestion


# ---------------------------------------------------------------------------
# JSON extraction and normalisation (pure)


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"positions": []}\n```') == {"positions": []}
    prose = 'Claro! Segue a resposta:\n{"a": {"b": "chave } com chaves"}}\nEspero que ajude.'
    assert extract_json(prose) == {"a": {"b": "chave } com chaves"}}
    assert extract_json("sem json aqui") is None
    assert extract_json(None) is None
    assert extract_json("[1, 2]") is None
    assert extract_json('{"quebrado": ') is None


def test_normalize_generation_scales_overallocation():
    data = {
        "positions": [
            {"ticker": "petr4", "allocation_pct": 80, "rationale": "a"},
            {"ticker": "VALE3", "allocation_pct": 60, "rationale": "b"},
        ]
    }
    items = svc.normalize_generation(data, "ACOES")
    assert [item["ticker"] for item in items] == ["PETR4", "VALE3"]
    assert sum(item["allocation_pct"] for item in items) <= Decimal(100)
    # Proportions kept: 80/60 = 4/3.
    ratio = items[0]["allocation_pct"] / items[1]["allocation_pct"]
    assert abs(ratio - Decimal(80) / Decimal(60)) < Decimal("0.01")


def test_normalize_generation_keeps_underallocation_as_cash():
    data = {"positions": [{"ticker": "PETR4", "allocation_pct": 40}]}
    items = svc.normalize_generation(data, "ACOES")
    assert items[0]["allocation_pct"] == Decimal(40)


def test_normalize_generation_renda_fixa_requires_valid_index():
    data = {
        "positions": [
            {"name": "CDB 110% CDI", "index_code": "CDI", "percent_of_index": 110, "allocation_pct": 50},
            {"name": "CDB estranho", "index_code": "XYZ", "allocation_pct": 50},
            {"index_code": "CDI", "allocation_pct": 10},  # no name
        ]
    }
    items = svc.normalize_generation(data, "RENDA_FIXA")
    assert len(items) == 1
    assert items[0]["percent_of_index"] == Decimal(110)


def test_normalize_generation_decodes_unicode_escapes():
    data = {"positions": [{"ticker": "PETR4", "allocation_pct": 10, "rationale": "a\\u00e7\\u00e3o boa"}]}
    items = svc.normalize_generation(data, "ACOES")
    assert items[0]["rationale"] == "ação boa"


def test_normalize_candidates_dedupes_and_uppercases():
    data = {"candidates": [{"ticker": "petr4"}, {"ticker": "PETR4"}, {"ticker": "vale3"}, {}]}
    assert svc.normalize_candidates(data) == ["PETR4", "VALE3"]


def test_normalize_suggestions_validates_shape():
    data = {
        "suggestions": [
            {"action": "buy_new", "ticker": "PETR4", "amount_brl": 1000, "rationale": "x"},
            {"action": "reduce", "ticker": "VALE3", "amount_brl": 0},  # zero amount
            {"action": "sell_all", "ticker": "HGLG11"},
            {"action": "increase", "amount_brl": 500},  # no ticker
            {"action": "explodir", "ticker": "PETR4", "amount_brl": 10},  # unknown action
            {"action": "rebalance", "ticker": "PETR4", "amount_brl": 500, "to_ticker": "vale3", "to_category": "acoes"},
        ]
    }
    items = svc.normalize_suggestions(data, "ACOES")
    actions = [item["action"] for item in items]
    assert actions == ["buy_new", "sell_all", "rebalance"]
    rebalance = items[-1]
    assert rebalance["to_ticker"] == "VALE3"
    assert rebalance["to_category"] == "ACOES"


# ---------------------------------------------------------------------------
# Generation math


def test_generation_brl_buys_whole_shares(db):
    wallet = make_wallet(db)
    seed_asset(db, "PETR4", price="37.50")
    result = generate(db, wallet, "ACOES", [stock_item("PETR4", 50, "estatal barata")])
    assert result["positions"] == 1 and not result["skipped"]

    position = db.scalar(select(AiWalletPosition))
    # 50% of 10k = R$5.000; 5000/37.50 = 133,33 → 133 whole shares.
    assert position.quantity == Decimal(133)
    assert position.cost_brl == Decimal("4987.50")
    assert position.rationale == "estatal barata"

    category = db.scalar(select(AiWalletCategory))
    assert category.cash == Decimal("5012.50")

    actions = {e.action for e in db.scalars(select(AiWalletEvent)).all()}
    assert "category.generated" in actions


def test_generation_usd_fractional_with_fx(db):
    wallet = make_wallet(db)
    seed_fx(db, "5.00")
    seed_asset(db, "AAPL", currency="USD", price="333")
    generate(db, wallet, "STOCKS", [stock_item("AAPL", 100)])

    position = db.scalar(select(AiWalletPosition))
    # 10000 / (333 × 5) = 6,006006 → 6.0060 (4 dp).
    assert position.quantity == Decimal("6.0060")
    assert position.avg_fx == Decimal(5)
    assert position.cost_brl == (Decimal("6.0060") * Decimal(333) * 5).quantize(Decimal("0.01"))
    category = db.scalar(select(AiWalletCategory))
    assert category.cash == Decimal(10000) - position.cost_brl


def test_generation_defers_usd_without_fx(db):
    wallet = make_wallet(db)
    seed_asset(db, "AAPL", currency="USD", price="200")
    result = generate(db, wallet, "STOCKS", [stock_item("AAPL", 100)])
    # No FX to price the buy — the money is reserved, not silently dropped.
    assert not result["skipped"]
    assert result["pending"][0]["ticker"] == "AAPL"
    position = db.scalar(select(AiWalletPosition))
    assert position.pending_brl == Decimal(10000) and position.quantity == Decimal(0)
    category = db.scalar(select(AiWalletCategory))
    assert category.cash == Decimal(0)

    # The wallet still values the reservation at face.
    valuation = svc.value_wallet(db, wallet)
    row = valuation["categories"]["STOCKS"]["positions"][0]
    assert row["market_value_brl"] == Decimal(10000)
    assert row["pending_brl"] == Decimal(10000)

    # Once FX arrives, the reservation settles into shares: 10000/(200×5)=10.
    seed_fx(db, "5.00")
    assert svc.settle_pending_positions(db, wallet) == 1
    db.commit()
    db.refresh(position)
    assert position.quantity == Decimal(10)
    assert position.pending_brl == Decimal(0)
    assert position.cost_brl == Decimal(10000)
    actions = [e.action for e in db.scalars(select(AiWalletEvent)).all()]
    assert "position.settled" in actions


def test_generation_defers_without_quote_then_settles(db):
    wallet = make_wallet(db)
    asset = seed_asset(db, "PETR4", price=None)  # resolvable row, no quote yet
    result = generate(db, wallet, "ACOES", [stock_item("PETR4", 60)])
    assert not result["skipped"] and result["pending"][0]["ticker"] == "PETR4"
    position = db.scalar(select(AiWalletPosition))
    assert position.pending_brl == Decimal(6000)
    category = db.scalar(select(AiWalletCategory))
    assert category.cash == Decimal(4000)

    db.add(
        Quote(
            asset_id=asset.id,
            price=Decimal("40"),
            currency="BRL",
            source="test",
            fetched_at=datetime.now(UTC),
        )
    )
    db.commit()
    assert svc.settle_pending_positions(db, wallet) == 1
    db.commit()
    db.refresh(position)
    assert position.quantity == Decimal(150)  # 6000 / 40
    assert position.cost_brl == Decimal(6000)
    assert position.pending_brl == Decimal(0)


def test_settle_refunds_below_one_unit(db):
    wallet = make_wallet(db)
    asset = seed_asset(db, "PETR4", price=None)
    generate(db, wallet, "ACOES", [stock_item("PETR4", 1)])  # reserves R$100
    db.add(
        Quote(
            asset_id=asset.id,
            price=Decimal("500"),
            currency="BRL",
            source="test",
            fetched_at=datetime.now(UTC),
        )
    )
    db.commit()
    assert svc.settle_pending_positions(db, wallet) == 1
    db.commit()
    # R$100 cannot buy a R$500 share: refunded, position removed, all visible.
    assert db.scalar(select(AiWalletPosition)) is None
    category = db.scalar(select(AiWalletCategory))
    assert category.cash == Decimal(10000)
    events = db.scalars(select(AiWalletEvent).where(AiWalletEvent.action == "position.settled")).all()
    assert events and "refunded_brl" in events[0].detail


def test_sell_all_pending_reservation_returns_cash(db):
    wallet = make_wallet(db)
    seed_asset(db, "PETR4", price=None)
    generate(db, wallet, "ACOES", [stock_item("PETR4", 100)])
    position = db.scalar(select(AiWalletPosition))
    suggestion = make_suggestion(
        db, wallet, "ACOES", "sell_all", ticker="PETR4", position_id=position.id
    )
    detail = svc.apply_suggestion(db, wallet, suggestion)
    assert detail["amount_brl"] == Decimal(10000) and detail["closed"] is True
    category = db.scalar(select(AiWalletCategory))
    assert category.cash == Decimal(10000)
    assert db.scalar(select(AiWalletPosition)) is None


def test_generation_skips_unknown_ticker(db, monkeypatch):
    monkeypatch.setattr(svc.lookup, "resolve", lambda ticker: None)
    wallet = make_wallet(db)
    result = generate(db, wallet, "ACOES", [stock_item("XPTO99", 100)])
    assert result["positions"] == 0
    assert result["skipped"][0]["ticker"] == "XPTO99"
    assert db.scalar(select(Asset)) is None  # never mint a junk row


def test_generation_stores_strategy_thesis(db):
    wallet = make_wallet(db)
    seed_asset(db, "PETR4", price="30")
    svc.apply_generation(
        db,
        wallet,
        "ACOES",
        [stock_item("PETR4", 50)],
        used_search=False,
        strategy="Foco em geradoras de caixa; rever se a Selic cair abaixo de 10%.",
    )
    row = db.scalar(select(AiWalletCategory))
    assert row.thesis and row.thesis.startswith("Foco em geradoras")
    valuation = svc.value_wallet(db, wallet)
    assert valuation["categories"]["ACOES"]["thesis"] == row.thesis


def test_generation_rejects_wrong_category(db):
    wallet = make_wallet(db)
    seed_asset(db, "HGLG11", kind="FII", price="160")
    result = generate(db, wallet, "ACOES", [stock_item("HGLG11", 100)])
    assert result["positions"] == 0 and result["skipped"]


def test_generation_twice_conflicts(db):
    wallet = make_wallet(db)
    seed_asset(db, "PETR4", price="30")
    generate(db, wallet, "ACOES", [stock_item("PETR4", 50)])
    with pytest.raises(IntegrityError):
        generate(db, wallet, "ACOES", [stock_item("PETR4", 50)])
    db.rollback()


def test_generation_renda_fixa_and_accrual(db):
    wallet = make_wallet(db)
    today = local_today()
    day = today - timedelta(days=21)
    while day <= today:
        if day.weekday() < 5:
            db.add(IndexRate(code="CDI", date=day, value=Decimal("0.05")))
        day += timedelta(days=1)
    db.commit()

    items = [
        {
            "name": "CDB 110% CDI",
            "index_code": "CDI",
            "percent_of_index": Decimal(110),
            "spread_annual": None,
            "fixed_rate_annual": None,
            "allocation_pct": Decimal(60),
            "rationale": "pós-fixado líquido",
        }
    ]
    generate(db, wallet, "RENDA_FIXA", items)
    position = db.scalar(select(AiWalletPosition))
    assert position.is_fixed_income and position.cost_brl == Decimal(6000)

    # Backdate the start so there is CDI to accrue over.
    position.fi_start_date = today - timedelta(days=21)
    db.commit()
    valuation = svc.value_wallet(db, wallet)
    row = valuation["categories"]["RENDA_FIXA"]["positions"][0]
    assert row["market_value_brl"] > Decimal(6000)
    assert row["fi_label"] == "110% CDI"


def test_valuation_falls_back_to_cost_without_quote(db):
    wallet = make_wallet(db)
    asset = seed_asset(db, "PETR4", price="40")
    generate(db, wallet, "ACOES", [stock_item("PETR4", 100)])
    db.delete(db.get(Quote, asset.id))
    db.commit()

    valuation = svc.value_wallet(db, wallet)
    row = valuation["categories"]["ACOES"]["positions"][0]
    assert row["market_value_brl"] == row["cost_brl"]
    assert row["priced"] is False
    assert "PETR4" in valuation["unpriced"]


# ---------------------------------------------------------------------------
# Suggestions


def test_accept_buy_new_caps_at_cash(db):
    wallet = make_wallet(db)
    seed_asset(db, "PETR4", price="100")
    generate(db, wallet, "ACOES", [stock_item("PETR4", 90)])  # cash left: 1000

    suggestion = make_suggestion(
        db, wallet, "ACOES", "buy_new", ticker="PETR4", amount_brl=Decimal(5000)
    )
    detail = svc.apply_suggestion(db, wallet, suggestion)
    assert detail["amount_brl"] == Decimal(1000)  # capped by the category's cash
    position = db.scalar(select(AiWalletPosition))
    assert position.quantity == Decimal(100)
    assert suggestion.status == "accepted"


def test_accept_increase_reweights_average(db):
    wallet = make_wallet(db)
    asset = seed_asset(db, "PETR4", price="10")
    generate(db, wallet, "ACOES", [stock_item("PETR4", 50)])  # 500 shares @10, cash 5000
    set_price(db, asset, "20")

    position = db.scalar(select(AiWalletPosition))
    suggestion = make_suggestion(
        db, wallet, "ACOES", "increase", ticker="PETR4", amount_brl=Decimal(1000), position_id=position.id
    )
    svc.apply_suggestion(db, wallet, suggestion)
    db.refresh(position)
    assert position.quantity == Decimal(550)
    expected = (Decimal(500) * 10 + Decimal(50) * 20) / Decimal(550)
    assert abs(Decimal(position.avg_price) - expected) < Decimal("0.0001")
    assert position.cost_brl == Decimal(6000)


def test_accept_reduce_releases_proportional_cost(db):
    wallet = make_wallet(db)
    asset = seed_asset(db, "PETR4", price="10")
    generate(db, wallet, "ACOES", [stock_item("PETR4", 100)])  # 1000 shares, cash 0
    set_price(db, asset, "20")  # position now worth 20 000

    position = db.scalar(select(AiWalletPosition))
    suggestion = make_suggestion(
        db, wallet, "ACOES", "reduce", ticker="PETR4", amount_brl=Decimal(5000), position_id=position.id
    )
    detail = svc.apply_suggestion(db, wallet, suggestion)
    assert detail["amount_brl"] == Decimal(5000)
    db.refresh(position)
    assert position.quantity == Decimal(750)
    assert position.cost_brl == Decimal(7500)
    category = db.scalar(select(AiWalletCategory))
    assert category.cash == Decimal(5000)


def test_accept_sell_all_closes_and_credits(db):
    wallet = make_wallet(db)
    asset = seed_asset(db, "PETR4", price="10")
    generate(db, wallet, "ACOES", [stock_item("PETR4", 100)])
    set_price(db, asset, "20")

    position = db.scalar(select(AiWalletPosition))
    suggestion = make_suggestion(
        db, wallet, "ACOES", "sell_all", ticker="PETR4", position_id=position.id
    )
    detail = svc.apply_suggestion(db, wallet, suggestion)
    assert detail["closed"] is True
    assert db.scalar(select(AiWalletPosition)) is None
    category = db.scalar(select(AiWalletCategory))
    assert category.cash == Decimal(20000)
    actions = [e.action for e in db.scalars(select(AiWalletEvent)).all()]
    assert "position.sell" in actions


def test_rebalance_cross_category_preserves_wallet_value(db):
    wallet = make_wallet(db)
    petr = seed_asset(db, "PETR4", price="10")
    seed_asset(db, "HGLG11", kind="FII", price="100")
    generate(db, wallet, "ACOES", [stock_item("PETR4", 100)])
    generate(db, wallet, "FII", [stock_item("HGLG11", 50)])
    set_price(db, petr, "20")

    before = svc.value_wallet(db, wallet)["value"]
    source = db.scalar(select(AiWalletPosition).where(AiWalletPosition.ticker == "PETR4"))
    suggestion = make_suggestion(
        db,
        wallet,
        "ACOES",
        "rebalance",
        ticker="PETR4",
        amount_brl=Decimal(2000),
        to_ticker="HGLG11",
        to_category="FII",
        position_id=source.id,
    )
    detail = svc.apply_suggestion(db, wallet, suggestion)
    assert detail["to_ticker"] == "HGLG11"
    assert detail["bought_brl"] == Decimal(2000)  # 20 shares @100

    after = svc.value_wallet(db, wallet)
    assert after["value"] == before  # internal flow only
    fii = db.scalar(select(AiWalletPosition).where(AiWalletPosition.ticker == "HGLG11"))
    assert fii.quantity == Decimal(70)
    actions = [e.action for e in db.scalars(select(AiWalletEvent)).all()]
    assert "position.rebalance" in actions


def test_rebalance_to_category_cash(db):
    wallet = make_wallet(db)
    seed_asset(db, "PETR4", price="10")
    seed_asset(db, "HGLG11", kind="FII", price="100")
    generate(db, wallet, "ACOES", [stock_item("PETR4", 100)])
    generate(db, wallet, "FII", [stock_item("HGLG11", 100)])

    source = db.scalar(select(AiWalletPosition).where(AiWalletPosition.ticker == "PETR4"))
    suggestion = make_suggestion(
        db,
        wallet,
        "ACOES",
        "rebalance",
        ticker="PETR4",
        amount_brl=Decimal(1000),
        to_category="FII",
        position_id=source.id,
    )
    svc.apply_suggestion(db, wallet, suggestion)
    fii_row = db.scalar(select(AiWalletCategory).where(AiWalletCategory.category == "FII"))
    assert fii_row.cash == Decimal(1000)


def test_suggestion_target_rules(db):
    wallet = make_wallet(db)
    seed_asset(db, "PETR4", price="10")
    generate(db, wallet, "ACOES", [stock_item("PETR4", 100)])

    # Existing position in another activated category: fine.
    seed_asset(db, "HGLG11", kind="FII", price="100")
    generate(db, wallet, "FII", [stock_item("HGLG11", 50)])
    ok = {"action": "rebalance", "ticker": "PETR4", "to_ticker": "HGLG11", "to_category": "FII"}
    assert svc.suggestion_target_error(db, wallet.id, "ACOES", ok) is None

    # New asset in a different category: refused.
    bad = {"action": "rebalance", "ticker": "PETR4", "to_ticker": "MXRF11", "to_category": "FII"}
    assert svc.suggestion_target_error(db, wallet.id, "ACOES", bad) is not None

    # Cash of a never-generated category: refused.
    never = {"action": "rebalance", "ticker": "PETR4", "to_category": "CRIPTO", "to_ticker": None}
    assert svc.suggestion_target_error(db, wallet.id, "ACOES", never) is not None

    # Position that does not exist: refused.
    ghost = {"action": "reduce", "ticker": "VALE3"}
    assert svc.suggestion_target_error(db, wallet.id, "ACOES", ghost) is not None


def test_apply_suggestion_error_keeps_state(db):
    wallet = make_wallet(db)
    seed_asset(db, "PETR4", price="10")
    generate(db, wallet, "ACOES", [stock_item("PETR4", 100)])
    suggestion = make_suggestion(
        db, wallet, "CRIPTO", "buy_new", ticker="BTC", amount_brl=Decimal(1000)
    )
    with pytest.raises(svc.SuggestionError):
        svc.apply_suggestion(db, wallet, suggestion)  # category never generated
    db.rollback()
    db.refresh(suggestion)
    assert suggestion.status == "pending"


# ---------------------------------------------------------------------------
# Snapshots


def test_snapshot_chain_and_category_activation(db):
    wallet = make_wallet(db)
    asset = seed_asset(db, "PETR4", price="10")
    generate(db, wallet, "ACOES", [stock_item("PETR4", 100)])  # 1000 shares, cash 0

    today = local_today()
    for offset, close in ((2, "10"), (1, "11")):
        db.add(PriceHistory(asset_id=asset.id, date=today - timedelta(days=offset), close=Decimal(close), source="test"))
    db.commit()

    svc.snapshot_wallet(db, wallet, on=today - timedelta(days=2))
    svc.snapshot_wallet(db, wallet, on=today - timedelta(days=1))
    db.commit()
    rows = db.scalars(select(AiWalletSnapshot).order_by(AiWalletSnapshot.date)).all()
    assert rows[0].value == Decimal(10000) and rows[0].return_factor == Decimal(1)
    assert rows[1].value == Decimal(11000)
    assert abs(Decimal(rows[1].return_factor) - Decimal("1.1")) < Decimal("0.0001")

    # Activating a second category adds equal value and flow: no factor jump.
    set_price(db, asset, "11")
    generate(db, wallet, "FII", [])  # empty: R$10.000 all in cash
    db.commit()
    db.expire_all()  # the upsert is raw SQL; drop stale identity-map copies
    latest = db.scalar(
        select(AiWalletSnapshot)
        .where(AiWalletSnapshot.date == today)
        .order_by(AiWalletSnapshot.id.desc())
    )
    assert latest.value == Decimal(21000)
    assert latest.invested == Decimal(20000)
    assert abs(Decimal(latest.return_factor) - Decimal("1.1")) < Decimal("0.0001")

    # Upsert is idempotent: snapshotting today twice keeps one row per day.
    svc.snapshot_ai_wallets(db)
    days = [row.date for row in db.scalars(select(AiWalletSnapshot)).all()]
    assert len(days) == len(set(days))


# ---------------------------------------------------------------------------
# Deletion and asset cleanup


def test_delete_wallet_cleans_exclusive_assets(db):
    wallet = make_wallet(db)
    seed_asset(db, "PETR4", price="10")
    seed_asset(db, "HGLG11", kind="FII", price="100")
    generate(db, wallet, "ACOES", [stock_item("PETR4", 100)])
    generate(db, wallet, "FII", [stock_item("HGLG11", 50)])
    db.add(WatchlistItem(ticker="HGLG11"))
    db.commit()
    make_suggestion(db, wallet, "ACOES", "sell_all", ticker="PETR4")
    svc.snapshot_ai_wallets(db)

    svc.delete_wallet(db, wallet)
    for model in (AiWallet, AiWalletCategory, AiWalletPosition, AiWalletSuggestion, AiWalletEvent, AiWalletSnapshot):
        assert db.scalar(select(model)) is None, model.__name__
    tickers = {asset.ticker for asset in db.scalars(select(Asset)).all()}
    assert "PETR4" not in tickers  # wallet-exclusive: leaves the refresh set too
    assert "HGLG11" in tickers  # watchlisted: survives
    trail = db.scalars(select(AuditLog)).all()
    assert any(entry.action == "ai_wallet.deleted" for entry in trail)


def test_get_or_create_tracks_minted_rows(db, monkeypatch):
    seed_asset(db, "PETR4", price="10")
    created: list[int] = []
    # Found, not minted: nothing tracked.
    assert svc.get_or_create_wallet_asset(db, "ACOES", "PETR4", created) is not None
    assert created == []

    monkeypatch.setattr(
        svc.lookup,
        "resolve",
        lambda ticker: MarketHit(
            ticker="VALE3", name="Vale", kind="STOCK", currency="BRL",
            market_symbol="VALE3.SA", exchange="B3",
        ),
    )
    monkeypatch.setattr(svc, "ensure_market_data", lambda db_, asset: None)
    minted = svc.get_or_create_wallet_asset(db, "ACOES", "VALE3", created)
    assert minted is not None and created == [minted.id]


def test_cleanup_unused_assets_keeps_every_live_reference(db):
    wallet = make_wallet(db)
    bought = seed_asset(db, "PETR4", price="10")
    generate(db, wallet, "ACOES", [stock_item("PETR4", 100)])
    stray = seed_asset(db, "VALE3", price="60")
    watched = seed_asset(db, "WEGE3", price="40")
    db.add(WatchlistItem(ticker="WEGE3"))
    db.commit()
    suggested = seed_asset(db, "BBAS3", price="28")
    make_suggestion(db, wallet, "ACOES", "buy_new", ticker="BBAS3", amount_brl=Decimal(500))

    removed = svc.cleanup_unused_assets(db, [bought.id, stray.id, watched.id, suggested.id])
    db.commit()
    assert removed == 1
    tickers = {asset.ticker for asset in db.scalars(select(Asset)).all()}
    assert tickers == {"PETR4", "WEGE3", "BBAS3"}  # only the stray candidate left
    trail = db.scalars(select(AuditLog)).all()
    assert any(entry.action == "asset.unwatch" for entry in trail)


class TestScreenerInjection:
    """The pre-screened candidate block added to phase A.

    The invariant that matters most here is the negative one: a user who never
    runs the universe ingest must get exactly the prompt they got before the
    screener existed.
    """

    def test_prompt_is_byte_identical_without_a_universe(self):
        from app.api.routes.ai_wallet import _phase_a_prompt

        assert _phase_a_prompt("ACOES", False, []) == _phase_a_prompt("ACOES", False)
        assert _phase_a_prompt("FII", True, None) == _phase_a_prompt("FII", True)

    def test_prescreened_tickers_reach_the_prompt(self):
        from app.api.routes.ai_wallet import _phase_a_prompt

        rows = [{"ticker": "TST3", "p_l": 8.1}, {"ticker": "AAA11", "dividend_yield_pct": 9.4}]
        prompt = _phase_a_prompt("ACOES", False, rows)
        assert "TST3" in prompt and "AAA11" in prompt

    def test_the_block_disclaims_completeness_and_recommendation(self):
        """A pre-screened list the model reads as advice is worse than none."""
        from app.api.routes.ai_wallet import _phase_a_prompt

        prompt = _phase_a_prompt("ACOES", False, [{"ticker": "TST3"}])
        lowered = prompt.lower()
        assert "não é uma recomendação" in lowered
        assert "não é exaustiva" in lowered

    def test_off_list_tickers_are_not_forbidden(self):
        # Forbidding them would make the wallet strictly worse whenever the
        # universe is stale or thin; the verification loop already rejects
        # anything the market disowns.
        from app.api.routes.ai_wallet import _phase_a_prompt

        prompt = _phase_a_prompt("ACOES", False, [{"ticker": "TST3"}])
        assert "fora dela for claramente melhor" in prompt

    def test_a_broken_screener_does_not_break_generation(self, db, monkeypatch):
        from app.services import universe as screener

        monkeypatch.setattr(screener.state, "is_enabled", lambda _db: True)
        monkeypatch.setattr(screener, "screen", lambda *a, **k: 1 / 0)
        assert screener.category_screen(db, "ACOES") == []


class TestScreenerCategoryCoverage:
    """Which wallet categories the local universe can actually serve.

    The requirement has to vary by class. An ETF has no company behind it
    filing a balance sheet, so demanding a market capitalisation rejects every
    one of them — and the category then receives no candidates at all, without
    a single error to show for it.
    """

    def _seed(self, db):
        from app.db.models import AssetUniverse

        db.add_all(
            [
                # ETFs: priced and liquid, but no fundamentals, ever.
                *[
                    AssetUniverse(
                        ticker=f"ET{n}11", name=f"ETF {n}", kind="ETF", currency="BRL",
                        market="B3", status="ATIVO", identity_source="teste",
                        price=Decimal(100), avg_volume_21d=Decimal(1_000_000 - n),
                    )
                    for n in range(12)
                ],
                # Ações: fundamentals present, so the stricter bar applies.
                *[
                    AssetUniverse(
                        ticker=f"AC{n}3", name=f"Acao {n}", kind="STOCK", currency="BRL",
                        market="B3", status="ATIVO", identity_source="teste",
                        price=Decimal(20), avg_volume_21d=Decimal(500_000 - n),
                        market_cap=Decimal(9_000_000), pe=Decimal(8), roe_pct=Decimal(15),
                        fundamentals_fetched_at=datetime.now(UTC),
                    )
                    for n in range(12)
                ],
            ]
        )
        db.commit()

    def test_etfs_are_offered_despite_having_no_market_cap(self, db, monkeypatch):
        from app.services import universe as screener

        self._seed(db)
        monkeypatch.setattr(screener.state, "is_enabled", lambda _db: True)
        rows = screener.category_screen(db, "ETF", limit=10)
        assert len(rows) == 10
        assert all(row["ticker"].endswith("11") for row in rows)

    def test_shares_still_require_fundamentals(self, db, monkeypatch):
        """A share with no published figures is not a considered pick."""
        from app.db.models import AssetUniverse
        from app.services import universe as screener

        self._seed(db)
        db.add(
            AssetUniverse(
                ticker="SHELL3", name="Sem dados", kind="STOCK", currency="BRL",
                market="B3", status="ATIVO", identity_source="teste",
                price=Decimal(1), avg_volume_21d=Decimal(99_000_000),  # most liquid of all
            )
        )
        db.commit()
        monkeypatch.setattr(screener.state, "is_enabled", lambda _db: True)
        rows = screener.category_screen(db, "ACOES", limit=10)
        assert "SHELL3" not in {row["ticker"] for row in rows}

    def test_the_staleness_cutoff_does_not_reject_etfs(self, db, monkeypatch):
        # An ETF row never carries a filing date; applying the fundamentals
        # cutoff to it would discard the class for being "stale".
        from app.services import universe as screener

        self._seed(db)
        monkeypatch.setattr(screener.state, "is_enabled", lambda _db: True)
        assert screener.category_screen(db, "ETF", limit=10)


class TestSuggestPromptInjection:
    def test_prompt_is_byte_identical_without_a_universe(self):
        from app.api.routes.ai_wallet import _suggest_prompt

        assert _suggest_prompt("ACOES", False, []) == _suggest_prompt("ACOES", False)
        assert _suggest_prompt("FII", True, None) == _suggest_prompt("FII", True)

    def test_candidates_reach_the_suggestion_prompt(self):
        """Otherwise 'buy_new' can only ever come from the model's memory."""
        from app.api.routes.ai_wallet import _suggest_prompt

        prompt = _suggest_prompt("ACOES", False, [{"ticker": "TST3", "p_l": 7.2}])
        assert "TST3" in prompt
        assert "não é recomendação nem lista exaustiva" in prompt.lower()
