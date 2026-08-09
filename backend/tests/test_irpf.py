"""The IRPF worksheet: what the form asks for, and what the ledger cannot answer."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import current_portfolio
from app.db.models import Asset, AssetUniverse
from app.db.session import get_db
from app.main import app
from app.portfolio import irpf
from app.portfolio.service import PortfolioService
from app.services.portfolio_registry import get_default_portfolio


@pytest.fixture
def client(engine, db: Session):
    """A TestClient bound to the test database and a seeded portfolio."""
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

HEADER = "Entrada/Saída,Data,Movimentação,Produto,Instituição,Quantidade,Preço unitário,Valor da Operação\n"
XP = "XP INVESTIMENTOS CCTVM S/A"
#: Two years so the declaration has both columns to compare, one sale inside the
#: exemption, a dividend, a JCP and a FII yield — one of each pot the form keeps
#: apart.
CSV = HEADER + (
    f'Credito,10/03/2024,Transferência - Liquidação,PETR4 - PETROLEO BRASILEIRO S.A.,{XP},100," R$ 30,00 "," R$ 3.000,00 "\n'
    f'Credito,05/02/2025,Transferência - Liquidação,PETR4 - PETROLEO BRASILEIRO S.A.,{XP},100," R$ 40,00 "," R$ 4.000,00 "\n'
    f'Credito,20/06/2025,Dividendo,PETR4 - PETROLEO BRASILEIRO S.A.,{XP},200," R$ 1,00 "," R$ 200,00 "\n'
    f'Credito,20/07/2025,Juros Sobre Capital Próprio,PETR4 - PETROLEO BRASILEIRO S.A.,{XP},200," R$ 0,50 "," R$ 100,00 "\n'
    f'Credito,11/03/2024,Transferência - Liquidação,MXRF11 - MAXI RENDA FDO INV IMOB,{XP},100," R$ 10,00 "," R$ 1.000,00 "\n'
    f'Credito,15/08/2025,Rendimento,MXRF11 - MAXI RENDA FDO INV IMOB,{XP},100," R$ 0,90 "," R$ 90,00 "\n'
    f'Debito,10/09/2025,Transferência - Liquidação,PETR4 - PETROLEO BRASILEIRO S.A.,{XP},50," R$ 50,00 "," R$ 2.500,00 "\n'
)


@pytest.fixture
def loaded(client: TestClient, db: Session):
    """The fixture history, imported, with a service over it."""
    client.post("/api/imports", files={"file": ("movimentacao.csv", CSV.encode("utf-8"), "text/csv")})
    portfolio = get_default_portfolio(db)
    return PortfolioService(db, portfolio.id)


def bem(report: dict, ticker: str) -> dict:
    return next(row for row in report["bens"] if row["ticker"] == ticker)


def test_bens_are_declared_at_cost_with_last_years_column(loaded):
    """*Bens e Direitos* wants what was paid on two dates, not what it is worth."""
    report = irpf.worksheet(loaded, 2025)

    petr = bem(report, "PETR4")
    # 100 at 30 in 2024; 100 more at 40 in 2025; 50 sold, which removes cost in
    # proportion (avg 35) and leaves 150 at 35.
    assert petr["cost_previous"] == Decimal(3000)
    assert petr["cost"] == pytest.approx(Decimal("5250"))
    assert petr["quantity"] == Decimal(150)
    # Market value never enters it: the closes are absent here and the figure is
    # unaffected, which is exactly the property the declaration needs.
    assert petr["grupo"], "every line carries a grupo/código"


def test_a_position_opened_this_year_shows_a_zero_opening(loaded, db: Session):
    """The form compares two columns; a line that appears from nowhere is a flag."""
    report = irpf.worksheet(loaded, 2024)
    petr = bem(report, "PETR4")
    assert petr["cost_previous"] == Decimal(0)
    assert petr["cost"] == Decimal(3000)


def test_income_lands_in_the_pot_the_form_keeps_it_in(loaded):
    """Dividends and FII yields are exempt; JCP was taxed at source."""
    report = irpf.worksheet(loaded, 2025)

    isentos = {row["ticker"]: row for row in report["isentos"]}
    assert isentos["PETR4"]["gross"] == Decimal(200)  # dividendo
    assert isentos["MXRF11"]["gross"] == Decimal(90)  # rendimento de FII

    exclusiva = {row["ticker"]: row for row in report["exclusiva"]}
    assert exclusiva["PETR4"]["op_type"] == "JCP"
    assert exclusiva["PETR4"]["gross"] == Decimal(100)
    # JCP never appears among the exempt: that is the whole distinction.
    assert "JCP" not in {row["op_type"] for row in report["isentos"]}


def test_the_cnpj_comes_from_the_registry_and_can_be_overridden(loaded, db: Session):
    """Most payers are already known; the rest have to be answerable by hand."""
    db.add(AssetUniverse(ticker="PETR4", cnpj="33000167000101"))
    db.commit()
    loaded._assets = None  # the service caches assets per request

    report = irpf.worksheet(loaded, 2025)
    assert bem(report, "PETR4")["cnpj"] == "33.000.167/0001-01"
    # MXRF11 is in no registry here, so it is named as a gap rather than left
    # blank in silence.
    assert bem(report, "MXRF11")["cnpj"] is None
    assert any(gap["kind"] == "cnpj" and "MXRF11" in gap["tickers"] for gap in report["gaps"])

    asset = db.scalar(select(Asset).where(Asset.ticker == "MXRF11"))
    asset.cnpj = "97521225000125"
    db.commit()
    loaded._assets = None
    report = irpf.worksheet(loaded, 2025)
    assert bem(report, "MXRF11")["cnpj"] == "97.521.225/0001-25"
    assert not any(gap["kind"] == "cnpj" and "MXRF11" in gap["tickers"] for gap in report["gaps"])


def test_sales_are_reported_per_month_without_deciding_the_exemption(loaded):
    """The R$ 20.000 test is shown, not applied — it is the taxpayer's to make."""
    report = irpf.worksheet(loaded, 2025)
    sales = report["sales"]

    september = next(month for month in sales["months"] if month["period"] == "2025-09")
    assert september["acoes"] == Decimal(2500)
    assert sales["exemption_limit"] == Decimal(20_000)
    # 50 shares at 50, against an average of 35.
    assert sales["result_by_bucket"]["acoes"] == pytest.approx(Decimal(750))


