"""Apex Ascend statement — Nomad, 2025-11 onwards, Portuguese *and* English.

Nomad migrated off DriveWealth on 2025-11-08, so this format picks up mid-month
where :mod:`app.importer.pdf.drivewealth` leaves off: the two November 2025
statements are halves of one month, not duplicates of each other.

Nomad then switched the same statement from Portuguese to English in February
2026. Only the labels changed, so one parser reads both — every heading and
column below is declared as a pair.

Three things make this the most awkward of the formats:

* **Number formats drift inside a single document.** The portfolio table prints
  ``1.274,58`` (pt-BR) while the activity table prints ``1.315.39`` for the same
  kind of value. :func:`~app.importer.pdf.values.parse_number` reads the
  separator layout instead of trusting a locale.
* **Dividends are reported net of withholding**, unlike every other format,
  which reports the gross payment and the tax separately. The gross is
  recoverable from the description, so both sides are reconstructed.
* **The symbol column is left-aligned** while every numeric column is
  right-aligned, and the ticker's own description is full of words that look
  like tickers (``NIKE INC CL B`` → ``NKE``), so the ticker is taken from the
  column's x-range rather than by shape.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.domain.enums import Direction
from app.importer.pdf import movements
from app.importer.pdf.base import ParsedStatement, StatementParser, StatementRow
from app.importer.pdf.holdings import read_holdings
from app.importer.pdf.layout import ColumnMap, Document, Line, Word, normalize
from app.importer.pdf.values import parse_date, parse_number

SECTION_TRADING = "TRADING ACTIVITIES"
SECTION_NON_TRADING = "NON-TRADING ACTIVITY"
#: Repeated, settled, in the following statement — importing it double-counts
#: every trade that straddles a month end.
SECTION_PENDING = "TRADING ACTIVITY PENDING SETTLEMENT"

#: Heading (accent-free, alphanumeric) -> canonical section, both languages.
HEADINGS = {
    "atividadesdenegociacao": SECTION_TRADING,
    "tradingactivities": SECTION_TRADING,
    "atividadedenegociacaopendentedeliquidacao": SECTION_PENDING,
    "tradingactivitypendingsettlement": SECTION_PENDING,
    "atividadenaorelacionadaanegociacao": SECTION_NON_TRADING,
    "nontradingactivity": SECTION_NON_TRADING,
    "carteira": "PORTFOLIO",
    "portfolio": "PORTFOLIO",
    "resumodaconta": "ACCOUNT SUMMARY",
    "accountsummary": "ACCOUNT SUMMARY",
}

#: Column header words, either language, as :func:`normalize` renders them —
#: "Preço($)" becomes "preco" and "líquido($)" becomes "liquido". Values are
#: right-aligned to these.
QUANTITY_LABELS = ("quantidade", "quantity")
PRICE_LABELS = ("preco", "price")
AMOUNT_LABELS = ("liquido", "amount")
#: The symbol column is left-aligned, so it is matched on its left edge.
SYMBOL_LABELS = ("simbolo", "symbol")
DESCRIPTION_LABELS = ("descricao", "description")

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CUSIP_RE = re.compile(r"^[0-9A-Z]{9}$")
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,5}$")
_TRADE_CODES = {"BUY", "SELL"}
#: "Cash Div of 0.41 on 29.61318 shs"
_DIVIDEND_RATE_RE = re.compile(r"(?i)div of ([\d.,]+) on ([\d.,]+) shs")
#: "With 30% NON Resident Alien Withholding"
_WITHHOLDING_RE = re.compile(r"(?i)with (\d{1,2}(?:[.,]\d+)?)\s*%\s*non resident alien")

_ACCOUNT_RE = re.compile(r"(?:N[úu]mero da conta|Account Number):\s*([A-Z0-9-]+)")
_TOTAL_RE = re.compile(
    r"(?i)(?:patrim[oô]nio (?:l[íi]quido )?total|total net worth)\s+([\d.,]+)\s+([\d.,]+)"
)
#: How far right of the symbol column's left edge a ticker/CUSIP may start.
SYMBOL_SPAN = 40.0


@dataclass(slots=True)
class _Table:
    """Column anchors of the activity table currently being read."""

    columns: ColumnMap
    symbol_x0: float
    description_x0: float

    def value(self, block: list[Line], name: str) -> Decimal | None:
        for line in block:
            for word in line.words:
                if self.columns.assign(word) != name:
                    continue
                parsed = parse_number(word.text)
                if parsed is not None:
                    return parsed
        return None

    def symbol_words(self, block: list[Line]) -> list[Word]:
        return [
            word
            for line in block
            for word in line.words
            if self.symbol_x0 - 4 <= word.x0 <= self.symbol_x0 + SYMBOL_SPAN
        ]

    def description(self, block: list[Line]) -> str:
        parts = [
            word.text
            for line in block
            for word in line.between(self.description_x0 - 4, self.symbol_x0 - 4)
        ]
        return re.sub(r"\s+", " ", " ".join(parts)).strip()


class ApexAscendParser(StatementParser):
    format = "apex-ascend"
    broker = "Nomad"

    def matches(self, document: Document) -> bool:
        text = document.text
        if not re.search(r"(?:Data do extrato|Statement Date):", text):
            return False
        return any(heading in normalize(text) for heading in HEADINGS)

    def parse(self, document: Document) -> ParsedStatement:
        text = document.text
        period = document.period()
        opening, closing = _balances(text)

        statement = ParsedStatement(
            format=self.format,
            broker=self.broker,
            institution_raw="Nomad (Apex Ascend)",
            currency="USD",
            period_start=period[0] if period else None,
            period_end=period[1] if period else None,
            account_ref=_account(text),
            opening_balance=opening,
            closing_balance=closing,
            holdings=read_holdings(document),
        )

        lines = document.lines()
        section = ""
        table: _Table | None = None
        block: list[Line] = []
        heading_parts = ""

        def flush() -> None:
            if block and table is not None:
                statement.rows.extend(_rows_from(list(block), table, section, statement))
            block.clear()

        page = 0
        for index, line in enumerate(lines):
            if line.page_number != page:
                # A row never spans a page — the table header is reprinted on
                # the next one. Without this the last row of a page absorbs the
                # following letterhead, and the account holder's name lands in
                # the symbol column.
                page = line.page_number
                flush()

            heading_parts, heading = _match_heading(heading_parts, line.key)
            if heading is not None:
                flush()
                section = heading
                table = None
                continue

            candidate = _read_header(line, lines[index + 1] if index + 1 < len(lines) else None)
            if candidate is not None:
                flush()
                table = candidate
                continue

            if table is None or section not in (SECTION_TRADING, SECTION_NON_TRADING):
                continue
            if _starts_row(line, section):
                flush()
                block.append(line)
            elif block:
                block.append(line)
        flush()
        return statement


def _match_heading(carried: str, key: str) -> tuple[str, str | None]:
    """Recognise a section heading that may be split across lines.

    The Portuguese layout wraps "ATIVIDADE NÃO RELACIONADA À NEGOCIAÇÃO" over
    three lines and the English one appends "(Cont'd)" when a table continues on
    the next page, so neither matches a whole heading on its own. Fragments are
    carried forward while they are still a prefix of some heading, and dropped
    as soon as they cannot become one.

    Returns the fragment to carry into the next line, plus the heading matched.
    """
    if not key:
        return carried, None
    combined = f"{carried}{key}"
    for candidate in (combined, key):
        trimmed = candidate.removesuffix("contd")
        if trimmed in HEADINGS:
            return "", HEADINGS[trimmed]
    if any(name.startswith(combined) for name in HEADINGS):
        return combined, None
    if any(name.startswith(key) for name in HEADINGS):
        return key, None
    return "", None


def _read_header(line: Line, following: Line | None) -> _Table | None:
    """Build the column map from a header row and its wrapped second line.

    "Valor líquido($)" is split across two lines in the trading table and fits
    on one in the non-trading table, so both lines are considered together.
    """
    words = list(line.words) + list(following.words if following else [])
    anchors = {normalize(word.text): word for word in words}

    quantity = _pick(anchors, QUANTITY_LABELS)
    amount = _pick(anchors, AMOUNT_LABELS)
    symbol = _pick(anchors, SYMBOL_LABELS)
    description = _pick(anchors, DESCRIPTION_LABELS)
    if quantity is None or amount is None or symbol is None or description is None:
        return None

    from app.importer.pdf.layout import Column

    columns = [
        Column(name="quantity", x0=quantity.x0, x1=quantity.x1),
        Column(name="amount", x0=amount.x0, x1=amount.x1),
    ]
    price = _pick(anchors, PRICE_LABELS)
    if price is not None:
        columns.append(Column(name="price", x0=price.x0, x1=price.x1))
    return _Table(
        columns=ColumnMap(columns=sorted(columns, key=lambda column: column.x0)),
        symbol_x0=symbol.x0,
        description_x0=description.x0,
    )


def _pick(anchors: dict[str, Word], labels: tuple[str, ...]) -> Word | None:
    return next((anchors[label] for label in labels if label in anchors), None)


def _account(text: str) -> str:
    match = _ACCOUNT_RE.search(text)
    return match.group(1) if match else ""


def _balances(text: str) -> tuple[Decimal | None, Decimal | None]:
    match = _TOTAL_RE.search(text)
    if not match:
        return None, None
    return parse_number(match.group(1)), parse_number(match.group(2))


def _starts_row(line: Line, section: str) -> bool:
    words = line.words
    if len(words) < 2:
        return False
    if section == SECTION_TRADING:
        return words[0].text.upper() in _TRADE_CODES and _ISO_RE.match(words[1].text) is not None
    return _ISO_RE.match(words[0].text) is not None


@dataclass(slots=True)
class _Identity:
    symbol: str
    cusip: str


def _identify(block: list[Line], table: _Table) -> _Identity:
    """Ticker and CUSIP, read from the symbol column rather than by shape.

    ``NIKE INC CL B`` is full of tokens that look like tickers, so only the
    column's own x-range is trusted; within it the nine-character token is the
    CUSIP and the other is the ticker.
    """
    symbol = ""
    cusip = ""
    for word in table.symbol_words(block):
        token = word.text.strip()
        if _CUSIP_RE.match(token) and any(character.isdigit() for character in token):
            cusip = cusip or token
        elif not symbol and _TICKER_RE.match(token):
            symbol = token
    return _Identity(symbol=symbol.upper(), cusip=cusip)


def _rows_from(
    block: list[Line], table: _Table, section: str, statement: ParsedStatement
) -> list[StatementRow]:
    words = block[0].words
    identity = _identify(block, table)
    description = table.description(block)
    quantity = table.value(block, "quantity") or Decimal(0)
    amount = table.value(block, "amount") or Decimal(0)

    if section == SECTION_TRADING:
        trade_date = parse_date(words[1].text, "iso")
        if trade_date is None:
            return []
        code = words[0].text.upper()
        return [
            StatementRow(
                trade_date=trade_date,
                settle_date=parse_date(words[2].text, "iso") if len(words) > 2 else None,
                movement=movements.BUY if code == "BUY" else movements.SELL,
                direction=Direction.DEBIT if code == "BUY" else Direction.CREDIT,
                amount=abs(amount),
                quantity=abs(quantity),
                unit_price=table.value(block, "price"),
                symbol=identity.symbol,
                description=description,
                cusip=identity.cusip,
                section=section,
                raw_text=_raw(block),
                page_number=block[0].page_number,
            )
        ]

    trade_date = parse_date(words[0].text, "iso")
    if trade_date is None or len(words) < 2:
        return []
    code = words[1].text.upper()

    def row(movement: str, direction: Direction, value: Decimal, qty: Decimal = Decimal(0)):
        return StatementRow(
            trade_date=trade_date,
            movement=movement,
            direction=direction,
            amount=abs(value),
            quantity=abs(qty),
            symbol=identity.symbol,
            description=description,
            cusip=identity.cusip,
            section=section,
            raw_text=_raw(block),
            page_number=block[0].page_number,
        )

    if code == "CASH_DIVIDEND":
        return _dividend_rows(row, _raw(block), amount, statement, trade_date, identity.symbol)
    if code == "STOCK_SPLIT" and identity.symbol:
        # The credited quantity is the *extra* shares, matching how the B3
        # importer models "Desdobro" — cost is preserved, the average dilutes.
        return [row(movements.SPLIT, Direction.CREDIT, Decimal(0), quantity)]
    if code == "TRANSFER" and identity.symbol:
        direction = Direction.DEBIT if quantity < 0 else Direction.CREDIT
        return [row(movements.CUSTODY_TRANSFER, direction, Decimal(0), quantity)]
    if code in {"FEE", "ADR_FEE"}:
        return [row(movements.FEE, Direction.DEBIT, amount)]
    if code == "INTEREST":
        return [row(movements.INTEREST, Direction.CREDIT, amount)]
    return [
        row(movements.CASH_MOVEMENT, Direction.CREDIT if amount >= 0 else Direction.DEBIT, amount)
    ]


def _dividend_rows(row, text: str, net: Decimal, statement, trade_date, symbol: str):
    """Rebuild the gross dividend and its withholding from the description.

    Apex Ascend reports only the net credit. Every other statement in this
    portfolio splits gross income from withheld tax, and the asset page adds the
    two up, so leaving this one net would understate Nomad's dividends and lose
    the tax entirely. The description carries the rate, the share count and the
    withholding percentage — but the split is only booked when ``gross - tax``
    reproduces the printed net, so a wording change degrades to the net figure
    instead of inventing numbers.
    """
    rate_match = _DIVIDEND_RATE_RE.search(text)
    withholding_match = _WITHHOLDING_RE.search(text)
    if rate_match and withholding_match:
        rate = parse_number(rate_match.group(1))
        shares = parse_number(rate_match.group(2))
        percent = parse_number(withholding_match.group(1))
        if rate is not None and shares is not None and percent is not None:
            try:
                gross = (rate * shares).quantize(Decimal("0.01"))
                tax = (gross * percent / Decimal(100)).quantize(Decimal("0.01"))
            except InvalidOperation:
                gross = tax = None
            if gross is not None and tax is not None and abs(gross - tax - net) <= Decimal("0.02"):
                return [
                    row(movements.DIVIDEND, Direction.CREDIT, gross),
                    row(movements.DIVIDEND_TAX, Direction.DEBIT, tax),
                ]
            # Usually means no tax was actually withheld on this distribution
            # (a qualified dividend or a return of capital), even though the
            # description carries the boilerplate withholding notice.
            statement.warnings.append(
                f"{trade_date}: {symbol} — {gross} bruto menos {tax} de imposto não reproduz o "
                f"líquido informado ({net}); registrado pelo líquido, sem separar o imposto"
            )
    return [row(movements.DIVIDEND, Direction.CREDIT if net >= 0 else Direction.DEBIT, net)]


def _raw(block: list[Line]) -> str:
    return " ".join(line.text for line in block)
