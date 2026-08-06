"""Importing broker statements: de-duplication, currency and coverage.

The synthetic tests build statements by hand so the assertions can be exact;
the archive tests replay the user's real files, which is where the interesting
cases live — two reports of one month that disagree, an account that migrated
mid-month, a missing download.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.models import Asset, FxRate, ImportBatch, Transaction
from app.domain.enums import Direction, OperationType
from app.importer.coverage import statement_coverage
from app.importer.dedup import FuzzyIndex, fuzzy_key
from app.importer.pdf.base import ParsedStatement, StatementRow
from app.importer.pdf.movements import BUY, DIVIDEND, DIVIDEND_TAX
from app.importer.service import ImportService
from app.portfolio.service import PortfolioService
from tests.conftest import parsed_statements, requires_statements

#: Import order matters when two reports describe one month, so the tests use
#: the same priority the startup auto-import does.
FORMAT_PRIORITY = {"apex-en": 0, "apex-ascend": 0, "drivewealth": 1, "avenue-pt": 2}


def _statement(rows: list[StatementRow], *, fmt: str = "apex-en", broker: str = "Avenue") -> ParsedStatement:
    return ParsedStatement(
        format=fmt,
        broker=broker,
        institution_raw=f"{broker} test",
        currency="USD",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        account_ref="TEST-1",
        opening_balance=Decimal("1000"),
        closing_balance=Decimal("1100"),
        rows=rows,
    )


def _buy(day: int, symbol: str, quantity: str, amount: str, description: str = "") -> StatementRow:
    return StatementRow(
        trade_date=date(2025, 1, day),
        movement=BUY,
        direction=Direction.DEBIT,
        amount=Decimal(amount),
        quantity=Decimal(quantity),
        symbol=symbol,
        # The description is what decides the asset class, so a test that cares
        # about the class has to supply a realistic one.
        description=description or f"{symbol} INC",
        section="BUY / SELL TRANSACTIONS",
    )


def _dividend(
    day: int, symbol: str, amount: str, movement: str = DIVIDEND, description: str = ""
) -> StatementRow:
    return StatementRow(
        trade_date=date(2025, 1, day),
        movement=movement,
        direction=Direction.CREDIT if movement == DIVIDEND else Direction.DEBIT,
        amount=Decimal(amount),
        symbol=symbol,
        description=description or f"{symbol} INC",
        section="DIVIDENDS AND INTEREST",
    )


def _import(db, portfolio, statement: ParsedStatement, name: str = "stmt.pdf"):
    return ImportService(db, portfolio).import_pdf(b"%PDF-fake", name, statement)


# -- de-duplication ---------------------------------------------------------
def test_reimporting_the_same_statement_adds_nothing(db, portfolio):
    statement = _statement([_buy(5, "O", "1.5", "83.59"), _dividend(10, "O", "4.62")])
    first = _import(db, portfolio, statement)
    assert first.rows_imported == 2

    second = _import(db, portfolio, _statement([_buy(5, "O", "1.5", "83.59"), _dividend(10, "O", "4.62")]))
    assert second.rows_imported == 0
    assert db.scalar(select(func.count(Transaction.id))) == 2


def test_identical_rows_in_one_statement_are_all_kept(db, portfolio):
    """Avenue's April 2025 report lists the same $5.00 reversal nine times."""
    rows = [_dividend(7, "SLG", "5.00", DIVIDEND_TAX) for _ in range(9)]
    result = _import(db, portfolio, _statement(rows))
    assert result.rows_imported == 9


def test_the_same_event_from_two_reports_is_imported_once(db, portfolio):
    """Apex and Avenue disagree by a day and by the commission.

    Apex dates a dividend 2025-01-03 and prices a purchase at $165.17; Avenue
    dates the same dividend 2025-01-04 and prices the same purchase, same
    quantity, at $162.67. Both are the same two events.
    """
    _import(db, portfolio, _statement([_buy(11, "O", "3.01743", "165.17"), _dividend(3, "VZ", "46.25")]),
            "apex.pdf")
    result = _import(
        db,
        portfolio,
        _statement(
            [_buy(11, "O", "3.01743", "162.67"), _dividend(4, "VZ", "46.25")], fmt="avenue-pt"
        ),
        "avenue.pdf",
    )
    assert result.rows_imported == 0
    assert result.summary["skipped"]["cross_source_duplicates"] == 2
    assert db.scalar(select(func.count(Transaction.id))) == 2