def test_a_coin_quoted_in_dollars_is_not_a_bem_no_exterior():
    """Quote currency is not a location, and the two rules are different.

    Everything crypto trades against the dollar, so a currency test called every
    coin a foreign asset: the discriminação asserted "ativo no exterior" about a
    wallet nothing here can locate, and the disposal went into the exterior
    bucket — which applied the wrong exemption, since crypto has a threshold of
    its own.
    """
    coin = Asset(ticker="BTC", kind="CRYPTO", currency="USD")
    stable = Asset(ticker="USDT", kind="STABLECOIN", currency="USD")
    share = Asset(ticker="NKE", kind="STOCK_INTL", currency="USD")

    assert irpf._is_foreign(coin, "BRL") is False
    assert irpf._is_foreign(stable, "BRL") is False
    assert irpf._is_foreign(share, "BRL") is True

    assert irpf._sale_bucket(coin, "BRL") == "cripto"
    assert irpf._sale_bucket(stable, "BRL") == "cripto"
    assert irpf._sale_bucket(share, "BRL") == "exterior"

    # And the line it writes admits what it does not know instead of inventing.
    line = irpf._discriminacao(coin, irpf._Held(Decimal("0.5"), Decimal(100)), None, False)
    assert "exterior" not in line
    assert "informe a corretora" in line


def test_the_year_in_progress_is_never_offered(loaded, monkeypatch):
    """A declaration is of a closed year, and half a year reads as a whole one."""
    monkeypatch.setattr(irpf, "local_today", lambda: date(2026, 8, 9))
    assert irpf.available_years(loaded)[0] == 2025


def test_the_codigo_table_is_data_and_falls_back_visibly(loaded):
    """A class with no entry gets 99-99, not a plausible-looking wrong code."""
    assert irpf.codes_for(2025)["FII"] == ("07", "03")
    # A year before any table still resolves, to the oldest one on file.
    assert irpf.codes_for(1999) == irpf.codes_for(min(irpf.BENS_CODES))
    assert irpf.FALLBACK_CODE == ("99", "99")


def test_the_endpoint_defaults_to_the_last_closed_year(client: TestClient, loaded):
    response = client.get("/api/reports/irpf")
    assert response.status_code == 200
    payload = response.json()
    assert payload["year"] == payload["years"][0]
    assert payload["bens"], "the worksheet has lines"
