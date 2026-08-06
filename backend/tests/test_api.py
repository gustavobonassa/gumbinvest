"""Integration tests that exercise the HTTP layer end to end.

These run the real FastAPI app against the test database, so serialisation
bugs (a response that cannot be encoded, a Decimal that reaches the client as a
string) fail here rather than in the browser.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import current_portfolio
from app.db.session import get_db
from app.main import app
from app.services.portfolio_registry import get_default_portfolio

HEADER = "Entrada/Saída,Data,Movimentação,Produto,Instituição,Quantidade,Preço unitário,Valor da Operação\n"
CSV = HEADER + (
    'Credito,10/01/2024,Transferência - Liquidação,PETR4 - PETROLEO BRASILEIRO S.A.,XP INVESTIMENTOS CCTVM S/A,100," R$ 30,00 "," R$ 3.000,00 "\n'
    'Credito,12/02/2024,Transferência - Liquidação,PETR4 - PETROLEO BRASILEIRO S.A.,XP INVESTIMENTOS CCTVM S/A,100," R$ 40,00 "," R$ 4.000,00 "\n'
    'Credito,15/02/2024,Dividendo,PETR4 - PETROLEO BRASILEIRO S.A.,XP INVESTIMENTOS CCTVM S/A,200," R$ 1,00 "," R$ 200,00 "\n'
    'Debito,20/03/2024,Transferência - Liquidação,PETR4 - PETROLEO BRASILEIRO S.A.,XP INVESTIMENTOS CCTVM S/A,50," R$ 50,00 "," R$ 2.500,00 "\n'
)


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
    # The lifespan runs the auto-import; skip it so tests control the data.
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def upload(client: TestClient, payload: str = CSV, name: str = "movimentacao.csv"):
    return client.post("/api/imports", files={"file": (name, payload.encode("utf-8"), "text/csv")})


def test_health(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_upload_returns_a_serialisable_summary(client: TestClient):
    response = upload(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows_total"] == 4
    assert body["rows_imported"] == 4
    assert body["rows_failed"] == 0
    assert body["summary"]["operations"] == {"BUY": 2, "DIVIDEND": 1, "SELL": 1}


def _xlsx_of_the_sample() -> bytes:
    """The same four movements as ``CSV``, as B3's spreadsheet download.

    Typed like the real file: real dates, real numbers — not text that happens
    to look like them.
    """
    openpyxl = pytest.importorskip("openpyxl")
    from datetime import date
    from io import BytesIO

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(
        ["Entrada/Saída", "Data", "Movimentação", "Produto", "Instituição",
         "Quantidade", "Preço unitário", "Valor da Operação"]
    )
    product, broker = "PETR4 - PETROLEO BRASILEIRO S.A.", "XP INVESTIMENTOS CCTVM S/A"
    for row in (
        ["Credito", date(2024, 1, 10), "Transferência - Liquidação", product, broker, 100, 30.00, 3000.00],
        ["Credito", date(2024, 2, 12), "Transferência - Liquidação", product, broker, 100, 40.00, 4000.00],
        ["Credito", date(2024, 2, 15), "Dividendo", product, broker, 200, 1.00, 200.00],
        ["Debito", date(2024, 3, 20), "Transferência - Liquidação", product, broker, 50, 50.00, 2500.00],
    ):
        sheet.append(row)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_the_b3_export_uploads_as_a_spreadsheet_too(client: TestClient):
    """B3 offers .csv and .xlsx of the same report; both must import."""
    response = client.post(
        "/api/imports",
        files={
            "file": (
                "movimentacao.xlsx",
                _xlsx_of_the_sample(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows_imported"] == 4
    assert body["summary"]["operations"] == {"BUY": 2, "DIVIDEND": 1, "SELL": 1}

    positions = client.get("/api/portfolio/positions").json()
    assert positions[0]["quantity"] == pytest.approx(150.0)
    assert positions[0]["average_price"] == pytest.approx(35.0)


def test_the_same_history_in_the_other_format_adds_nothing(client: TestClient):
    """Downloading the report again as a spreadsheet must not double it.

    The de-duplication key is built from the movement's own fields, not from
    the file, so the format it arrived in is irrelevant — which is the whole
    point of accepting both.
    """
    upload(client)
    second = client.post(
        "/api/imports",
        files={"file": ("movimentacao.xlsx", _xlsx_of_the_sample(), "application/octet-stream")},
    ).json()

    assert second["rows_imported"] == 0
    assert second["rows_duplicate"] == 4
    assert client.get("/api/transactions").json()["total"] == 4


def test_reupload_through_the_api_is_idempotent(client: TestClient):
    upload(client)
    second = upload(client).json()
    assert second["rows_imported"] == 0
    assert second["rows_duplicate"] == 4
    assert client.get("/api/transactions").json()["total"] == 4


def test_overview_returns_numbers_not_strings(client: TestClient):
    upload(client)
    body = client.get("/api/portfolio/overview").json()
    # 200 bought for 7.000 (average 35), 50 sold at 50 -> realised 750, cost 5.250
    assert isinstance(body["market_value"], (int, float))
    assert body["cost_basis"] == pytest.approx(5250.0)
    assert body["realized_pnl"] == pytest.approx(750.0)
    assert body["income_total"] == pytest.approx(200.0)


def test_reconciling_a_balance_corrects_the_position_without_editing_history(client: TestClient):
    """State the balance the venue shows; the difference becomes a movement.

    Some of a position cannot be derived from any export — interest that
    compounds inside a staking product is paid into the balance without being
    itemised — so there has to be a way to say what the real number is. Positions
    stay derived: the correction is appended, never written over the top.
    """
    upload(client)
    before = client.get("/api/portfolio/positions").json()[0]
    assert before["quantity"] == pytest.approx(150.0)

    response = client.post("/api/assets/PETR4/reconcile", json={"quantity": 160})
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    assert float(response.json()["difference"]) == pytest.approx(10.0)

    after = client.get("/api/portfolio/positions").json()[0]
    assert after["quantity"] == pytest.approx(160.0)
    # The extra units arrived free, so the cost basis is untouched and the
    # average price dilutes — exactly how a bonus share is treated.
    assert after["cost_basis"] == pytest.approx(before["cost_basis"])

    detail = client.get("/api/assets/PETR4").json()
    assert detail["transactions_count"] == 5
    assert any(t["movement"] == "Balance adjustment" for t in detail["transactions"])

    # Saying the same thing twice changes nothing.
    again = client.post("/api/assets/PETR4/reconcile", json={"quantity": 160})
    assert again.json()["applied"] is False
    assert client.get("/api/portfolio/positions").json()[0]["quantity"] == pytest.approx(160.0)


def test_reconciling_downwards_removes_quantity_at_cost(client: TestClient):
    upload(client)
    client.post("/api/assets/PETR4/reconcile", json={"quantity": 100})
    position = client.get("/api/portfolio/positions").json()[0]
    assert position["quantity"] == pytest.approx(100.0)
    # Removed proportionally, so the average price is unchanged.
    assert position["average_price"] == pytest.approx(35.0)


def test_positions_and_asset_detail(client: TestClient):
    upload(client)
    positions = client.get("/api/portfolio/positions").json()
    assert [p["ticker"] for p in positions] == ["PETR4"]
    assert positions[0]["quantity"] == pytest.approx(150.0)
    assert positions[0]["average_price"] == pytest.approx(35.0)

    detail = client.get("/api/assets/PETR4").json()
    # `transactions` is the ledger; the count travels separately, otherwise the
    # UI renders the array where a number belongs.
    assert isinstance(detail["transactions"], list)
    assert len(detail["transactions"]) == 4
    assert detail["transactions_count"] == 4
    assert len(detail["dividends"]) == 1
    # No withholding on this dividend, so gross and net agree.
    assert detail["income_months"] == [
        {"period": "2024-02", "gross": 200.0, "tax": 0.0, "net": 200.0, "payments": 1}
    ]
    assert detail["income_tax"] == 0.0

    assert client.get("/api/assets/NOPE9").status_code == 404


def test_asset_income_by_month_is_net_of_withholding():
    """The asset page and the Proventos page must agree on a given month.

    They did not: the asset chart plotted gross dividends only, so a month that
    was *entirely* a withholding refund — CIO's April 2026 is three of them and
    no dividend at all — showed nothing while the Proventos page showed the
    refund. A refund raises the net; it is tax coming back.
    """
    from datetime import date
    from decimal import Decimal

    from app.api.routes.assets import _income_by_month
    from app.db.models import Transaction

    def row(day: str, op_type: str, net: str) -> Transaction:
        return Transaction(trade_date=date.fromisoformat(day), op_type=op_type, net_amount=Decimal(net))

    months = _income_by_month(
        [
            row("2025-04-24", "DIVIDEND", "43.61"),
            row("2025-04-24", "TAX", "-13.08"),  # withheld: cash left
            row("2025-04-04", "TAX", "8.43"),  # refunded: cash came back
            row("2026-04-06", "TAX", "13.08"),
            # Brokerage on a purchase. Not a deduction from any dividend — and
            # counting it turned VOOG's January, which held two US$2.50 buys and
            # no dividend at all, into a dividend of −US$5.00.
            row("2025-04-13", "FEE", "-2.50"),
            row("2027-01-13", "FEE", "-2.50"),
        ]
    )

    by_period = {m["period"]: m for m in months}
    assert by_period["2025-04"]["gross"] == Decimal("43.61")
    assert by_period["2025-04"]["tax"] == Decimal("4.65")  # 13.08 withheld − 8.43 back
    assert by_period["2025-04"]["net"] == Decimal("38.96")

    # A month with no dividend still pays, and the net is positive.
    assert by_period["2026-04"]["gross"] == Decimal(0)
    assert by_period["2026-04"]["net"] == Decimal("13.08")
    assert by_period["2026-04"]["payments"] == 0

    # A month of nothing but brokerage does not appear at all, and the month
    # that had both keeps the commission out of its tax.
    assert "2027-01" not in by_period
    assert [m["period"] for m in months] == ["2025-04", "2026-04"]


def test_profit_history_splits_the_result_by_class(client: TestClient):
    """The rentabilidade curve, and the class segments that must add up to it."""
    upload(client)
    total = client.get("/api/portfolio/profit-history", params={"range": "max"}).json()
    assert total, "the curve exists as soon as there is a movement"
    last = total[-1]
    # No quotes in the test database, so the position is marked at its own
    # average price: nothing unrealised, and the result is what was realised
    # plus the dividend received.
    assert last["unrealized"] == pytest.approx(0.0)
    assert last["realized"] == pytest.approx(750.0)
    assert last["income"] == pytest.approx(200.0)
    assert last["profit"] == pytest.approx(950.0)
    assert last["kinds"] == {}  # not requested

    by_kind = client.get(
        "/api/portfolio/profit-history", params={"range": "max", "group_by": "kind"}
    ).json()
    kinds = by_kind[-1]["kinds"]
    assert kinds["STOCK"] == pytest.approx(950.0)
    assert sum(kinds.values()) == pytest.approx(by_kind[-1]["profit"])


def seed_closes(db: Session) -> None:
    """Closes that agree with the fixture's trade prices: 30, then 40, then 50."""
    from app.db.models import Asset, PriceHistory

    asset_id = db.scalar(select(Asset.id).where(Asset.ticker == "PETR4"))
    for day, close in (("2024-01-10", 30), ("2024-02-12", 40), ("2024-03-20", 50)):
        db.add(PriceHistory(asset_id=asset_id, date=date.fromisoformat(day), close=Decimal(close)))
    db.commit()