def test_a_second_report_still_contributes_what_the_first_lacked(db, portfolio):
    """Avenue reports dividends the Apex statement leaves out entirely."""
    _import(db, portfolio, _statement([_dividend(3, "VZ", "46.25")]), "apex.pdf")
    result = _import(
        db,
        portfolio,
        _statement([_dividend(3, "VZ", "46.25"), _dividend(2, "NOBL", "15.27")], fmt="avenue-pt"),
        "avenue.pdf",
    )
    assert result.rows_imported == 1
    tickers = set(db.scalars(select(Asset.ticker)).all())
    assert tickers == {"VZ", "NOBL"}


def test_two_reports_of_the_same_format_do_not_fuzzy_match(db, portfolio):
    """Within one format a repeat is a real repeat, not a re-report.

    Only the exact key applies, so a genuinely duplicated download is skipped
    while a genuinely repeated movement is kept.
    """
    _import(db, portfolio, _statement([_dividend(3, "VZ", "46.25")]), "a.pdf")
    result = _import(db, portfolio, _statement([_dividend(4, "VZ", "46.25")]), "b.pdf")
    assert result.rows_imported == 1


def test_fuzzy_matching_is_count_aware():
    """Three reported events cannot cancel nine identical ones."""
    index = FuzzyIndex()
    key = fuzzy_key("Avenue", "TAX", "CASH_OUT", "SLG", Decimal(0), Decimal("5.00"))
    for _ in range(3):
        index.add(key, date(2026, 4, 7))
    matched = sum(1 for _ in range(9) if index.take(key, date(2026, 4, 7)))
    assert matched == 3


def test_fuzzy_matching_respects_the_date_window():
    index = FuzzyIndex()
    key = fuzzy_key("Avenue", "DIVIDEND", "CASH_IN", "VZ", Decimal(0), Decimal("46.25"))
    index.add(key, date(2025, 2, 3))
    assert not index.take(key, date(2025, 3, 3))  # a month later is another dividend
    assert index.take(key, date(2025, 2, 4))  # a day later is the same one


def test_transfer_legs_are_not_merged_by_fuzzy_matching():
    """The two sides of a custody move must both survive to cancel out."""
    outbound = fuzzy_key("Nomad", "TRANSFER_OUT", "QTY_OUT_FREE", "NKE", Decimal("29.6"), Decimal(0))
    inbound = fuzzy_key("Nomad", "TRANSFER_IN", "QTY_IN_FREE", "NKE", Decimal("29.6"), Decimal(0))
    assert outbound != inbound


# -- currency ---------------------------------------------------------------
def _seed_rates(db, rate: str = "5.00") -> None:
    day = date(2024, 1, 1)
    while day <= date(2026, 12, 31):
        db.add(FxRate(base="USD", quote="BRL", date=day, rate=Decimal(rate)))
        day += timedelta(days=30)
    db.commit()


def test_foreign_movements_are_stamped_with_the_trade_date_rate(db, portfolio):
    _seed_rates(db)
    _import(db, portfolio, _statement([_buy(15, "O", "2", "100.00")]))
    transaction = db.scalar(select(Transaction))
    assert transaction.currency == "USD"
    assert transaction.fx_rate == Decimal("5.00000000")


def test_positions_stay_native_while_totals_convert(db, portfolio):
    _seed_rates(db)
    _import(db, portfolio, _statement([_buy(15, "O", "2", "100.00"), _dividend(20, "O", "10.00")]))

    service = PortfolioService(db, portfolio.id)
    position = next(iter(service.asset_positions(include_closed=True)))
    assert position.asset.currency == "USD"
    # Native: the dollars actually paid.
    assert position.position.cost_basis == Decimal("100.00")
    assert position.position.income == Decimal("10.00")
    # Base: the same figures in reais.
    assert position.cost_basis_base == Decimal("500.00")
    assert position.income_base == Decimal("50.00")

    overview = service.overview()
    assert overview["base_currency"] == "BRL"
    assert overview["cost_basis"] == Decimal("500.00")
    assert overview["income_total"] == Decimal("50.00")


def test_a_movement_with_no_rate_available_is_left_out_and_flagged(db, portfolio):
    """Better a total that is visibly short than one that is quietly wrong.

    With no rate downloaded, treating a dollar as a real would overstate the
    holding by a factor of five and look entirely plausible. The position is
    excluded from the converted totals and named in ``unconverted_positions``.
    """
    _import(db, portfolio, _statement([_buy(15, "O", "2", "100.00")]))
    service = PortfolioService(db, portfolio.id)
    overview = service.overview()
    assert overview["cost_basis"] == Decimal(0)
    assert overview["unconverted_positions"] == ["O"]
    # The native position is unaffected: the asset page still shows the dollars.
    assert service.positions()[db.scalar(select(Asset.id))].cost_basis == Decimal("100.00")


