"""Portfolio-wide AI chat: context builder, nullable ticker, request shape."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from app.api.routes.ai import ChatRequest, _portfolio_context
from app.db.models import AiChat, Asset, Transaction
from app.portfolio.service import PortfolioService


def _buy(portfolio, asset, quantity: int, price: int, day: date, key: str) -> Transaction:
    return Transaction(
        portfolio_id=portfolio.id, asset_id=asset.id, broker_id=None, import_batch_id=None,
        trade_date=day, direction="CREDIT", op_type="BUY", effect="ACQUIRE",
        quantity=Decimal(quantity), unit_price=Decimal(price),
        gross_amount=Decimal(quantity * price), fees=Decimal(0), taxes=Decimal(0),
        net_amount=Decimal(quantity * price), currency="BRL", fx_rate=None,
        raw_movement="Compra", raw_product="p", raw_institution="i", source_line=None,
        dedup_key=key, occurrence=0,
    )


def test_portfolio_context_carries_totals_and_positions(db, portfolio):
    petr = Asset(ticker="PETR4", name="Petrobras", kind="STOCK", currency="BRL")
    hglg = Asset(ticker="HGLG11", name="CSHG Log", kind="FII", currency="BRL")
    db.add_all([petr, hglg])
    db.commit()
    db.add_all(
        [
            _buy(portfolio, petr, 100, 30, date(2024, 1, 10), "k1"),
            _buy(portfolio, hglg, 50, 160, date(2024, 2, 10), "k2"),
        ]
    )
    db.commit()

    context = json.loads(_portfolio_context(PortfolioService(db, portfolio.id)))
    assert "resumo_da_carteira" in context
    assert context["resumo_da_carteira"]["positions_count"] == 2

    tickers = [p["ticker"] for p in context["posicoes_abertas"]]
    assert set(tickers) == {"PETR4", "HGLG11"}
    # Sorted by size: HGLG11 (R$ 8.000) ahead of PETR4 (R$ 3.000) at cost.
    assert tickers[0] == "HGLG11"
    for position in context["posicoes_abertas"]:
        assert set(position) <= {
            "ticker", "kind", "currency", "quantity", "average_price", "current_price",
            "market_value_base", "allocation_pct", "unrealized_pct", "total_return",
            "income", "day_change_pct",
        }
        # The compact view must not leak the full ledger.
        assert "transactions" not in position

    kinds = context["alocacao_por_classe_pct"]
    assert set(kinds) == {"STOCK", "FII"}
    assert abs(sum(kinds.values()) - 100) < 0.01


def test_chat_request_accepts_missing_ticker():
    payload = ChatRequest(messages=[{"role": "user", "content": "Como está minha carteira?"}])
    assert payload.ticker is None
    scoped = ChatRequest(ticker="PETR4", messages=[{"role": "user", "content": "oi"}])
    assert scoped.ticker == "PETR4"


def test_chat_rows_persist_without_ticker(db, portfolio):
    db.add(AiChat(portfolio_id=portfolio.id, ticker=None, title="Sobre a carteira", messages=[]))
    db.commit()
    saved = db.query(AiChat).filter(AiChat.ticker.is_(None)).one()
    assert saved.title == "Sobre a carteira"
