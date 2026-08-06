"""Number and date parsing for broker statements.

These are the cases that made the parsers wrong before they were handled, so
each one is a regression guard rather than a general exercise of the API.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.importer.pdf.values import parse_date, parse_money, parse_number, parse_period


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # en-US and pt-BR, unambiguous because both separators are present.
        ("1,234.56", "1234.56"),
        ("1.234,56", "1234.56"),
        ("$ 3,801.58", "3801.58"),
        ("R$ 1.234,56", "1234.56"),
        # A lone separator with a non-thousands tail is always a decimal mark,
        # whatever the document's locale: Apex Ascend's activity table prints
        # "626.97" two pages after its portfolio prints "1.274,58".
        ("626.97", "626.97"),
        ("2.09797", "2.09797"),
        ("878,44", "878.44"),
        # Thousands separator *and* decimal point, both dots — the shape Apex
        # Ascend used for November 2025.
        ("1.315.39", "1315.39"),
        # Parentheses mark a debit in the DriveWealth statements.
        ("(3.36)", "-3.36"),
        ("(1,804.21)", "-1804.21"),
        # Negative quantities: an outbound custody transfer.
        ("-16.51202", "-16.51202"),
    ],
)
def test_parse_number_reads_the_separator_layout(text: str, expected: str) -> None:
    assert parse_number(text) == Decimal(expected)


def test_a_lone_separator_with_three_digits_falls_back_to_the_locale() -> None:
    """``1.234`` is the one genuinely ambiguous shape, so the hint decides.

    In en-US the dot is the decimal mark and the comma groups thousands; in
    pt-BR it is the other way round. Every other shape is settled by the token
    itself, which is why this is the only place the hint is consulted.
    """
    assert parse_number("1.234", prefer="en") == Decimal("1.234")
    assert parse_number("1.234", prefer="pt") == Decimal("1234")
    assert parse_number("1,234", prefer="en") == Decimal("1234")
    assert parse_number("1,234", prefer="pt") == Decimal("1.234")


@pytest.mark.parametrize("text", ["", "-", "--", "N/A", "nenhum", None])
def test_missing_values_parse_as_none(text: str | None) -> None:
    assert parse_money(text) is None


def test_date_order_is_explicit_because_the_same_day_reads_two_ways() -> None:
    """Apex writes January 5th as ``01/05/26``; Avenue writes ``05/01/2026``."""
    assert parse_date("01/05/26", "mdy") == date(2026, 1, 5)
    assert parse_date("05/01/2026", "dmy") == date(2026, 1, 5)
    assert parse_date("2026-01-05", "iso") == date(2026, 1, 5)


def test_auto_date_order_resolves_what_it_can() -> None:
    # 16 cannot be a month, so this can only be day-first.
    assert parse_date("16/01/2026") == date(2026, 1, 16)
    # Ambiguous: defaults to month-first, which is why parsers state the order.
    assert parse_date("05/01/2026") == date(2026, 5, 1)


def test_impossible_dates_are_rejected_rather_than_clamped() -> None:
    assert parse_date("31/02/2026", "dmy") is None
    assert parse_date("not a date") is None


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("January 01, 2026 - January 31, 2026", date(2026, 1, 1), date(2026, 1, 31)),
        ("May 1, 2021 - May 31, 2021", date(2021, 5, 1), date(2021, 5, 31)),
        ("Data do extrato: 2025-11-01 - 2025-11-30", date(2025, 11, 1), date(2025, 11, 30)),
    ],
)
def test_statement_periods_are_found_in_all_three_shapes(text, start, end) -> None:
    assert parse_period(text) == (start, end)