def test_withholding_reduces_income_rather_than_counting_as_a_fee_refund(db, portfolio):
    _seed_rates(db)
    _import(
        db,
        portfolio,
        _statement([_dividend(10, "O", "20.00"), _dividend(10, "O", "6.00", DIVIDEND_TAX)]),
    )
    service = PortfolioService(db, portfolio.id)
    position = next(iter(service.positions().values()))
    assert position.income == Decimal("20.00")
    # Withholding is tracked apart from brokerage: one is taken out of the
    # dividend, the other is a cost of trading, and only the first may be
    # netted off income.
    assert position.income_tax == Decimal("6.00")
    assert position.fees == Decimal("0.00")


def test_a_refunded_withholding_gives_the_cost_back(db, portfolio):
    """"NRA ADJ": Apex returns tax it took, sometimes years later.

    It reduces what the position cost; counting it as income would inflate
    dividends received.
    """
    _seed_rates(db)
    row = _dividend(12, "O", "3.00", DIVIDEND_TAX)
    row.direction = Direction.CREDIT
    _import(db, portfolio, _statement([row]))
    position = next(iter(PortfolioService(db, portfolio.id).positions().values()))
    assert position.income == Decimal(0)
    assert position.income_tax == Decimal("-3.00")
    assert position.fees == Decimal("0.00")


def test_income_analytics_convert_foreign_dividends(db, portfolio):
    """The Proventos page adds payments across assets, so it needs one currency.

    A US$ 10 dividend left unconverted next to a R$ 10 one understates it
    fivefold and flatters every Brazilian payer in the ranking.
    """
    _seed_rates(db)
    _import(db, portfolio, _statement([_buy(15, "O", "2", "100.00"), _dividend(20, "O", "10.00")]))

    report = PortfolioService(db, portfolio.id).dividends("month")
    assert report["totals"]["all_time"] == Decimal("50.00")  # 10 USD x 5.00
    assert report["series"][0]["total"] == Decimal("50.00")
    assert report["by_kind"][0]["total"] == Decimal("50.00")
    assert report["by_asset"][0]["ticker"] == "O"
    assert report["by_asset"][0]["total"] == Decimal("50.00")


def test_income_series_carries_both_breakdown_axes(db, portfolio):
    """Payment type and asset class are independent questions, both answered.

    "What kind of payment was it" and "what paid it" are different views of the
    same money, so the series carries both and the chart picks one — merging
    them into a single breakdown makes neither readable.
    """
    _seed_rates(db)
    # Two US payments of different classes, plus a Brazilian one.
    _import(
        db,
        portfolio,
        _statement([_buy(2, "NKE", "1", "50"), _dividend(20, "NKE", "10.00")]),
        "nke.pdf",
    )
    _import(
        db,
        portfolio,
        _statement(
            [
                _buy(2, "O", "1", "50", "REALTY INCOME CORP"),
                _dividend(21, "O", "4.00", description="REALTY INCOME CORP"),
            ]
        ),
        "o.pdf",
    )
    ImportService(db, portfolio).import_csv(
        "Entrada/Saída,Data,Movimentação,Produto,Instituição,Quantidade,Preço unitário,Valor da Operação\n"
        'Credito,22/01/2025,Dividendo,PETR4 - PETROLEO,XP,100," R$ 1,00 "," R$ 100,00 "\n',
        "b3.csv",
    )

    report = PortfolioService(db, portfolio.id).dividends("month")

    # By type: every one of these is a dividend, wherever it was paid.
    by_type = {row["op_type"]: row["total"] for row in report["by_type"]}
    assert by_type["DIVIDEND"] == Decimal("170.00")

    # By class: the same money, split by what paid it.
    by_kind = {row["kind"]: row["total"] for row in report["by_kind"]}
    assert by_kind["STOCK"] == Decimal("100.00")
    assert by_kind["STOCK_INTL"] == Decimal("50.00")  # 10 USD x 5.00
    assert by_kind["REIT"] == Decimal("20.00")  # 4 USD x 5.00

    # The period series carries both, so switching axis needs no new request.
    period = next(p for p in report["series"] if p["period"] == "2025-01")
    assert period["types"] == {"DIVIDEND": Decimal("170.00")}
    assert period["kinds"] == {
        "STOCK": Decimal("100.00"),
        "STOCK_INTL": Decimal("50.00"),
        "REIT": Decimal("20.00"),
    }
    assert period["total"] == Decimal("170.00")
    # Both axes must add up to the same total, or one of the charts is lying.
    assert sum(period["types"].values()) == sum(period["kinds"].values()) == period["total"]