def test_profit_history_time_weighted_return_is_price_movement_only(client: TestClient, db: Session):
    """`twr_pct` is a return: only what the holding earned counts.

    * **12/02**: 100 shares held from January go 30 → 40. 1.000 on a 3.000
      base → **+33,33 %**. The 100 shares *bought* that day are in neither the
      gain nor the base; a purchase is not performance.
    * **15/02**: a 200 dividend on 200 shares worth 8.000 → **+2,5 %**.
    * **20/03**: 200 shares go 40 → 50. 2.000 on 8.000 → **+25 %**. The 50
      shares sold that day do not shrink the denominator.

    Chained: 1,3333 × 1,025 × 1,25 − 1 = **70,83 %**.
    """
    upload(client)
    seed_closes(db)

    series = client.get("/api/portfolio/profit-history", params={"range": "max"}).json()
    assert series[0]["twr_pct"] == pytest.approx(0.0), "the window opens at zero"
    assert series[-1]["twr_pct"] == pytest.approx(70.833333, abs=1e-4)
    # Whichever benchmarks happen to be on file, they are rebased to the same
    # first day as the portfolio — otherwise the two lines are not comparable.
    assert all(value == pytest.approx(0.0) for value in series[0]["benchmarks"].values())


def test_profit_history_headline_return_weights_capital_by_time(client: TestClient, db: Session):
    """`return_pct` is Modified Dietz: money that arrived late counts for less.

    The fixture puts 3.000 in on 10/01 and 4.000 more on 12/02, and takes 2.500
    out on 20/03; the result over the window is 950 (750 realised + a 200
    dividend). Weighted by the days each amount was actually invested, the
    capital at work is far below the 7.000 that passed through — so the return
    lands well above the naive result-over-contributions figure, and the two
    measures disagree by design.
    """
    upload(client)
    seed_closes(db)

    series = client.get("/api/portfolio/profit-history", params={"range": "max"}).json()
    first, last = series[0], series[-1]
    assert first["return_pct"] == pytest.approx(0.0), "the window opens at zero"

    # Recompute the denominator the way the page documents it, from the numbers
    # the endpoint itself reports: the result over the capital it was earned on.
    result = last["profit"] - first["profit"]
    assert result == pytest.approx(950.0)
    implied = result / (last["return_pct"] / 100)
    assert 0 < implied < 7000, "weighted capital sits below the money that passed through"
    # Money-weighted is the headline; size-blind is quoted beside it, and on a
    # window this short with one buy doubling the position they cannot agree.
    assert last["return_pct"] != pytest.approx(last["twr_pct"])


