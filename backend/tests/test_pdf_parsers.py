"""Statement parsing, checked against the real files when they are available.

Every one of these statements prints its own control totals and its own list of
holdings, so the strongest possible test is to parse the file and compare with
what the broker says. That is what :func:`test_every_statement_reconciles` does
across the whole archive; the rest pin down the specific behaviours that were
wrong at some point and would be easy to break again.

The tests skip when ``data/`` is absent, exactly as the B3 CSV tests do.
"""
from __future__ import annotations

from decimal import Decimal

from app.importer.pdf import parse_pdf
from app.importer.pdf.movements import CASH_MOVEMENT, DIVIDEND, DIVIDEND_TAX
from app.importer.pdf.symbols import canonical_ticker, resolve_ticker
from tests.conftest import STATEMENT_FILES, parsed_statements, requires_statements


@requires_statements
def test_every_statement_is_recognised_by_some_parser() -> None:
    unknown = []
    for path in STATEMENT_FILES:
        try:
            parse_pdf(path.read_bytes())
        except Exception as exc:  # noqa: BLE001 — the failure list is the message
            unknown.append(f"{path.name}: {exc}")
    assert not unknown, "statements no parser handles:\n" + "\n".join(unknown)


@requires_statements
def test_every_statement_reconciles_with_its_own_printed_totals() -> None:
    """The check that makes a silent mis-parse impossible.

    A number read in the wrong locale, a row lost to a page break or an amount
    put in the wrong column all show up here as a section whose parsed sum
    disagrees with the total the broker printed at the bottom of it.
    """
    problems: list[str] = []
    for path, statement in parsed_statements():
        problems.extend(f"{path.name}: {issue}" for issue in statement.reconciliation_warnings())
    assert not problems, "statements that do not reconcile:\n" + "\n".join(problems)


@requires_statements
def test_every_statement_has_a_period_and_a_broker() -> None:
    for path, statement in parsed_statements():
        assert statement.period_start is not None, path.name
        assert statement.period_end is not None, path.name
        assert statement.period_start <= statement.period_end, path.name
        assert statement.broker, path.name
        assert statement.currency == "USD", path.name


@requires_statements
def test_asset_rows_resolve_to_a_ticker() -> None:
    """A movement that changes a position must name a security.

    Cash-only rows are allowed not to: Avenue's April 2025 statement lists
    withholding reversals with the ticker column blank, and the importer
    reports those rather than inventing an asset.
    """
    unresolved: list[str] = []
    for path, statement in parsed_statements():
        for row in statement.rows:
            if row.movement == CASH_MOVEMENT or not row.quantity:
                continue
            if resolve_ticker(row.symbol, row.cusip, row.description) is None:
                unresolved.append(f"{path.name}: {row.raw_text[:80]}")
    assert not unresolved, "position-changing rows with no ticker:\n" + "\n".join(unresolved[:20])


@requires_statements
def test_pending_settlement_rows_are_not_imported() -> None:
    """Apex lists a month-end trade as pending, then again as settled.

    Both statements are imported, so reading the pending table would book the
    same trade twice — with different dates, which no de-duplication can catch.
    """
    for path, statement in parsed_statements():
        sections = {row.section for row in statement.rows}
        assert not any("PENDING" in section.upper() for section in sections), path.name
        assert "TRADE SETTLEMENT ACCOUNT" not in sections, path.name


@requires_statements
def test_dividends_are_split_from_their_withholding() -> None:
    """Income and tax are separate rows, in every format.

    Apex Ascend reports only the net credit, so the parser reconstructs the
    gross and the tax from the description; the other three print both.
    """
    formats_with_tax: set[str] = set()
    for path, statement in parsed_statements():
        if any(row.movement == DIVIDEND_TAX for row in statement.rows):
            formats_with_tax.add(statement.format)
    assert formats_with_tax == {"apex-en", "apex-ascend", "avenue-pt", "drivewealth"}


#: A statement that lists no position must also be worth ~nothing. The bound is
#: not zero because a wound-down account keeps a few dollars of money-market
#: cash, which is a balance and not a holding.
EMPTY_ACCOUNT_LIMIT = Decimal(50)


@requires_statements
def test_a_statement_reports_holdings_unless_the_account_was_empty() -> None:
    """The holdings table is what the position check compares against.

    Three statements in this archive legitimately list nothing: the account had
    just been emptied into another custodian, leaving only sweep cash. Any
    *other* statement with no holdings is a table the parser failed to read.
    """
    unexplained = [
        f"{path.name} (fecha em {statement.closing_balance})"
        for path, statement in parsed_statements()
        if not statement.holdings
        and (statement.closing_balance is None or statement.closing_balance > EMPTY_ACCOUNT_LIMIT)
    ]
    assert not unexplained, "statements whose holdings were not read:\n" + "\n".join(unexplained)