def test_payment_types_stay_distinct_within_one_class(db, portfolio):
    """Grouping by class must not lose the type axis, and vice versa.

    Two payments from the same class but of different types split on one axis
    and merge on the other — which is exactly why both are kept.
    """
    ImportService(db, portfolio).import_csv(
        "Entrada/Saída,Data,Movimentação,Produto,Instituição,Quantidade,Preço unitário,Valor da Operação\n"
        'Credito,22/01/2025,Dividendo,PETR4 - PETROLEO,XP,100," R$ 1,00 "," R$ 100,00 "\n'
        'Credito,23/01/2025,Juros Sobre Capital Próprio,PETR4 - PETROLEO,XP,100," R$ 0,50 "," R$ 50,00 "\n',
        "b3.csv",
    )
    period = PortfolioService(db, portfolio.id).dividends("month")["series"][0]
    assert period["types"] == {"DIVIDEND": Decimal("100.00"), "JCP": Decimal("50.00")}
    assert period["kinds"] == {"STOCK": Decimal("150.00")}


def test_income_reports_gross_tax_and_net(db, portfolio):
    """A US dividend arrives with 30 % withheld, so both figures are reported.

    Gross is what gets declared and what every breakdown sums to; net is what
    reached the account. Showing only one of them would be wrong for one of the
    two questions the page is asked.
    """
    _seed_rates(db)
    _import(
        db,
        portfolio,
        _statement(
            [
                _dividend(20, "NKE", "10.00"),
                _dividend(20, "NKE", "3.00", DIVIDEND_TAX),
            ]
        ),
    )

    report = PortfolioService(db, portfolio.id).dividends("month")
    totals = report["totals"]
    assert totals["all_time"] == Decimal("50.00")  # 10 USD gross
    assert totals["tax"] == Decimal("15.00")  # 3 USD withheld
    assert totals["net"] == Decimal("35.00")

    period = report["series"][0]
    assert period["total"] == Decimal("50.00")
    assert period["tax"] == Decimal("15.00")
    assert period["net"] == Decimal("35.00")
    # The breakdown axes stay gross, so they still sum to the gross total.
    assert sum(period["types"].values()) == period["total"]

    asset = report["by_asset"][0]
    assert (asset["total"], asset["tax"], asset["net"]) == (
        Decimal("50.00"),
        Decimal("15.00"),
        Decimal("35.00"),
    )


def test_every_income_breakdown_has_a_net_counterpart_that_adds_up(db, portfolio):
    """Each widget on the Proventos page reads a different slice of this.

    If one of them is left on gross while the headline says "líquido", the page
    contradicts itself — so every breakdown is checked against the same net
    total here rather than trusting each screen to have been updated.
    """
    _seed_rates(db)
    _import(
        db,
        portfolio,
        _statement(
            [
                _dividend(20, "NKE", "10.00"),
                _dividend(20, "NKE", "3.00", DIVIDEND_TAX),
                _buy(2, "O", "1", "50", "REALTY INCOME CORP"),
                _dividend(21, "O", "4.00", description="REALTY INCOME CORP"),
                _dividend(21, "O", "1.20", DIVIDEND_TAX, description="REALTY INCOME CORP"),
            ]
        ),
    )
    ImportService(db, portfolio).import_csv(
        "Entrada/Saída,Data,Movimentação,Produto,Instituição,Quantidade,Preço unitário,Valor da Operação\n"
        'Credito,22/01/2025,Dividendo,PETR4 - PETROLEO,XP,100," R$ 1,00 "," R$ 100,00 "\n',
        "b3.csv",
    )

    report = PortfolioService(db, portfolio.id).dividends("month")
    net = report["totals"]["net"]
    assert net == Decimal("100.00") + Decimal("50.00") - Decimal("15.00") + Decimal("20.00") - Decimal("6.00")

    # by_kind — the "Proventos por classe" donut and its table.
    assert sum(row["net"] for row in report["by_kind"]) == net
    # by_type — the "Tipo" grouping of the period chart.
    assert sum(row["net"] for row in report["by_type"]) == net
    # by_asset — the payers ranking and the "Renda por ativo" table.
    assert sum(row["net"] for row in report["by_asset"]) == net

    period = report["series"][0]
    # Both stacked breakdowns of the period chart.
    assert sum(period["types_net"].values()) == period["net"]
    assert sum(period["kinds_net"].values()) == period["net"]
    assert period["net"] == net

    # Shares are shares *of the net total*, so they still make 100 %.
    assert sum(row["share"] for row in report["by_kind"]) == pytest.approx(
        Decimal(100), abs=Decimal("0.01")
    )