def test_profit_history_says_how_much_of_the_portfolio_it_prices(client: TestClient):
    """No closes on file means no time-weighted return — the page is told, not fooled."""
    upload(client)
    series = client.get("/api/portfolio/profit-history", params={"range": "max"}).json()
    assert series[-1]["twr_pct"] == pytest.approx(0.0)
    assert series[-1]["priced_share"] == pytest.approx(0.0)
    # The result in money does not depend on a price series: it is still there,
    # and so is the money-weighted return built from it.
    assert series[-1]["profit"] == pytest.approx(950.0)
    assert series[-1]["return_pct"] > 0


def test_performers_report_the_window_they_were_asked_for(client: TestClient):
    upload(client)
    total = client.get("/api/reports/performers", params={"window": "total"}).json()
    assert total["window"] == "total"
    assert total["best"][0]["ticker"] == "PETR4"
    assert total["best"][0]["window_change"] == pytest.approx(950.0)

    # A single session has to be compared against a quote's previous close, and
    # there is none here — so the ranking comes back empty rather than wrong.
    day = client.get("/api/reports/performers", params={"window": "day"}).json()
    assert day["window"] == "day"
    assert day["best"] == []

    # The whole history happened before this window, so nothing moved in it.
    recent = client.get("/api/reports/performers", params={"window": "1m"}).json()
    assert recent["window"] == "1m"
    assert recent["best"] == []


