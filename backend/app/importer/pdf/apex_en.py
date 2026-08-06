"""Apex Clearing "ACCOUNT STATEMENT" (English) — Avenue, 2021-05 onwards.

The only source covering Avenue between May 2021 and December 2024, so it has
to be read even though it is the harder of the two Avenue formats:

* transaction rows carry **no ticker** — only a description and a ``CUSIP:``
  line, so the symbol is resolved downstream (see :mod:`app.importer.pdf.symbols`);
* a dividend and its withholding share one block, the withholding hiding on a
  continuation line as ``WH 1.95`` in the debit column;
* the *pending settlement* table repeats trades that the following month's
  statement books for real — it is skipped, or every month-end trade would be
  counted twice.
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.domain.enums import Direction
from app.importer.pdf import movements
from app.importer.pdf.base import ParsedStatement, SectionTotals, StatementParser, StatementRow
from app.importer.pdf.layout import Document
from app.importer.pdf.holdings import read_holdings
from app.importer.pdf.ledger import LedgerBlock, read_ledger
from app.importer.pdf.values import parse_date, parse_number

ROW_TYPES = frozenset(
    {"BOUGHT", "SOLD", "DIVIDEND", "JOURNAL", "CNV", "MERGER", "ADR", "INTEREST", "FEE", "NRA"}
)

SECTIONS = {
    "buyselltransactions": "BUY / SELL TRANSACTIONS",
    "dividendsandinterest": "DIVIDENDS AND INTEREST",
    "fundspaidandreceived": "FUNDS PAID AND RECEIVED",
    "securitiesreceivedanddelivered": "SECURITIES RECEIVED AND DELIVERED",
    "miscellaneoustransactions": "MISCELLANEOUS TRANSACTIONS",
    "tradesettlementaccount": "TRADE SETTLEMENT ACCOUNT",
    "portfoliosummary": "PORTFOLIO SUMMARY",
}
#: Trades already listed here reappear, settled, in the next statement.
SKIP_SECTIONS = frozenset({"TRADE SETTLEMENT ACCOUNT"})

_ACCOUNT_RE = re.compile(r"ACCOUNT NUMBER\s+([A-Z0-9-]+)")
_BALANCE_RE = re.compile(r"Total Equity Holdings\s*\$?([\d.,]+)\s*\$?([\d.,]+)")
#: Withholding tucked onto a continuation line: "CASH DIV ON  WH 0.20".
_WITHHOLDING_MARKERS = ("WH", "NONRESTAXWITHHELD", "NRAWITHHELD")


class ApexEnglishParser(StatementParser):
    format = "apex-en"
    broker = "Avenue"

    def matches(self, document: Document) -> bool:
        head = document.head
        return "Apex Clearing Corporation" in head and "ACCOUNT NUMBER" in head

    def parse(self, document: Document) -> ParsedStatement:
        head = document.head
        period = document.period()
        opening, closing = _balances(head)

        statement = ParsedStatement(
            format=self.format,
            broker=self.broker,
            institution_raw="Avenue Securities (Apex Clearing)",
            currency="USD",
            period_start=period[0] if period else None,
            period_end=period[1] if period else None,
            account_ref=_account(head),
            opening_balance=opening,
            closing_balance=closing,
            holdings=read_holdings(document),
        )

        blocks, totals = read_ledger(document, ROW_TYPES, SECTIONS, SKIP_SECTIONS)
        for block in blocks:
            statement.rows.extend(_rows_from(block, statement))

        _attach_totals(statement, totals)
        return statement


def _account(text: str) -> str:
    match = _ACCOUNT_RE.search(text)
    return match.group(1) if match else ""


def _balances(text: str) -> tuple[Decimal | None, Decimal | None]:
    match = _BALANCE_RE.search(text.replace("\n", " "))
    if not match:
        return None, None
    return parse_number(match.group(1)), parse_number(match.group(2))


def _rows_from(block: LedgerBlock, statement: ParsedStatement) -> list[StatementRow]:
    trade_date = parse_date(block.date_text, "mdy")
    if trade_date is None:
        statement.warnings.append(f"data ilegível em '{block.raw_text[:80]}'")
        return []

    description = block.description()
    quantity = block.first("quantity") or Decimal(0)
    price = block.first("price")
    debits = block.column_values("debit")
    credits = block.column_values("credit")
    code = block.type_code

    def row(movement: str, direction: Direction, amount: Decimal, qty: Decimal = Decimal(0)):
        return StatementRow(
            trade_date=trade_date,
            movement=movement,
            direction=direction,
            amount=abs(amount),
            quantity=abs(qty),
            unit_price=price,
            symbol="",
            description=description,
            cusip=block.cusip(),
            section=block.section,
            raw_text=block.raw_text,
            page_number=block.page_number,
        )

    if code == "BOUGHT":
        return [row(movements.BUY, Direction.DEBIT, _first(debits), quantity)]
    if code == "SOLD":
        return [row(movements.SELL, Direction.CREDIT, _first(credits), quantity)]

    if code == "MERGER":
        # Apex debits the old line and credits the new one, both at zero cost.
        direction = Direction.DEBIT if quantity < 0 else Direction.CREDIT
        return [row(movements.MERGER, direction, Decimal(0), quantity)]

    if code == "CNV":
        if block.section == "SECURITIES RECEIVED AND DELIVERED":
            direction = Direction.DEBIT if quantity < 0 else Direction.CREDIT
            return [row(movements.CUSTODY_TRANSFER, direction, Decimal(0), quantity)]
        return [_cash(block, trade_date, description, debits, credits)]

    if code == "ADR" or (code in {"JOURNAL", "FEE"} and block.has_text("ADR Fee", "AGENCY PROCESSING FEE")):
        return [row(movements.ADR_FEE, Direction.DEBIT, _first(debits))]

    if code == "NRA":
        # "NRA ADJ 2023" — the IRS reclassified a distribution and Apex refunds
        # part of the withholding it took, sometimes years later.
        return [row(movements.DIVIDEND_TAX, Direction.CREDIT, _first(credits))]

    if code == "DIVIDEND":
        return _dividend_rows(block, row, debits, credits)

    if code == "INTEREST":
        return [row(movements.INTEREST, Direction.CREDIT, _first(credits))]

    return [_cash(block, trade_date, description, debits, credits)]


def _dividend_rows(block: LedgerBlock, row, debits: list[Decimal], credits: list[Decimal]):
    """Split a dividend block into its gross payment and its withholding.

    Apex prints them together — the credit is the gross dividend, any debit in
    the same block is the non-resident withholding — except in "Funds Paid and
    Received", where the two sides are separate blocks and only the description
    says which is which.
    """
    rows = []
    withholding_only = block.has_text(*_WITHHOLDING_MARKERS) and not credits
    for amount in credits:
        # A credit inside a withholding block is a reversal of that tax.
        movement = movements.DIVIDEND_TAX if withholding_only else movements.DIVIDEND
        rows.append(row(movement, Direction.CREDIT, amount))
    for amount in debits:
        rows.append(row(movements.DIVIDEND_TAX, Direction.DEBIT, amount))
    if not rows:
        rows.append(row(movements.DIVIDEND, Direction.CREDIT, Decimal(0)))
    return rows


def _cash(block: LedgerBlock, trade_date, description: str, debits, credits) -> StatementRow:
    amount = _first(credits) if credits else -_first(debits)
    return StatementRow(
        trade_date=trade_date,
        movement=movements.CASH_MOVEMENT,
        direction=Direction.CREDIT if amount >= 0 else Direction.DEBIT,
        amount=abs(amount),
        description=description,
        cusip=block.cusip(),
        section=block.section,
        raw_text=block.raw_text,
        page_number=block.page_number,
    )


def _first(values: list[Decimal]) -> Decimal:
    return values[0] if values else Decimal(0)


def _attach_totals(statement: ParsedStatement, totals) -> None:
    """Pair each printed section total with what the parser actually read."""
    parsed: dict[str, SectionTotals] = {}
    for row in statement.rows:
        entry = parsed.setdefault(row.section, SectionTotals(section=row.section))
        if row.direction is Direction.CREDIT:
            entry.parsed_credit += row.amount
        else:
            entry.parsed_debit += row.amount

    for total in totals:
        entry = parsed.setdefault(total.section, SectionTotals(section=total.section))
        entry.printed_debit = total.debit
        entry.printed_credit = total.credit
    statement.totals = list(parsed.values())