def test_withholding_is_attributed_to_the_income_it_came_from(db, portfolio):
    """Tax names no payment type, so it is put back on the one it was taken off.

    Without that the "Tipo" chart would show a dividend segment at its gross
    height while the bar total was net, and the two would never reconcile.
    """
    _seed_rates(db)
    _import(
        db,
        portfolio,
        _statement([_dividend(20, "NKE", "10.00"), _dividend(20, "NKE", "3.00", DIVIDEND_TAX)]),
    )
    report = PortfolioService(db, portfolio.id).dividends("month")
    by_type = {row["op_type"]: row for row in report["by_type"]}
    assert by_type["DIVIDEND"]["tax"] == Decimal("15.00")
    assert by_type["DIVIDEND"]["net"] == Decimal("35.00")
    assert report["series"][0]["types_net"] == {"DIVIDEND": Decimal("35.00")}


def test_the_payment_calendar_shows_what_landed(db, portfolio):
    """A payment and its withholding are separate rows on the same day."""
    _seed_rates(db)
    _import(
        db,
        portfolio,
        _statement([_dividend(20, "NKE", "10.00"), _dividend(20, "NKE", "3.00", DIVIDEND_TAX)]),
    )
    entry = PortfolioService(db, portfolio.id).income_calendar(10)[0]
    assert entry["amount"] == Decimal("50.00")
    assert entry["tax"] == Decimal("15.00")
    assert entry["net"] == Decimal("35.00")


def test_a_withholding_refund_increases_net_income(db, portfolio):
    """"NRA ADJ" returns tax withheld earlier, sometimes years later."""
    _seed_rates(db)
    refund = _dividend(25, "NKE", "1.00", DIVIDEND_TAX)
    refund.direction = Direction.CREDIT
    _import(db, portfolio, _statement([_dividend(20, "NKE", "10.00"), refund]))

    totals = PortfolioService(db, portfolio.id).dividends("month")["totals"]
    assert totals["tax"] == Decimal("-5.00")  # 1 USD came back
    assert totals["net"] == Decimal("55.00")  # more than the gross dividend


def test_brokerage_is_reported_but_never_netted_off_income(db, portfolio):
    """Avenue bills US$ 2.50 per order as a separate "Corretagem" row.

    It is a cost of buying, not a deduction from a dividend, so it must not
    reduce reported income — but it is still surfaced rather than dropped.
    """
    _seed_rates(db)
    from app.importer.pdf import movements

    fee = _dividend(15, "NKE", "2.50", movements.FEE)
    fee.direction = Direction.DEBIT
    _import(db, portfolio, _statement([_dividend(20, "NKE", "10.00"), fee]))

    totals = PortfolioService(db, portfolio.id).dividends("month")["totals"]
    assert totals["net"] == Decimal("50.00")  # untouched by the commission
    assert totals["tax"] == Decimal(0)
    assert totals["trading_costs"] == Decimal("12.50")  # 2.50 USD x 5.00


def test_yield_on_cost_divides_converted_income_by_converted_cost(db, portfolio):
    """Both sides of the ratio must be in the same currency.

    Converting only the income would report a yield five times too high.
    """
    _seed_rates(db)
    _import(db, portfolio, _statement([_buy(15, "O", "2", "100.00"), _dividend(20, "O", "10.00")]))

    entry = PortfolioService(db, portfolio.id).dividends("month")["by_asset"][0]
    assert entry["cost_basis"] == Decimal("500.00")  # 100 USD x 5.00
    assert entry["yield_on_cost"] == Decimal("10")  # 50 / 500, not 50 / 100


def test_fx_status_reports_the_rate_currently_in_force(db, portfolio):
    """The sidebar prints this, so it must be the latest rate, not any rate."""
    from app.market.fx import fx_status

    db.add(FxRate(base="USD", quote="BRL", date=date(2026, 7, 30), rate=Decimal("5.10")))
    db.add(FxRate(base="USD", quote="BRL", date=date(2026, 7, 31), rate=Decimal("5.25")))
    db.add(FxRate(base="USD", quote="BRL", date=date(2026, 7, 29), rate=Decimal("4.90")))
    db.commit()

    series = fx_status(db)[0]
    assert series["pair"] == "USD/BRL"
    assert series["end"] == date(2026, 7, 31)
    assert series["rate"] == Decimal("5.25000000")
    assert series["points"] == 3


