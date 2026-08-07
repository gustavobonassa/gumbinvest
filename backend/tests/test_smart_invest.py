"""Aporte inteligente: category discovery, model context, reply validation."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from app.db.models import Asset, Quote, SmartInvestRun, Transaction
from app.services import smart_invest


def seed_asset(db, ticker, kind="STOCK", currency="BRL", price=None) -> Asset:
    asset = Asset(ticker=ticker, name=ticker, kind=kind, currency=currency)
    db.add(asset)
    db.commit()
    db.refresh(asset)
    if price is not None:
        db.add(Quote(asset_id=asset.id, price=Decimal(price), currency=currency))
        db.commit()
    return asset


def seed_trade(db, portfolio, asset, quantity=10, unit_price=10, day="2024-03-01") -> None:
    amount = Decimal(quantity) * Decimal(unit_price)
    db.add(
        Transaction(
            portfolio_id=portfolio.id,
            asset_id=asset.id,
            trade_date=date.fromisoformat(day),
            direction="CREDIT",
            op_type="BUY",
            effect="ACQUIRE",
            quantity=Decimal(quantity),
            unit_price=Decimal(unit_price),
            gross_amount=amount,
            net_amount=amount,
            currency=asset.currency,
            raw_movement="Compra",
            raw_product=asset.ticker,
            dedup_key=f"test:{asset.ticker}:{day}",
            occurrence=0,
        )
    )
    db.commit()


def test_available_categories_counts_open_positions(db, portfolio):
    stock = seed_asset(db, "AAAA3", price=12)
    fii = seed_asset(db, "BBBB11", kind="FII", price=100)
    seed_asset(db, "ZZZZ3")  # never traded: not a category
    seed_trade(db, portfolio, stock)
    seed_trade(db, portfolio, fii, quantity=2)

    rows = smart_invest.available_categories(db, portfolio.id)

    assert [(row["kind"], row["count"]) for row in rows] == [("FII", 1), ("STOCK", 1)]
    assert rows[0]["label"] == "FIIs"
    assert rows[0]["value"] == 200.0


def test_invest_context_scopes_to_selected_kinds(db, portfolio):
    stock = seed_asset(db, "AAAA3", price=12)
    fii = seed_asset(db, "BBBB11", kind="FII", price=100)
    seed_trade(db, portfolio, stock)
    seed_trade(db, portfolio, fii, quantity=2)

    context, facts = smart_invest.invest_context(db, portfolio.id, ["STOCK"], "BRL")

    assert [item["ticker"] for item in context] == ["AAAA3"]
    item = context[0]
    assert item["categoria"] == "Ações"
    assert item["preco_medio"] == 10.0
    assert item["preco_atual"] == 12.0
    assert item["resultado_nao_realizado_pct"] == 20.0
    # Weight is against the WHOLE portfolio, so the model sees the imbalance
    # between the selected categories and everything else.
    assert item["alocacao_na_carteira_pct"] == 37.5  # 120 of 320
    assert facts["AAAA3"]["price_in_currency"] == Decimal(12)


def test_invest_context_flags_missing_quote(db, portfolio):
    stock = seed_asset(db, "AAAA3")  # no quote
    seed_trade(db, portfolio, stock)

    context, facts = smart_invest.invest_context(db, portfolio.id, ["STOCK"], "BRL")

    assert context[0]["preco_atual"] is None
    assert context[0]["observacao"] == "sem cotação neste momento"
    assert facts["AAAA3"]["price_in_currency"] is None


def test_price_in_never_uses_rate_one():
    assert smart_invest._price_in(Decimal(10), "USD", "BRL", Decimal("5.5")) == Decimal("55.0")
    assert smart_invest._price_in(Decimal(55), "USD", "USD", None) == Decimal(55)
    assert smart_invest._price_in(Decimal(10), "USD", "BRL", None) is None
    assert smart_invest._price_in(None, "BRL", "BRL", Decimal(5)) is None


FACTS = {
    "AAAA3": {
        "name": "AAAA3",
        "kind": "STOCK",
        "label": "Ações",
        "currency": "BRL",
        "price": Decimal(12),
        "price_in_currency": Decimal(12),
    },
    "NKE": {
        "name": "Nike",
        "kind": "STOCK_INTL",
        "label": "Stocks (exterior)",
        "currency": "USD",
        "price": Decimal(100),
        "price_in_currency": Decimal(550),
    },
    "CDB X": {
        "name": "CDB X",
        "kind": "FIXED_INCOME",
        "label": "Renda fixa",
        "currency": "BRL",
        "price": None,
        "price_in_currency": None,
    },
}


def test_normalize_allocations_validates_and_scales():
    data = {
        "allocations": [
            {"ticker": "AAAA3", "amount": 6000, "rationale": "desconto"},
            {"ticker": "MMMM3", "amount": 1000, "rationale": "não é do usuário"},
            {"ticker": "NKE", "amount": 6000, "rationale": "força"},
            {"ticker": "AAAA3", "amount": 99, "rationale": "duplicado"},
            {"ticker": "CDB X", "amount": -5, "rationale": "negativo"},
            "lixo",
        ]
    }
    items, skipped = smart_invest.normalize_allocations(data, FACTS, Decimal(10000))

    assert skipped == ["MMMM3"]
    assert [item["ticker"] for item in items] == ["AAAA3", "NKE"]
    # 12000 suggested for a 10000 aporte: scaled proportionally, never over.
    assert sum(item["amount"] for item in items) <= Decimal(10000)
    assert items[0]["amount"] == Decimal("5000.00")

    assert smart_invest.normalize_allocations(None, FACTS, Decimal(100)) == ([], [])


def test_record_run_survives_the_registry(db, portfolio):
    """The history is the durable copy: Decimals stringified, order newest-first."""
    result = {
        "amount": Decimal("10000.00"),
        "currency": "BRL",
        "categories": ["Ações"],
        "strategy": "concentrar",
        "allocations": [{"ticker": "AAAA3", "amount": Decimal("5000.00"), "rationale": "a"}],
        "leftover": Decimal("5000.00"),
        "skipped": [],
        "used_search": True,
        "provider": "anthropic",
        "provider_label": "Anthropic (Claude)",
        "model": "claude-sonnet-5",
        "generated_at": datetime.now(UTC),
    }
    smart_invest.record_run(db, portfolio.id, result)
    db.commit()

    runs = smart_invest.list_runs(db, portfolio.id)
    assert len(runs) == 1
    assert runs[0]["amount"] == "10000.00"  # JSON-safe: money as strings
    assert runs[0]["allocations"][0]["amount"] == "5000.00"
    assert runs[0]["id"] == db.scalars(select(SmartInvestRun)).first().id


def test_enrich_allocations_prices_quantities():
    items = [
        {"ticker": "AAAA3", "amount": Decimal("5000.00"), "rationale": "a"},
        {"ticker": "NKE", "amount": Decimal("1000.00"), "rationale": "b"},
        {"ticker": "CDB X", "amount": Decimal("2000.00"), "rationale": "c"},
    ]
    out = smart_invest.enrich_allocations(items, FACTS)

    b3, us, fi = out
    assert b3["approx_quantity"] == Decimal(416)  # whole shares, floor
    assert us["approx_quantity"] == Decimal("1.8181")  # 4 dp on US listings
    assert fi["approx_quantity"] is None  # renda fixa is bought in value
    assert us["current_price"] == Decimal(100)
    assert us["price_currency"] == "USD"
