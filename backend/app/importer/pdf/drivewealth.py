"""DriveWealth "Account Statement" — Avenue 2020-2021 and Nomad 2025.

Both brokers white-labelled the same DriveWealth statement, so one parser reads
them; only the letterhead differs, which is what :meth:`matches` keys on.

The activity table is a flat, one-row-per-movement grid::

    Trade Date Settle Date Currency Activity Type Symbol / Description Quantity Price Amount

with the symbol embedded in the description (``NKE - NIKE INC CL B - TRD ...``)
and debits in parentheses. Long descriptions wrap, so rows are grouped by
"starts with a trade date" rather than by line.
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.domain.enums import Direction
from app.importer.pdf import movements
from app.importer.pdf.base import ParsedStatement, StatementParser, StatementRow
from app.importer.pdf.holdings import read_holdings
from app.importer.pdf.layout import ColumnMap, Document, Line
from app.importer.pdf.values import parse_date, parse_number

ACTIVITY_HEADER = {
    "quantity": "Quantity",
    "price": "Price",
    "amount": "Amount",
}
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
#: ``SYM - DESCRIPTION - free text``; the trailing text is broker commentary.
_SYMBOL_RE = re.compile(r"^([A-Z][A-Z0-9.]{0,5})\s+-\s+(.*)$")

#: DriveWealth activity codes seen across the corpus.
#: ``DWBDS`` money-market rows are the sweep account and carry no real position.
TRADE_CODES = {"BUY", "SELL"}
DIVIDEND_CODES = {"DIV"}
WITHHOLDING_CODES = {"DIVNRA"}
#: ACATS moves shares between custodians; ACATC/JNLC/CSD move only cash.
TRANSFER_CODES = {"ACATS"}
CASH_CODES = {"JNLC", "ACATC", "CSD", "JNLS", "WIRE", "ACH"}
#: The money-market sweep vehicle — cash dressed up as a holding.
SWEEP_SYMBOLS = {"DWBDS"}


class DriveWealthParser(StatementParser):
    format = "drivewealth"
    broker = ""  # resolved per document: Avenue or Nomad

    def matches(self, document: Document) -> bool:
        head = document.head
        return "Account Statement" in head and (
            "DriveWealth" in document.text or "Valuation Summary" in head
        )

    def parse(self, document: Document) -> ParsedStatement:
        head = document.head
        broker = "Nomad" if "NOMAD" in head.upper() else "Avenue"
        institution = "Nomad (DriveWealth)" if broker == "Nomad" else "Avenue Securities (DriveWealth)"
        period = document.period()

        statement = ParsedStatement(
            format=self.format,
            broker=broker,
            institution_raw=institution,
            currency="USD",
            period_start=period[0] if period else None,
            period_end=period[1] if period else None,
            account_ref=_account_number(head),
            opening_balance=_balance(head, "Beginning Account Value"),
            closing_balance=_balance(head, "Ending Account Value"),
            holdings=read_holdings(document),
        )

        columns: ColumnMap | None = None
        section = ""
        block: list[Line] = []

        def flush() -> None:
            if block:
                row = _build_row(block, columns, section)
                if row is not None:
                    statement.rows.append(row)
                block.clear()

        for line in document.lines():
            header = ColumnMap.from_line(line, ACTIVITY_HEADER)
            if header is not None:
                flush()
                columns = header
                continue
            if line.key in {"activity", "sweepactivity", "holdings", "balances"}:
                flush()
                section = line.text.strip()
                continue
            if columns is None:
                continue
            if line.words and _DATE_RE.match(line.words[0].text):
                flush()
                block.append(line)
            elif block:
                block.append(line)
        flush()
        return statement


def _account_number(text: str) -> str:
    match = re.search(r"Account Number\s*:?\s*([A-Z0-9-]+)", text)
    return match.group(1) if match else ""


def _balance(text: str, label: str) -> Decimal | None:
    match = re.search(rf"{re.escape(label)}\s+\$?\(?([\d.,]+)\)?", text)
    return parse_number(match.group(1)) if match else None


def _build_row(block: list[Line], columns: ColumnMap | None, section: str) -> StatementRow | None:
    if not block or columns is None:
        return None
    first = block[0]
    words = first.words
    if len(words) < 4:
        return None

    trade_date = parse_date(words[0].text, "mdy")
    if trade_date is None:
        return None
    settle_date = parse_date(words[1].text, "mdy")
    # words[2] is the currency ("USD"); the activity code follows it.
    code = words[3].text.upper()

    quantity = _column_value(block, columns, "quantity") or Decimal(0)
    price = _column_value(block, columns, "price")
    amount = _column_value(block, columns, "amount") or Decimal(0)

    description = _description(block, columns)
    symbol, name = _split_symbol(description)
    if symbol in SWEEP_SYMBOLS:
        # The DW Bank Sweep is the cash account wearing a ticker.
        symbol, name = "", description

    movement, direction = _interpret(code, symbol, quantity, amount)
    return StatementRow(
        trade_date=trade_date,
        settle_date=settle_date,
        movement=movement,
        direction=direction,
        amount=abs(amount),
        quantity=abs(quantity),
        unit_price=price,
        symbol=symbol,
        description=name,
        section=section or "ACTIVITY",
        raw_text=" / ".join(line.text for line in block),
        page_number=first.page_number,
    )


def _interpret(
    code: str, symbol: str, quantity: Decimal, amount: Decimal
) -> tuple[str, Direction]:
    """Map an activity code onto a canonical movement plus its direction."""
    if not symbol or code in CASH_CODES:
        return movements.CASH_MOVEMENT, _sign(amount)
    if code in TRADE_CODES:
        # The amount's sign is authoritative: a "SELL" of dust settles at 0.00
        # and a "BUY" always leaves cash, so quantity alone would mislead.
        if code == "BUY":
            return movements.BUY, Direction.DEBIT
        return movements.SELL, Direction.CREDIT
    if code in DIVIDEND_CODES:
        return movements.DIVIDEND, _sign(amount)
    if code in WITHHOLDING_CODES:
        # Withholding is printed as a negative amount; a reversal is positive.
        return movements.DIVIDEND_TAX, _sign(amount)
    if code in TRANSFER_CODES:
        # Outbound custody moves carry a negative quantity.
        return movements.CUSTODY_TRANSFER, Direction.DEBIT if quantity < 0 else Direction.CREDIT
    return movements.CASH_MOVEMENT, _sign(amount)


def _sign(amount: Decimal) -> Direction:
    return Direction.DEBIT if amount < 0 else Direction.CREDIT


def _column_value(block: list[Line], columns: ColumnMap, name: str) -> Decimal | None:
    column = columns.get(name)
    if column is None:
        return None
    for line in block:
        for word in line.words:
            if columns.assign(word) != name:
                continue
            value = parse_number(word.text)
            if value is not None:
                return value
    return None


def _description(block: list[Line], columns: ColumnMap) -> str:
    """Everything left of the numeric columns, across the wrapped lines."""
    quantity = columns.get("quantity")
    limit = quantity.x0 - 2 if quantity else 10_000.0
    parts: list[str] = []
    for index, line in enumerate(block):
        words = line.before(limit)
        if index == 0:
            words = words[4:]  # skip trade date, settle date, currency, code
        parts.extend(word.text for word in words)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _split_symbol(description: str) -> tuple[str, str]:
    """``"NKE - NIKE INC CL B - TRD ..."`` -> ``("NKE", "NIKE INC CL B")``."""
    match = _SYMBOL_RE.match(description)
    if not match:
        return "", description
    symbol = match.group(1).upper()
    remainder = match.group(2)
    name = remainder.split(" - ")[0].strip()
    return symbol, name or description