def test_income_calendar_is_converted(db, portfolio):
    _seed_rates(db)
    _import(db, portfolio, _statement([_dividend(20, "O", "10.00")]))
    entry = PortfolioService(db, portfolio.id).income_calendar(10)[0]
    assert entry["amount"] == Decimal("50.00")


def test_monthly_series_convert_foreign_movements(db, portfolio):
    """Aportes mensais and Proventos por mês are base-currency series."""
    _seed_rates(db)
    _import(db, portfolio, _statement([_buy(15, "O", "2", "100.00"), _dividend(20, "O", "10.00")]))

    service = PortfolioService(db, portfolio.id)
    assert service.contributions_series("month")[0]["bought"] == Decimal("500.00")
    assert service.income_series("month")[0]["total"] == Decimal("50.00")


def test_allocation_shares_add_up_across_currencies(db, portfolio):
    """A mixed portfolio's slices must still total 100 %.

    They only do if the numerator and the denominator are both converted — the
    bug this guards against showed a US position's value in dollars against a
    portfolio total in reais.
    """
    _seed_rates(db)
    _import(db, portfolio, _statement([_buy(15, "O", "2", "100.00")]))
    # A domestic asset alongside it, so the two currencies actually mix.
    ImportService(db, portfolio).import_csv(
        "Entrada/Saída,Data,Movimentação,Produto,Instituição,Quantidade,Preço unitário,Valor da Operação\n"
        'Credito,15/01/2025,Transferência - Liquidação,PETR4 - PETROLEO,XP,100," R$ 5,00 "," R$ 500,00 "\n',
        "b3.csv",
    )

    service = PortfolioService(db, portfolio.id)
    slices = service.allocation("asset")
    assert sum(s["percent"] for s in slices) == pytest.approx(Decimal(100), abs=Decimal("0.01"))
    # Both positions cost the same in reais, so neither may dominate.
    by_key = {s["key"]: s["percent"] for s in slices}
    assert by_key["O"] == pytest.approx(by_key["PETR4"], abs=Decimal("0.01"))


def test_positions_carry_both_currencies_for_the_widgets(db, portfolio):
    """Widgets that mix assets read the ``_base`` fields; asset pages read native."""
    _seed_rates(db)
    _import(db, portfolio, _statement([_buy(15, "O", "2", "100.00"), _dividend(20, "O", "10.00")]))

    service = PortfolioService(db, portfolio.id)
    items = service.asset_positions()
    row = items[0].to_dict(sum((i.market_value_base for i in items), Decimal(0)))
    assert row["currency"] == "USD"
    assert row["market_value"] == Decimal("100.00")  # native, for the asset page
    assert row["market_value_base"] == Decimal("500.00")  # converted, for totals
    assert row["income_base"] == Decimal("50.00")
    assert row["total_return_base"] == row["unrealized_pnl_base"] + row["realized_pnl_base"] + row["income_base"]
    assert row["allocation_pct"] == Decimal(100)


# -- cash rows --------------------------------------------------------------
def test_cash_movements_are_reported_but_not_persisted(db, portfolio):
    from app.importer.pdf.movements import CASH_MOVEMENT

    row = StatementRow(
        trade_date=date(2025, 1, 12),
        movement=CASH_MOVEMENT,
        direction=Direction.CREDIT,
        amount=Decimal("911.80"),
        description="ICT Deposit",
        section="NON-TRADING ACTIVITY",
    )
    result = _import(db, portfolio, _statement([row]))
    assert result.rows_imported == 0
    assert result.summary["skipped"]["cash_movements"] == 1
    assert db.scalar(select(func.count(Transaction.id))) == 0


def test_an_unidentified_cash_row_is_reported_not_invented(db, portfolio):
    """Avenue prints withholding reversals with the ticker column blank."""
    row = StatementRow(
        trade_date=date(2025, 4, 7),
        movement=DIVIDEND_TAX,
        direction=Direction.CREDIT,
        amount=Decimal("5.66"),
        description="Estorno Retenção Impostos sobre Dividendos",
        section="DIVIDENDS AND INTEREST",
    )
    result = _import(db, portfolio, _statement([row]))
    assert result.rows_imported == 0
    assert result.summary["skipped"]["unattributed"] == 1
    assert db.scalar(select(func.count(Asset.id))) == 0


def test_an_unidentified_row_that_moves_quantity_is_kept(db, portfolio):
    """Dropping it would silently corrupt the position held."""
    row = StatementRow(
        trade_date=date(2025, 4, 7),
        movement=BUY,
        direction=Direction.DEBIT,
        amount=Decimal("100.00"),
        quantity=Decimal("3"),
        description="MYSTERY HOLDINGS INC",
        section="BUY / SELL TRANSACTIONS",
    )
    result = _import(db, portfolio, _statement([row]))
    assert result.rows_imported == 1
    assert result.summary["skipped"]["provisional_tickers"] == 1
    assert db.scalar(select(Asset.ticker)).startswith("?")


