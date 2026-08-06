"""Watch-only assets: papers the portfolio never traded but wants to follow.

Search finds them on the market, the detail endpoint creates them on first
view, and everything position-shaped comes back zeroed with ``held: False``.
Network is always mocked — these tests must pass offline.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import current_portfolio
from app.db.models import Asset, Transaction
from app.db.session import get_db
from app.main import app
from app.market import lookup
from app.market.lookup import MarketHit, _to_hit
from app.services.portfolio_registry import get_default_portfolio

PETR4_HIT = MarketHit(
    ticker="PETR4",
    name="Petróleo Brasileiro S.A. - Petrobras",
    kind="STOCK",
    currency="BRL",
    market_symbol="PETR4.SA",
    exchange="São Paulo",
)


@pytest.fixture
def client(engine, db: Session):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    portfolio = get_default_portfolio(db)

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_portfolio] = lambda: portfolio
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def offline_market(monkeypatch):
    """Market data bootstrap must not leave the machine during tests."""
    monkeypatch.setattr("app.api.routes.assets.ensure_market_data", lambda db, asset: None)


# ---------------------------------------------------------------------------
# Lookup mapping (pure, no network)
# ---------------------------------------------------------------------------
def test_lookup_maps_b3_equity():
    hit = _to_hit(
        {
            "symbol": "PETR4.SA",
            "longname": "Petróleo Brasileiro S.A. - Petrobras",
            "quoteType": "EQUITY",
            "exchDisp": "São Paulo",
        }
    )
    assert hit == MarketHit(
        ticker="PETR4",
        name="Petróleo Brasileiro S.A. - Petrobras",
        kind="STOCK",
        currency="BRL",
        market_symbol="PETR4.SA",
        exchange="São Paulo",
    )


def test_lookup_maps_b3_fii_by_name():
    hit = _to_hit(
        {"symbol": "HGLG11.SA", "longname": "CSHG Logística Fundo Imob - FII", "quoteType": "EQUITY"}
    )
    assert hit is not None
    assert hit.kind == "FII"


def test_lookup_maps_us_symbols_to_the_offshore_families():
    """A US listing is STOCK_INTL / ETF_INTL, never the domestic family.

    The two are kept apart everywhere else in the app — different tab, different
    colour, different bucket in the allocation — because they are not comparable
    on currency or on tax treatment. A watch-only asset minted from a search
    that answered ``STOCK`` would sit among the B3 shares for good: nothing
    reclassifies it later, since ``reclassify_assets`` reads the product text of
    a transaction and a watched asset has none.
    """
    equity = _to_hit({"symbol": "AAPL", "shortname": "Apple Inc.", "quoteType": "EQUITY"})
    fund = _to_hit({"symbol": "VOO", "shortname": "Vanguard S&P 500 ETF", "quoteType": "ETF"})
    reit = _to_hit({"symbol": "O", "shortname": "Realty Income Corporation", "quoteType": "EQUITY"})
    assert equity is not None and equity.currency == "USD" and equity.kind == "STOCK_INTL"
    assert fund is not None and fund.kind == "ETF_INTL"
    # The importer's REIT list comes along for free by reusing its classifier.
    assert reit is not None and reit.kind == "REIT"


def test_lookup_rejects_unsupported_results():
    # Crypto pairs, currencies and non-US foreign listings all come back from
    # the same search; none may become an asset row.
    assert _to_hit({"symbol": "BTC-USD", "quoteType": "CRYPTOCURRENCY"}) is None
    assert _to_hit({"symbol": "VOD.L", "quoteType": "EQUITY"}) is None
    assert _to_hit({"symbol": "EURUSD=X", "quoteType": "CURRENCY"}) is None


# ---------------------------------------------------------------------------
# Detail endpoint: creation, shape, idempotency
# ---------------------------------------------------------------------------
def test_unknown_ticker_becomes_watch_only_asset(client: TestClient, db, offline_market, monkeypatch):
    monkeypatch.setattr(lookup, "resolve", lambda ticker: PETR4_HIT)

    response = client.get("/api/assets/PETR4")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["held"] is False
    assert body["ticker"] == "PETR4"
    assert body["kind"] == "STOCK"
    assert Decimal(str(body["quantity"])) == 0
    assert body["transactions"] == []
    assert body["dividends"] == []

    created = db.scalar(select(Asset).where(Asset.ticker == "PETR4"))
    assert created is not None
    assert created.market_symbol == "PETR4.SA"
    assert created.currency == "BRL"

    # Second view reuses the row: still exactly one PETR4.
    assert client.get("/api/assets/PETR4").status_code == 200
    assert len(db.scalars(select(Asset).where(Asset.ticker == "PETR4")).all()) == 1


def test_typoed_ticker_stays_a_404(client: TestClient, db, offline_market, monkeypatch):
    monkeypatch.setattr(lookup, "resolve", lambda ticker: None)
    response = client.get("/api/assets/XXXX99")
    assert response.status_code == 404
    assert db.scalar(select(Asset).where(Asset.ticker == "XXXX99")) is None


def test_existing_row_without_transactions_is_watch_only(client: TestClient, db, offline_market):
    db.add(Asset(ticker="VALE3", name="Vale S.A.", kind="STOCK", currency="BRL"))
    db.commit()
    body = client.get("/api/assets/VALE3").json()
    assert body["held"] is False
    assert body["name"] == "Vale S.A."


def test_held_asset_reports_held_true(client: TestClient, db):
    header = "Entrada/Saída,Data,Movimentação,Produto,Instituição,Quantidade,Preço unitário,Valor da Operação\n"
    row = 'Credito,10/01/2024,Transferência - Liquidação,PETR4 - PETROLEO BRASILEIRO S.A.,XP INVESTIMENTOS CCTVM S/A,100," R$ 30,00 "," R$ 3.000,00 "\n'
    upload = client.post(
        "/api/imports", files={"file": ("movimentacao.csv", (header + row).encode(), "text/csv")}
    )
    assert upload.status_code == 200, upload.text

    body = client.get("/api/assets/PETR4").json()
    assert body["held"] is True
    assert Decimal(str(body["quantity"])) == 100


# ---------------------------------------------------------------------------
# Market search endpoint
# ---------------------------------------------------------------------------
def test_market_search_excludes_local_assets(client: TestClient, db, monkeypatch):
    db.add(Asset(ticker="PETR4", name="Petrobras", kind="STOCK", currency="BRL"))
    db.commit()
    vale = MarketHit(
        ticker="VALE3", name="Vale S.A.", kind="STOCK", currency="BRL",
        market_symbol="VALE3.SA", exchange="São Paulo",
    )
    monkeypatch.setattr(lookup, "search_market", lambda q, limit=8: [PETR4_HIT, vale])

    body = client.get("/api/search/market", params={"q": "petr"}).json()
    tickers = [item["ticker"] for item in body["items"]]
    assert tickers == ["VALE3"]  # PETR4 already has a local row


# ---------------------------------------------------------------------------
# Quotes and AI context
# ---------------------------------------------------------------------------
def test_only_assets_someone_asked_for_are_refreshed(db, portfolio):
    """The scheduled refresh follows deliberate acts, not page views.

    Browsing a ticker — from search, or from one of the thousands of rows in
    the asset universe — used to mint a row with no transactions, which the old
    rule then refreshed every half hour forever. Enrolment in a recurring job
    is now something you opt into by holding the paper, watchlisting it, or
    letting an AI wallet buy it. A merely-browsed asset is refreshed when its
    page is opened instead (``refresh_if_stale``).
    """
    from datetime import date

    from app.db.models import WatchlistItem
    from app.market.service import quotable_assets

    browsed = Asset(ticker="VALE3", name="Vale", kind="STOCK", currency="BRL")
    sold = Asset(ticker="ITSA4", name="Itaúsa", kind="STOCK", currency="BRL")
    listed = Asset(ticker="WEGE3", name="WEG", kind="STOCK", currency="BRL")
    db.add_all([browsed, sold, listed])
    db.commit()
    # WEGE3 is on the watchlist — an explicit "keep this current".
    db.add(WatchlistItem(ticker="WEGE3"))
    db.commit()
    # ITSA4 was bought and fully sold: rows exist, position is closed.
    common = dict(
        portfolio_id=portfolio.id, asset_id=sold.id, broker_id=None, import_batch_id=None,
        unit_price=Decimal(10), gross_amount=Decimal(100), fees=Decimal(0), taxes=Decimal(0),
        net_amount=Decimal(100), currency="BRL", fx_rate=None, raw_movement="t",
        raw_product="p", raw_institution="i", source_line=None, occurrence=0,
    )
    db.add_all(
        [
            Transaction(
                trade_date=date(2024, 1, 2), direction="CREDIT", op_type="BUY",
                effect="ACQUIRE", quantity=Decimal(10), dedup_key="k1", **common,
            ),
            Transaction(
                trade_date=date(2024, 2, 2), direction="DEBIT", op_type="SELL",
                effect="DISPOSE", quantity=Decimal(10), dedup_key="k2", **common,
            ),
        ]
    )
    db.commit()

    tickers = {a.ticker for a in quotable_assets(db, portfolio.id)}
    assert "WEGE3" in tickers  # watchlisted: deliberately tracked
    assert "VALE3" not in tickers  # merely browsed: refreshed on sight, not on a timer
    assert "ITSA4" not in tickers  # sold out: excluded as before
    # ...and nothing changed for the caller that wants every asset.
    assert "VALE3" in {a.ticker for a in quotable_assets(db, portfolio.id, only_held=False)}


def test_ai_context_says_the_user_owns_none(db, portfolio):
    import json

    from app.api.routes.ai import _asset_context
    from app.portfolio.service import PortfolioService

    db.add(Asset(ticker="VALE3", name="Vale", kind="STOCK", currency="BRL"))
    db.commit()
    context = json.loads(_asset_context(db, PortfolioService(db, portfolio.id), "VALE3"))
    assert context["posicao_do_usuario"]["possui_o_ativo"] is False


# ---------------------------------------------------------------------------
# Browsing must not enrol an asset in a recurring job
# ---------------------------------------------------------------------------
def test_browsing_a_universe_ticker_does_not_add_it_to_the_refresh_set(db, portfolio, monkeypatch):
    """Opening an asset page is looking, not subscribing.

    With the asset universe listing thousands of clickable tickers, the old
    rule would have grown the every-30-minutes refresh set by one paper per
    click, forever, without anyone choosing it.
    """
    from app.db.models import AssetUniverse
    from app.market.service import quotable_assets

    db.add(
        AssetUniverse(
            ticker="TST3", name="Teste S.A.", kind="STOCK", currency="BRL",
            market="B3", market_symbol="TST3.SA", identity_source="teste",
        )
    )
    db.commit()

    from app.api.routes import assets as routes

    monkeypatch.setattr(routes, "ensure_market_data", lambda *a, **k: None)
    asset = routes._create_watch_only(db, "TST3")

    assert asset.id is not None  # the page has a row to hang quotes off
    assert "TST3" not in {a.ticker for a in quotable_assets(db, portfolio.id)}


def test_a_universe_ticker_resolves_without_asking_the_market(db, monkeypatch):
    """The universe was downloaded so questions like this need no provider."""
    from app.db.models import AssetUniverse

    db.add(
        AssetUniverse(
            ticker="TST3", name="Teste S.A.", kind="STOCK", currency="BRL",
            market="B3", market_symbol="TST3.SA", sector="Energia", identity_source="teste",
        )
    )
    db.commit()

    from app.api.routes import assets as routes

    def refuse(*_args, **_kwargs):
        raise AssertionError("the market must not be searched for a ticker we already know")

    monkeypatch.setattr(routes.lookup, "resolve", refuse)
    monkeypatch.setattr(routes, "ensure_market_data", lambda *a, **k: None)
    asset = routes._create_watch_only(db, "TST3")
    assert asset.name == "Teste S.A."
    assert asset.sector == "Energia"


def test_an_untracked_asset_is_refreshed_when_its_page_opens(db, portfolio, monkeypatch):
    from app.market import service

    asset = Asset(ticker="TST3", name="Teste", kind="STOCK", currency="BRL")
    db.add(asset)
    db.commit()

    calls: list[str] = []
    monkeypatch.setattr(service, "ensure_market_data", lambda _db, a: calls.append(a.ticker))
    assert service.refresh_if_stale(db, asset, portfolio.id) is True
    assert calls == ["TST3"]


def test_a_tracked_asset_is_left_to_the_schedule(db, portfolio, monkeypatch):
    """No second fetch per page view — the scheduled job already owns it."""
    from app.db.models import WatchlistItem
    from app.market import service

    asset = Asset(ticker="TST3", name="Teste", kind="STOCK", currency="BRL")
    db.add(asset)
    db.commit()
    db.add(WatchlistItem(ticker="TST3"))
    db.commit()

    monkeypatch.setattr(
        service, "ensure_market_data", lambda *a: (_ for _ in ()).throw(AssertionError("refetched"))
    )
    assert service.refresh_if_stale(db, asset, portfolio.id) is False


def test_a_fresh_quote_is_not_refetched(db, portfolio, monkeypatch):
    from datetime import UTC, datetime

    from app.db.models import Quote
    from app.market import service

    asset = Asset(ticker="TST3", name="Teste", kind="STOCK", currency="BRL")
    db.add(asset)
    db.commit()
    db.add(
        Quote(
            asset_id=asset.id, price=Decimal(10), currency="BRL",
            source="teste", fetched_at=datetime.now(UTC),
        )
    )
    db.commit()

    monkeypatch.setattr(
        service, "ensure_market_data", lambda *a: (_ for _ in ()).throw(AssertionError("refetched"))
    )
    assert service.refresh_if_stale(db, asset, portfolio.id) is False


def test_a_failing_refresh_never_breaks_the_page(db, portfolio, monkeypatch):
    from app.market import service

    asset = Asset(ticker="TST3", name="Teste", kind="STOCK", currency="BRL")
    db.add(asset)
    db.commit()
    monkeypatch.setattr(
        service, "ensure_market_data", lambda *a: (_ for _ in ()).throw(RuntimeError("provider down"))
    )
    assert service.refresh_if_stale(db, asset, portfolio.id) is False