def test_the_dashboard_and_the_proventos_page_quote_the_same_income(client: TestClient):
    """Both screens report what reached the account, not what was declared.

    A JCP of 100 with 15 withheld made the portfolio 85. The dashboard used to
    show the gross figure and the Proventos page the net one, so the two
    disagreed by exactly the tax — and the headline result counted money that
    never arrived.
    """
    upload(
        client,
        HEADER
        + (
            'Credito,10/01/2024,Transferência - Liquidação,PETR4 - PETROLEO BRASILEIRO S.A.,'
            'XP INVESTIMENTOS CCTVM S/A,100," R$ 30,00 "," R$ 3.000,00 "\n'
            'Credito,15/02/2024,Juros Sobre Capital Próprio,PETR4 - PETROLEO BRASILEIRO S.A.,'
            'XP INVESTIMENTOS CCTVM S/A,100," R$ 1,00 "," R$ 100,00 "\n'
            'Debito,15/02/2024,IRRF,PETR4 - PETROLEO BRASILEIRO S.A.,'
            'XP INVESTIMENTOS CCTVM S/A,100," R$ 0,15 "," R$ 15,00 "\n'
        ),
        "jcp.csv",
    )

    overview = client.get("/api/portfolio/overview").json()
    totals = client.get("/api/portfolio/dividends").json()["totals"]

    assert totals["all_time"] == pytest.approx(100.0), "gross is what was declared"
    assert totals["tax"] == pytest.approx(15.0)
    assert totals["net"] == pytest.approx(85.0)
    assert overview["income_total"] == pytest.approx(totals["net"])
    # And the headline result is built from the same net figure.
    assert overview["total_profit"] == pytest.approx(
        overview["unrealized_pnl"] + overview["realized_pnl"] + 85.0
    )

    # Per asset, the gross figure and the tax stay visible beside the net one,
    # so the ledger can still be reconciled against a broker statement.
    position = client.get("/api/portfolio/positions").json()[0]
    assert position["income"] == pytest.approx(85.0)
    assert position["income_gross"] == pytest.approx(100.0)
    assert position["income_withheld"] == pytest.approx(15.0)