# -- coverage ---------------------------------------------------------------
def test_coverage_finds_a_missing_month(db, portfolio):
    for month in (1, 2, 4):  # March never downloaded
        statement = _statement([_buy(5, "O", "1", "50")])
        statement.period_start = date(2025, month, 1)
        statement.period_end = date(2025, month, 28)
        _import(db, portfolio, statement, f"stmt-{month}.pdf")

    accounts = statement_coverage(db, portfolio.id)
    assert len(accounts) == 1
    assert accounts[0]["missing_months"] == ["2025-03"]
    assert accounts[0]["is_complete"] is False


def test_coverage_flags_a_balance_that_does_not_carry_over(db, portfolio):
    first = _statement([_buy(5, "O", "1", "50")])
    first.period_start, first.period_end = date(2025, 1, 1), date(2025, 1, 31)
    first.closing_balance = Decimal("1000")
    _import(db, portfolio, first, "jan.pdf")

    second = _statement([_buy(6, "O", "1", "50")])
    second.period_start, second.period_end = date(2025, 2, 1), date(2025, 2, 28)
    second.opening_balance = Decimal("2500")  # value appeared from nowhere
    _import(db, portfolio, second, "feb.pdf")

    breaks = statement_coverage(db, portfolio.id)[0]["balance_breaks"]
    assert len(breaks) == 1
    assert breaks[0]["month"] == "2025-02"
    assert Decimal(breaks[0]["difference"]) == Decimal("1500")


def test_coverage_separates_two_series_of_the_same_broker(db, portfolio):
    """Avenue issues an Apex-numbered report and an Avenue-numbered one."""
    apex = _statement([_buy(5, "O", "1", "50")])
    apex.account_ref = "6AV-56990-17"
    _import(db, portfolio, apex, "apex.pdf")

    avenue = _statement([_buy(5, "O", "1", "50")], fmt="avenue-pt")
    avenue.account_ref = "098455499"
    _import(db, portfolio, avenue, "avenue.pdf")

    accounts = statement_coverage(db, portfolio.id)
    assert {a["account_ref"] for a in accounts} == {"6AV-56990-17", "098455499"}


# -- the real archive -------------------------------------------------------
def _import_archive(db, portfolio) -> None:
    service = ImportService(db, portfolio)
    parsed = [
        (
            statement.broker,
            statement.period_start,
            FORMAT_PRIORITY.get(statement.format, 9),
            path,
            statement,
        )
        for path, statement in parsed_statements()
    ]
    parsed.sort(key=lambda item: (item[0], item[1], item[2], item[3].name))
    for *_, path, statement in parsed:
        service.import_pdf(path.read_bytes(), path.name, statement)


@pytest.fixture
def archive(db, portfolio):
    _seed_rates(db)
    _import_archive(db, portfolio)
    return db, portfolio


@requires_statements
def test_archive_import_is_idempotent(archive):
    db, portfolio = archive
    before = db.scalar(select(func.count(Transaction.id)))
    assert before > 0
    _import_archive(db, portfolio)
    assert db.scalar(select(func.count(Transaction.id))) == before


@requires_statements
def test_archive_positions_match_the_brokers_own_holdings(archive):
    """The end-to-end proof: replaying the archive reproduces the statements.

    Every quantity the engine computes is compared with the figure the most
    recent statement of each broker prints for it. A disagreement means a
    movement was missed, mis-parsed or double-counted.
    """
    db, portfolio = archive
    drift = [
        f"{account['broker']} {item['ticker']}: "
        f"extrato {item['reported']} vs calculado {item['computed']}"
        for account in statement_coverage(db, portfolio.id)
        for item in account["position_drift"]
    ]
    assert not drift, "positions disagree with the statements:\n" + "\n".join(drift)


@requires_statements
def test_archive_leaves_no_failed_batches(archive):
    db, _ = archive
    failed = db.scalars(select(ImportBatch.filename).where(ImportBatch.status == "FAILED")).all()
    assert not failed


@requires_statements
def test_archive_assets_are_all_dollar_denominated(archive):
    db, _ = archive
    currencies = set(db.scalars(select(Asset.currency)).all())
    assert currencies == {"USD"}


