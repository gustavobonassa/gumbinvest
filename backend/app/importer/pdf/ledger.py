"""Shared reader for the Apex-style ledger table.

Apex Clearing's English statements and Avenue's Portuguese ones print the same
skeleton::

    TRANSACTION DATE TYPE DESCRIPTION QUANTITY PRICE DEBIT CREDIT
    BUY / SELL TRANSACTIONS
    BOUGHT 01/05/26 C REALTY INCOME CORP        1.46958  $56.88  $83.59
                       CUSIP: 756109104
    Total Buy / Sell Transactions                                $83.59

so the mechanics — find the section, re-anchor the columns on each header row,
group a transaction with its continuation lines, read the control total — live
here once. The two parsers only differ in how they *interpret* a block.

Column x-positions are re-read from every header row rather than assumed: the
same document uses one set of positions for the main ledger and a wider set for
the pending-settlement table.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.importer.pdf.layout import ColumnMap, Document, Line, normalize
from app.importer.pdf.values import parse_number

HEADER_LABELS = {
    "quantity": "QUANTITY",
    "price": "PRICE",
    "debit": "DEBIT",
    "credit": "CREDIT",
}
#: Where the description column begins, relative to the "TYPE" column.
DESCRIPTION_MARGIN = 8.0
#: Slack allowed left of the first column before a word counts as page furniture.
SIDEBAR_MARGIN = 6.0

_CUSIP_RE = re.compile(r"CUSIP:\s*([0-9A-Z]{9})")
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
_TOTAL_RE = re.compile(r"(?i)^total\s+(.*)$")
#: "FROM 11135B100 TO 75574U101" — Apex spells out a merger's two sides.
_MERGER_RE = re.compile(r"FROM\s+([0-9A-Z]{9})\s+TO\s+([0-9A-Z]{9})")


@dataclass
class LedgerBlock:
    """One transaction: its first line plus every continuation line."""

    section: str
    type_code: str
    date_text: str
    columns: ColumnMap
    lines: list[Line] = field(default_factory=list)
    #: x where the description column starts (right of the account-type letter).
    description_x0: float = 0.0

    @property
    def page_number(self) -> int:
        return self.lines[0].page_number if self.lines else 0

    @property
    def raw_text(self) -> str:
        return " / ".join(line.text for line in self.lines)

    def _description_x1(self) -> float:
        quantity = self.columns.get("quantity")
        return quantity.x0 - 2 if quantity else 10_000.0

    def description(self) -> str:
        """Description words from every line of the block, in reading order."""
        parts: list[str] = []
        for line in self.lines:
            for word in line.between(self.description_x0, self._description_x1()):
                parts.append(word.text)
        text = " ".join(parts)
        text = _CUSIP_RE.sub("", text)
        return re.sub(r"\s+", " ", text).strip()

    def cusip(self) -> str:
        match = _CUSIP_RE.search(self.raw_text)
        return match.group(1) if match else ""

    def merger_sides(self) -> tuple[str, str] | None:
        match = _MERGER_RE.search(self.raw_text)
        return (match.group(1), match.group(2)) if match else None

    def column_values(self, name: str) -> list[Decimal]:
        """Every number that lands in one column, across the block's lines.

        A dividend block puts its gross amount in ``credit`` on the first line
        and its withholding in ``debit`` on a continuation line, so both are
        collected rather than only the first.
        """
        column = self.columns.get(name)
        if column is None:
            return []
        values: list[Decimal] = []
        for line in self.lines:
            for word in line.words:
                if word.text in {"$", "-"}:
                    continue
                if self.columns.assign(word) != name:
                    continue
                value = parse_number(word.text)
                if value is not None:
                    values.append(value)
        return values

    def first(self, name: str) -> Decimal | None:
        values = self.column_values(name)
        return values[0] if values else None

    def has_text(self, *needles: str) -> bool:
        key = normalize(self.raw_text)
        return any(normalize(needle) in key for needle in needles)


@dataclass(slots=True)
class LedgerTotal:
    """A ``Total <section>`` control row."""

    section: str
    debit: Decimal | None
    credit: Decimal | None


def read_ledger(
    document: Document,
    row_types: frozenset[str],
    sections: dict[str, str],
    skip_sections: frozenset[str] = frozenset(),
) -> tuple[list[LedgerBlock], list[LedgerTotal]]:
    """Walk the whole document, yielding transaction blocks and their totals.

    ``sections`` maps a normalised section heading to the name used downstream;
    ``skip_sections`` names headings whose rows must be ignored entirely — the
    pending-settlement table repeats trades that the next statement books for
    real, so importing it would double-count every trade that straddles a month
    end.
    """
    blocks: list[LedgerBlock] = []
    totals: list[LedgerTotal] = []

    columns: ColumnMap | None = None
    type_x1 = 0.0
    content_x0 = 0.0
    section = ""
    section_key = ""
    skipping = False
    current: LedgerBlock | None = None
    page = 0

    for line in document.lines():
        if line.page_number != page:
            # A transaction never spans a page: the section heading and column
            # header are reprinted on the next one. Without this, the last block
            # of a page swallows the following page's letterhead — and the Miami
            # ZIP code sits exactly under the credit column.
            page = line.page_number
            current = None

        header = ColumnMap.from_line(line, HEADER_LABELS)
        if header is not None:
            columns = header
            type_column = line.find("(?i)TYPE")
            type_x1 = type_column.x1 if type_column else 0.0
            first_column = line.find("(?i)TRANSACTION")
            content_x0 = (first_column.x0 - SIDEBAR_MARGIN) if first_column else 0.0
            current = None
            continue

        heading = _match_section(line, sections, skip_sections)
        if heading is not None:
            section_key, section, skipping = heading
            current = None
            continue

        total = _match_total(line, columns, section, section_key, content_x0)
        if total is not None:
            # Totals of a skipped section would flag a mismatch against rows
            # that were deliberately never read.
            if not skipping:
                totals.append(total)
            current = None
            continue

        if columns is None or skipping:
            continue

        # Apex prints "ACCOUNT STATEMENT" vertically down the left margin, one
        # letter per line, which lands as a stray leading word on whichever
        # rows happen to line up with it. Anything left of the first column is
        # page furniture, not data.
        words = line.after(content_x0)
        started = (
            len(words) >= 2
            and words[0].text.upper() in row_types
            and _DATE_RE.match(words[1].text) is not None
        )
        if started:
            current = LedgerBlock(
                section=section,
                type_code=words[0].text.upper(),
                date_text=words[1].text,
                columns=columns,
                lines=[line],
                description_x0=type_x1 + DESCRIPTION_MARGIN,
            )
            blocks.append(current)
        elif current is not None:
            current.lines.append(line)

    return blocks, totals


def _match_section(
    line: Line, sections: dict[str, str], skip_sections: frozenset[str]
) -> tuple[str, str, bool] | None:
    """Recognise a section heading, tolerating the ``(continued)`` suffix.

    A leading sidebar letter is stripped first, so ``C DIVIDENDS AND INTEREST``
    is still recognised as the dividends section.
    """
    key = line.key.replace("continued", "")
    for candidate, name in sections.items():
        if key == candidate or (len(key) == len(candidate) + 1 and key[1:] == candidate):
            return candidate, name, name in skip_sections
    return None


def _match_total(
    line: Line,
    columns: ColumnMap | None,
    section: str,
    section_key: str,
    content_x0: float = 0.0,
) -> LedgerTotal | None:
    """Read a ``Total <section>`` control row.

    The label has to name the section it closes. Both Avenue statements print
    unrelated totals in the same columns further down the page ("Total Cash &
    Cash Equivalents"), which would otherwise be compared against the dividend
    rows and report a mismatch that is not there.
    """
    text = " ".join(word.text for word in line.after(content_x0))
    match = _TOTAL_RE.match(text.strip())
    if match is None or columns is None or not section_key:
        return None
    if section_key not in normalize(text):
        return None
    debit: Decimal | None = None
    credit: Decimal | None = None
    for word in line.words:
        if word.text in {"$", "-"}:
            continue
        value = parse_number(word.text)
        if value is None:
            continue
        target = columns.assign(word)
        if target == "debit":
            debit = value
        elif target == "credit":
            credit = value
    if debit is None and credit is None:
        return None
    return LedgerTotal(section=section, debit=debit, credit=credit)
