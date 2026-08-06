"""Avenue's own statement (``Stmt_YYYYMMDD.pdf``) — 2025-01 onwards.

Same ledger skeleton as the Apex statement it replaced, but Avenue-branded and
with Portuguese descriptions. Worth reading *in addition to* the Apex file for
the same month: Avenue reports the whole account, while the Apex statement only
covers what Apex custodies, so it silently omits positions held at Velocity.

Two traps:

* dates are ``dd/mm/yyyy`` here and ``mm/dd/yy`` in the Apex file — the same
  trade is "05/01/2026" in one and "01/05/26" in the other;
* the ticker is not on the transaction line. It sits on the following line,
  inside the description column, which is why the parser reads a whole block
  rather than a line.
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.domain.enums import Direction
from app.importer.pdf import movements
from app.importer.pdf.base import ParsedStatement, SectionTotals, StatementParser, StatementRow
from app.importer.pdf.layout import Document, normalize
from app.importer.pdf.holdings import read_holdings
from app.importer.pdf.ledger import LedgerBlock, read_ledger
from app.importer.pdf.values import parse_date, parse_number

ROW_TYPES = frozenset({"BOUGHT", "SOLD", "DIVIDEND", "FEE", "JOURNAL", "INTEREST"})

SECTIONS = {
    "buyselltransactions": "BUY / SELL TRANSACTIONS",
    "dividendsandinterest": "DIVIDENDS AND INTEREST",
    "fundspaidandreceived": "FUNDS PAID AND RECEIVED",
    "miscellaneoustransactions": "MISCELLANEOUS TRANSACTIONS",
    "portfoliosummary": "PORTFOLIO SUMMARY",
}

_ACCOUNT_RE = re.compile(r"AVENUE ACCOUNT NUMBER:\s*([0-9A-Z-]+)")
_EQUITY_RE = re.compile(r"Total Equity Holdings\s*\$?\s*([\d.,]+)\s*\$?\s*([\d.,]+)")
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,5}$")

#: Portuguese descriptions -> what actually happened. Order matters: "estorno"
#: (a reversal) must be tested before the withholding it reverses.
_DESCRIPTIONS: tuple[tuple[str, str], ...] = (
    ("estornoretencaoimpostos", movements.DIVIDEND_TAX),
    ("retencaoimpostos", movements.DIVIDEND_TAX),
    ("creditodividendos", movements.DIVIDEND),
    ("creditojuros", movements.INTEREST),
    ("debitocompradeativo", movements.BUY),
    ("creditovendadeativo", movements.SELL),
)


class AvenuePortugueseParser(StatementParser):
    format = "avenue-pt"
    broker = "Avenue"

    def matches(self, document: Document) -> bool:
        head = document.head
        return "customer@avenue.us" in head and "AVENUE ACCOUNT NUMBER" in document.text

    def parse(self, document: Document) -> ParsedStatement:
        head = document.head
        period = document.period()
        opening, closing = _balances(head)

        statement = ParsedStatement(
            format=self.format,
            broker=self.broker,
            institution_raw="Avenue Securities",
            currency="USD",
            period_start=period[0] if period else None,
            period_end=period[1] if period else None,
            account_ref=_account(document.text),
            opening_balance=opening,
            closing_balance=closing,
            holdings=read_holdings(document),
        )

        blocks, totals = read_ledger(document, ROW_TYPES, SECTIONS)
        for block in blocks:
            row = _row_from(block, statement)
            if row is not None:
                statement.rows.append(row)

        _attach_totals(statement, totals)
        return statement


def _account(text: str) -> str:
    match = _ACCOUNT_RE.search(text)
    return match.group(1) if match else ""


def _balances(text: str) -> tuple[Decimal | None, Decimal | None]:
    match = _EQUITY_RE.search(text.replace("\n", " "))
    if not match:
        return None, None
    return parse_number(match.group(1)), parse_number(match.group(2))


def _row_from(block: LedgerBlock, statement: ParsedStatement) -> StatementRow | None:
    trade_date = parse_date(block.date_text, "dmy")
    if trade_date is None:
        statement.warnings.append(f"data ilegível em '{block.raw_text[:80]}'")
        return None

    description = block.description()
    symbol = _extract_symbol(block)
    debits = block.column_values("debit")
    credits = block.column_values("credit")
    amount = credits[0] if credits else (debits[0] if debits else Decimal(0))
    direction = Direction.CREDIT if credits else Direction.DEBIT

    movement = _movement_for(block, description, direction)
    return StatementRow(
        trade_date=trade_date,
        movement=movement,
        direction=direction,
        amount=abs(amount),
        quantity=abs(block.first("quantity") or Decimal(0)),
        unit_price=block.first("price"),
        symbol=symbol,
        description=description,
        section=block.section,
        raw_text=block.raw_text,
        page_number=block.page_number,
    )


def _movement_for(block: LedgerBlock, description: str, direction: Direction) -> str:
    code = block.type_code
    if code == "BOUGHT":
        return movements.BUY
    if code == "SOLD":
        return movements.SELL
    if code == "FEE":
        return movements.ADR_FEE if block.has_text("ADR") else movements.FEE
    if code == "JOURNAL":
        return movements.CASH_MOVEMENT

    key = normalize(description)
    for needle, movement in _DESCRIPTIONS:
        if needle in key:
            return movement
    if code == "DIVIDEND":
        # Unrecognised wording: the column still says which way the cash went,
        # and a debit inside a dividend section is always the withholding.
        return movements.DIVIDEND if direction is Direction.CREDIT else movements.DIVIDEND_TAX
    return movements.CASH_MOVEMENT


def _extract_symbol(block: LedgerBlock) -> str:
    """The ticker sits alone on a continuation line inside the description column."""
    for line in block.lines[1:]:
        words = line.between(block.description_x0, block.description_x0 + 120)
        if len(words) == 1 and _TICKER_RE.match(words[0].text):
            return words[0].text.upper()
    return ""


def _attach_totals(statement: ParsedStatement, totals) -> None:
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