@requires_statements
def test_startup_reclassification_keeps_us_assets_quotable(archive):
    """`reclassify_assets` runs on every boot and must not undo the US kinds.

    The B3 classifier reads the ticker suffix, which says nothing about a US
    listing: run it over `NKE` and every offshore holding becomes `OTHER`, a
    family no quote provider is asked about, so the whole US side would silently
    stop being priced.
    """
    from app.importer.service import reclassify_assets

    reclassify_assets(db := archive[0])
    kinds = {
        ticker: kind
        for ticker, kind in db.execute(select(Asset.ticker, Asset.kind)).all()
    }
    assert kinds["NKE"] == "STOCK_INTL"
    assert kinds["VOO"] == "ETF_INTL"
    assert kinds["O"] == "REIT"
    assert "OTHER" not in {kinds[t] for t in ("NKE", "VOO", "O", "BAC", "VNQ", "STAG")}


@requires_statements
def test_archive_reits_are_all_classified_as_reits(archive):
    """The real holdings, checked against the class they belong to."""
    db, _ = archive
    kinds = dict(db.execute(select(Asset.ticker, Asset.kind)).all())
    for ticker in ("SLG", "STAG", "WPC", "MAC", "STOR", "RC", "O", "MPT", "CIO", "ARI"):
        assert kinds.get(ticker) == "REIT", f"{ticker} is {kinds.get(ticker)}"
    assert kinds["NKE"] == "STOCK_INTL"
    assert kinds["VNQ"] == "ETF_INTL"


@requires_statements
def test_a_renamed_ticker_ends_up_under_one_asset(archive):
    """MPW and MPT are the same company, so there is one asset, named MPT."""
    from app.importer.service import reconcile_ticker_aliases

    db, _ = archive
    reconcile_ticker_aliases(db)
    tickers = set(db.scalars(select(Asset.ticker)).all())
    assert "MPT" in tickers and "MPW" not in tickers
    assert "BNY" in tickers and "BK" not in tickers


@requires_statements
def test_renaming_a_ticker_does_not_reopen_the_history_to_reimport(archive):
    """The de-duplication key embeds the ticker, so a rename must rewrite it.

    Without that every stored movement becomes invisible to the next import and
    the whole history is added a second time under the new name — which is
    exactly what happened: 92 Medical Properties movements became 184.
    """
    from app.importer.service import reconcile_ticker_aliases

    db, portfolio = archive
    asset = db.scalar(select(Asset).where(Asset.ticker.in_(("MPT", "MPW"))))
    before = db.scalar(
        select(func.count(Transaction.id)).where(Transaction.asset_id == asset.id)
    )
    assert before > 0

    # Rename, then replay the whole archive exactly as a restart would.
    reconcile_ticker_aliases(db)
    _import_archive(db, portfolio)

    asset = db.scalar(select(Asset).where(Asset.ticker == "MPT"))
    after = db.scalar(
        select(func.count(Transaction.id)).where(Transaction.asset_id == asset.id)
    )
    assert after == before, f"re-import after rename added {after - before} movements"


@requires_statements
def test_offshore_holdings_never_land_in_a_domestic_family(archive):
    """Nothing bought in dollars may be filed under a B3 family.

    The domestic/offshore split is the point of the allocation chart, and it
    only holds if the two never mix — a single US share classified as ``STOCK``
    would silently be counted as Brazilian.
    """
    db, _ = archive
    domestic = {"STOCK", "ETF", "FII", "BDR", "UNIT"}
    misfiled = [
        f"{ticker} ({kind})"
        for ticker, kind, currency in db.execute(
            select(Asset.ticker, Asset.kind, Asset.currency)
        ).all()
        if currency != "BRL" and kind in domestic
    ]
    assert not misfiled, "offshore assets in a domestic family: " + ", ".join(misfiled)


@requires_statements
def test_archive_never_creates_a_provisional_ticker(archive):
    """Every security in the archive is identified by symbol, CUSIP or name."""
    db, _ = archive
    provisional = [t for t in db.scalars(select(Asset.ticker)).all() if t.startswith("?")]
    assert not provisional


@requires_statements
def test_archive_custody_transfers_cancel_out(archive):
    """Nomad's migration moved shares out on one day and in on the next.

    Applying both would strip the cost basis, so the engine pairs them and
    neutralises both sides even though the dates differ.
    """
    db, portfolio = archive
    service = PortfolioService(db, portfolio.id)
    transfers = [
        movement
        for movement in service.movements()
        if movement.op_type in (OperationType.TRANSFER_IN.value, OperationType.TRANSFER_OUT.value)
    ]
    assert transfers, "the archive contains custody transfers"
    # Every position still has cost after the migration.
    for position in service.positions().values():
        if position.is_open:
            assert position.cost_basis > 0
