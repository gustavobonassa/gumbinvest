"""Monthly income tax on sales: the rules (pure), due dates, and the ledger path.

Every figure is synthetic. The rule tests drive ``compute`` with plain monthly
numbers, so each Receita rule is pinned on its own, without a database.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from app.db.models import Asset, Transaction
from app.main import app
from app.portfolio import monthly_tax as mt
from app.portfolio.service import PortfolioService

D = Decimal
TODAY = date(2030, 1, 15)  # every test month is in the past


def month(period, **figures):
    return mt.MonthFigures(period=period, **{k: D(str(v)) for k, v in figures.items()})


def only(rows, period):
    return next(row for row in rows if row["period"] == period)


# ---------------------------------------------------------------------------
# Operações comuns


def test_acoes_gain_is_exempt_up_to_20k_of_sales():
    row = mt.compute([month("2025-03", sales_exempt_eligible=20_000,
                            result_exempt_eligible=3_000)], TODAY)[0]
    assert row["exempt"] is True
    assert row["exempt_gain"] == D(3_000)
    assert row["darf"] == 0 and row["status"] == "sem_imposto"


def test_above_20k_the_whole_gain_pays_15_percent_minus_irrf():
    row = mt.compute([month("2025-03", sales_exempt_eligible="20000.01",
                            result_exempt_eligible=2_000)], TODAY)[0]
    assert row["exempt"] is False
    assert row["common"]["tax"] == D("300.00")
    assert row["irrf_estimated"] == D("1.00")  # 0,005 % of the sales
    assert row["darf"] == D("299.00")
    assert row["due_date"] == date(2025, 4, 30)


def test_etf_and_bdr_gains_are_never_exempt():
    row = mt.compute([month("2025-03", sales_exempt_eligible=5_000, result_exempt_eligible=1_000,
                            sales_common_other=4_000, result_common_other=800)], TODAY)[0]
    assert row["exempt_gain"] == D(1_000)  # the ações part
    assert row["common"]["tax"] == D("120.00")  # 15 % of the ETF gain only


def test_losses_carry_forward_and_offset_later_gains():
    rows = mt.compute([
        month("2025-01", sales_exempt_eligible=30_000, result_exempt_eligible=-1_000),
        month("2025-02", sales_exempt_eligible=30_000, result_exempt_eligible=1_500),
    ], TODAY)
    assert only(rows, "2025-01")["losses_after"]["common"] == D(1_000)
    february = only(rows, "2025-02")
    assert february["common"]["loss_used"] == D(1_000)
    assert february["common"]["base"] == D(500)
    assert february["common"]["tax"] == D("75.00")
    assert february["losses_after"]["common"] == 0


def test_a_loss_in_an_exempt_month_still_counts():
    rows = mt.compute([
        month("2025-01", sales_exempt_eligible=10_000, result_exempt_eligible=-400),
        month("2025-02", sales_common_other=3_000, result_common_other=1_000),
    ], TODAY)
    assert only(rows, "2025-02")["common"]["base"] == D(600)


def test_an_exempt_gain_does_not_consume_carried_losses():
    rows = mt.compute([
        month("2025-01", sales_exempt_eligible=30_000, result_exempt_eligible=-500),
        month("2025-02", sales_exempt_eligible=10_000, result_exempt_eligible=2_000),
    ], TODAY)
    assert only(rows, "2025-02")["losses_after"]["common"] == D(500)


# ---------------------------------------------------------------------------
# FIIs and day trade: their own rates, their own pools


def test_fii_pays_20_percent_with_no_exemption():
    row = mt.compute([month("2025-05", sales_fii=1_000, result_fii=100)], TODAY)[0]
    assert row["fii"]["tax"] == D("20.00")
    assert row["darf"] == D("20.00")  # IRRF on R$ 1.000 is under R$ 1: not withheld


def test_pools_never_mix():
    rows = mt.compute([
        month("2025-01", sales_fii=5_000, result_fii=-1_000),
        month("2025-02", sales_exempt_eligible=30_000, result_exempt_eligible=1_000),
    ], TODAY)
    february = only(rows, "2025-02")
    assert february["common"]["loss_used"] == 0  # a FII loss cannot touch ações
    assert february["losses_after"]["fii"] == D(1_000)


def test_day_trade_uses_the_entered_result_and_irrf():
    rows = mt.compute([
        mt.MonthFigures(period="2025-06", day_trade_result=D(-300)),
        mt.MonthFigures(period="2025-07", day_trade_result=D(1_000), day_trade_irrf=D("7.00")),
    ], TODAY)
    july = only(rows, "2025-07")
    assert july["day_trade"]["loss_used"] == D(300)
    assert july["day_trade"]["tax"] == D("140.00")  # 20 % of 700
    assert july["irrf_used"] == D("7.00")
    assert july["darf"] == D("133.00")


# ---------------------------------------------------------------------------
# DARF mechanics


def test_a_darf_under_10_reais_rolls_into_the_next_month():
    rows = mt.compute([
        month("2025-01", sales_fii=500, result_fii=30),  # R$ 6 of tax
        month("2025-02", sales_fii=500, result_fii=30),
    ], TODAY)
    january, february = only(rows, "2025-01"), only(rows, "2025-02")
    assert january["darf"] == 0 and january["status"] == "acumulado"
    assert february["carried_in"] == D("6.00")
    assert february["darf"] == D("12.00")


def test_irrf_credit_does_not_cross_the_year():
    rows = mt.compute([
        month("2025-12", sales_exempt_eligible=200_000, result_exempt_eligible=0),  # R$ 10 IRRF, no tax
        month("2026-01", sales_fii=1_000, result_fii=100),
    ], TODAY)
    assert only(rows, "2026-01")["irrf_used"] == 0
    assert only(rows, "2026-01")["darf"] == D("20.00")


def test_status_follows_the_calendar():
    rows = mt.compute([
        month("2029-11", sales_fii=1_000, result_fii=100),
        month("2029-12", sales_fii=1_000, result_fii=100),
        mt.MonthFigures(period="2030-01"),
    ], TODAY)
    assert only(rows, "2029-11")["status"] == "vencido"  # due 2029-12-31
    assert only(rows, "2029-12")["status"] == "a_pagar"  # due 2030-01-31
    assert only(rows, "2030-01")["status"] == "em_andamento"


def test_a_month_marked_paid_stops_reading_as_overdue():
    figures = [month("2029-11", sales_fii=1_000, result_fii=100)]
    assert mt.compute(figures, TODAY)[0]["status"] == "vencido"
    assert mt.compute(figures, TODAY, paid={"2029-11"})[0]["status"] == "pago"


def test_figures_are_rounded_to_cents_before_the_rules():
    row = mt.compute([month("2025-03", sales_exempt_eligible=30_000,
                            result_exempt_eligible="4190.476190476")], TODAY)[0]
    assert row["results"]["acoes"] == D("4190.48")
    assert row["common"]["tax"] == D("628.57")


def test_due_dates_skip_weekends_and_bank_holidays():
    assert mt._easter(2024) == date(2024, 3, 31)
    assert mt._easter(2025) == date(2025, 4, 20)
    assert mt._easter(2026) == date(2026, 4, 5)
    assert {date(2026, 2, 16), date(2026, 2, 17)} <= mt.bank_holidays(2026)  # Carnaval
    assert mt.darf_due_date("2026-01") == date(2026, 2, 27)  # 28/02 is a Saturday
    assert mt.darf_due_date("2025-12") == date(2026, 1, 30)  # rolls into the next year


# ---------------------------------------------------------------------------
# From the ledger


def _tx(portfolio, asset, effect, direction, day, quantity, price, key, *, fees=0):
    amount = D(quantity) * D(price)
    return Transaction(
        portfolio_id=portfolio.id, asset_id=asset.id, broker_id=None, import_batch_id=None,
        trade_date=day, direction=direction,
        op_type="BUY" if effect == "ACQUIRE" else "SELL", effect=effect,
        quantity=D(quantity), unit_price=D(price), gross_amount=amount,
        fees=D(fees), taxes=D(0), net_amount=amount, currency=asset.currency, fx_rate=None,
        raw_movement="Transferência - Liquidação", raw_product=f"{asset.ticker} - {asset.name}",
        raw_institution="Corretora", source_line=None, dedup_key=key, occurrence=0,
    )


def _ledger(db, portfolio):
    stock = Asset(ticker="XPTO3", name="XPTO SA ON", kind="STOCK", currency="BRL")
    fund = Asset(ticker="FUND11", name="FII FUNDO TESTE IMOBILIARIO", kind="FII", currency="BRL")
    abroad = Asset(ticker="ACME", name="Acme Inc", kind="STOCK_INTL", currency="USD")
    db.add_all([stock, fund, abroad])
    db.commit()
    db.add_all([
        _tx(portfolio, stock, "ACQUIRE", "CREDIT", date(2025, 1, 10), 1_000, 20, "a"),
        # March: R$ 25.000 of ações sold above cost — taxable.
        _tx(portfolio, stock, "DISPOSE", "DEBIT", date(2025, 3, 10), 1_000, 25, "b"),
        _tx(portfolio, fund, "ACQUIRE", "CREDIT", date(2025, 1, 10), 10, 100, "c"),
        _tx(portfolio, fund, "DISPOSE", "DEBIT", date(2025, 4, 10), 10, 90, "d", fees=1),
        # A same-day round trip: flagged as a possible day trade.
        _tx(portfolio, fund, "ACQUIRE", "CREDIT", date(2025, 5, 5), 5, 100, "e"),
        _tx(portfolio, fund, "DISPOSE", "DEBIT", date(2025, 5, 5), 5, 101, "f"),
        _tx(portfolio, abroad, "ACQUIRE", "CREDIT", date(2025, 1, 10), 1, 100, "g"),
        _tx(portfolio, abroad, "DISPOSE", "DEBIT", date(2025, 6, 10), 1, 150, "h"),
    ])
    db.commit()


def test_ledger_figures_land_in_the_right_month_and_regime(db, portfolio):
    _ledger(db, portfolio)
    sheet = mt.monthly_taxes(PortfolioService(db, portfolio.id), today=TODAY)
    march = only(sheet["months"], "2025-03")
    assert march["sales"]["acoes"] == D(25_000)
    assert march["results"]["acoes"] == D(5_000)
    assert march["common"]["tax"] == D("750.00")
    assert march["darf"] == D("748.75")  # minus R$ 1,25 of IRRF

    april = only(sheet["months"], "2025-04")
    assert april["results"]["fii"] == D(-101)  # R$ 899 net of the R$ 1 fee, cost R$ 1.000
    assert april["losses_after"]["fii"] == D(101)

    # Abroad is reported apart, never taxed monthly.
    assert sheet["excluded_sales"] == [{"period": "2025-06", "exterior": D(150)}]
    assert not any(row["sales"]["acoes"] for row in sheet["months"] if row["period"] == "2025-06")

    kinds = {warning["kind"] for warning in sheet["warnings"]}
    assert kinds == {"possible_day_trade", "missing_costs"}
    trip = next(w for w in sheet["warnings"] if w["kind"] == "possible_day_trade")["items"][0]
    assert trip["ticker"] == "FUND11" and trip["date"] == "2025-05-05"

    # The month in progress is always on the sheet, newest first.
    assert sheet["months"][0]["period"] == "2030-01"


def test_day_trade_entries_round_trip_through_the_api(db, portfolio):
    _ledger(db, portfolio)
    with TestClient(app) as client:
        assert client.put("/api/taxes/day-trade/2025-13", json={"result": "1"}).status_code == 422
        saved = client.put("/api/taxes/day-trade/2025-07", json={"result": "500.00", "irrf": "5.00"})
        assert saved.status_code == 200
        sheet = client.get("/api/taxes/monthly").json()
        july = only(sheet["months"], "2025-07")
        assert july["day_trade"]["informed"] is True
        assert D(str(july["day_trade"]["tax"])) == D("100.00")

        march = only(client.get("/api/taxes/monthly").json()["months"], "2025-03")
        assert march["status"] == "vencido"
        assert client.put("/api/taxes/paid/2025-03", json={"paid": True}).status_code == 200
        march = only(client.get("/api/taxes/monthly").json()["months"], "2025-03")
        assert march["status"] == "pago"

        assert client.delete("/api/taxes/day-trade/2025-07").status_code == 200
        sheet = client.get("/api/taxes/monthly").json()
        assert not any(row["period"] == "2025-07" for row in sheet["months"])