def test_transaction_filters_and_export(client: TestClient):
    upload(client)
    filtered = client.get("/api/transactions", params={"op_type": ["DIVIDEND"]}).json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["op_type"] == "DIVIDEND"

    searched = client.get("/api/transactions", params={"search": "petroleo"}).json()
    assert searched["total"] == 4

    export = client.get("/api/transactions/export")
    assert export.status_code == 200
    assert "text/csv" in export.headers["content-type"]
    assert len(export.text.strip().splitlines()) == 5  # header + 4 rows


def test_allocation_history_and_reports(client: TestClient):
    upload(client)
    assert client.get("/api/portfolio/allocation").json()[0]["ticker" if False else "key"] == "PETR4"
    assert len(client.get("/api/portfolio/history").json()) > 0
    summary = client.get("/api/reports/summary").json()
    assert summary["performers"]["best"][0]["ticker"] == "PETR4"
    assert client.get("/api/portfolio/warnings").status_code == 200


def test_search_and_settings(client: TestClient):
    upload(client)
    found = client.get("/api/search", params={"q": "PETR"}).json()
    assert found["assets"][0]["ticker"] == "PETR4"

    settings = client.get("/api/settings").json()
    assert "providers" in settings
    updated = client.put("/api/settings", json={"values": {"currency": "USD"}}).json()
    assert updated["values"]["currency"] == "USD"


def test_upload_rejects_a_file_that_is_not_a_b3_export(client: TestClient):
    response = upload(client, "foo,bar\n1,2\n", "wrong.csv")
    assert response.status_code == 422
    assert "column" in response.json()["detail"].lower()


def test_import_history_is_listed(client: TestClient):
    upload(client)
    page = client.get("/api/imports").json()
    assert page["total"] == 1
    assert len(page["items"]) == 1
    detail = client.get(f"/api/imports/{page['items'][0]['id']}").json()
    assert detail["rows_imported"] == 4


def test_import_history_is_paginated(client: TestClient):
    """The log grows with every upload, so the page shows the latest few."""
    for _ in range(7):
        upload(client)

    first = client.get("/api/imports", params={"page_size": 5}).json()
    assert first["total"] == 7
    assert first["pages"] == 2
    assert len(first["items"]) == 5

    second = client.get("/api/imports", params={"page": 2, "page_size": 5}).json()
    assert len(second["items"]) == 2
    # Newest first, and no batch appears on both pages.
    ids = [item["id"] for item in first["items"] + second["items"]]
    assert ids == sorted(ids, reverse=True)
    assert len(set(ids)) == 7