@requires_statements
def test_holdings_are_read_from_the_bulk_of_the_archive() -> None:
    """Smoke test for the opposite failure: silently reading none at all.

    A regression in the holdings reader would not fail the check above — a
    statement with no holdings and no balance parsed looks the same as an empty
    account — so the overall hit rate is asserted separately.
    """
    total = len(parsed_statements())
    with_holdings = sum(1 for _, statement in parsed_statements() if statement.holdings)
    assert with_holdings >= total * 0.9, f"only {with_holdings}/{total} statements yielded holdings"


@requires_statements
def test_holding_quantities_are_positive_numbers() -> None:
    for path, statement in parsed_statements():
        for holding in statement.holdings:
            assert holding.quantity > 0, f"{path.name}: {holding.symbol}"


def test_ticker_aliases_fold_a_renamed_company_onto_its_current_symbol() -> None:
    """Statements print MPW and BK; the market now says MPT and BNY.

    Without the alias the same holding splits into two assets, each with half
    the history — and the new symbol wins, because that is the one the broker
    and the quote provider use today.
    """
    assert canonical_ticker("MPW") == "MPT"
    assert canonical_ticker("MPT") == "MPT"
    assert canonical_ticker("BK") == "BNY"
    assert canonical_ticker("nke") == "NKE"


def test_reits_are_recognised_by_ticker_not_by_name() -> None:
    """Being a REIT is a tax structure, and the name rarely gives it away.

    "SL Green Realty", "STAG Industrial" and "W. P. Carey" are all REITs whose
    descriptions say nothing of the sort, which is why a name heuristic on its
    own filed them under stocks.
    """
    from app.importer.service import classify_us_asset_kind

    for ticker, description in (
        ("SLG", "SL GREEN REALTY CORP"),
        ("STAG", "STAG INDUSTRIAL INC"),
        ("WPC", "W P CAREY INC"),
        ("MAC", "MACERICH CO"),
        ("STOR", "STORE CAPITAL CORPORATION"),
        ("RC", "READY CAPITAL CORPORATION"),
        ("O", "REALTY INCOME CORP"),
        ("MPT", "MEDICAL PROPERTIES TRUST INC"),
    ):
        assert classify_us_asset_kind(ticker, description).value == "REIT", ticker

    # The old spelling resolves too, since the alias runs first.
    assert classify_us_asset_kind("MPW", "MEDICAL PROPERTIES TRUST INC").value == "REIT"

    # A real-estate *fund* is a fund, not a REIT.
    assert classify_us_asset_kind("VNQ", "VANGUARD SPECIALIZED FUNDS REAL ESTATE ETF").value == "ETF_INTL"
    # And an ordinary share stays one.
    assert classify_us_asset_kind("NKE", "NIKE INC CL B").value == "STOCK_INTL"


def test_cusip_resolves_a_ticker_the_statement_never_prints() -> None:
    """Apex's English statements identify securities by CUSIP only."""
    assert resolve_ticker("", "756109104", "REALTY INCOME CORP") == "O"
    assert resolve_ticker("", "", "REALTY INCOME CORP") == "O"
    assert resolve_ticker("", "", "SOMETHING NOBODY HAS HEARD OF") is None


def test_learned_cusips_take_precedence_over_the_seed_table() -> None:
    """A mapping read from a real statement beats the built-in guess."""
    learned = {"999999999": "NEWCO"}
    assert resolve_ticker("", "999999999", "NEW COMPANY INC", learned) == "NEWCO"


@requires_statements
def test_amounts_are_never_negative_because_direction_carries_the_sign() -> None:
    """Statement rows mirror the B3 CSV: a magnitude plus a direction."""
    for path, statement in parsed_statements():
        for row in statement.rows:
            assert row.amount >= 0, f"{path.name}: {row.raw_text[:80]}"
            assert row.quantity >= 0, f"{path.name}: {row.raw_text[:80]}"


@requires_statements
def test_dividend_amounts_are_plausible() -> None:
    """Guards against a decimal-separator slip turning $8.50 into $850."""
    for path, statement in parsed_statements():
        for row in statement.rows:
            if row.movement in (DIVIDEND, DIVIDEND_TAX):
                assert row.amount < Decimal(10_000), f"{path.name}: {row.raw_text[:80]}"
