"""Reading the positions a statement says you hold.

Every one of these statements prints its own portfolio table. Capturing it turns
the importer's output into something checkable: replay the movements, compare
the resulting quantity with what the broker reported at the end of that month,
and any disagreement means the history is incomplete — a statement never
downloaded, a format quirk not handled, a corporate action missed.

That check is far stronger than looking for gaps in a calendar, because it also
catches the case where the file *is* there but something in it was not read.
See :mod:`app.importer.coverage`.

All four layouts share one geometry: the ticker is left-aligned under its header
and the quantity is right-aligned under its own, so a single reader handles them
all once the header row is found.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.importer.pdf.layout import Document, Line, normalize
from app.importer.pdf.values import parse_number

SYMBOL_LABELS = frozenset({"symbol", "simbolo", "symbolcusip"})
QUANTITY_LABELS = frozenset({"quantity", "quantidade"})
#: Labels that mark an *activity* table, which also has symbol and quantity
#: columns and must not be mistaken for a holdings table.
ACTIVITY_LABELS = frozenset(
    {"date", "data", "debit", "credit", "settle", "trade", "transaction", "transacao", "negociacao"}
)
#: How far right of the symbol column a ticker may extend.
SYMBOL_SPAN = 60.0
#: Slack on the right-aligned quantity column.
QUANTITY_TOLERANCE = 8.0

_CUSIP_RE = re.compile(r"^[0-9A-Z]{9}$")
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,5}$")
#: The money-market sweep vehicles: cash wearing a ticker, not a position.
CASH_SYMBOLS = frozenset({"DWBDS", "QACDS", "MMDA1"})


@dataclass(slots=True)
class Holding:
    """One line of a statement's portfolio table."""

    symbol: str
    quantity: Decimal
    cusip: str = ""


@dataclass(slots=True)
class _Anchor:
    symbol_x0: float
    quantity_x1: float


def read_holdings(document: Document) -> list[Holding]:
    """Every position the statement reports, across all its portfolio tables."""
    lines = document.lines()
    holdings: dict[str, Holding] = {}
    anchor: _Anchor | None = None

    for index, line in enumerate(lines):
        candidate = _read_header(line, lines[index + 1] if index + 1 < len(lines) else None)
        if candidate is not None:
            anchor = candidate
            continue
        if anchor is None:
            continue
        if line.key.startswith("total"):
            anchor = None
            continue

        symbol_words = [
            word
            for word in line.words
            if anchor.symbol_x0 - 6 <= word.x0 <= anchor.symbol_x0 + SYMBOL_SPAN
        ]
        if not symbol_words:
            continue

        token = symbol_words[0].text.strip().upper()
        if _CUSIP_RE.match(token) and any(character.isdigit() for character in token):
            # A CUSIP printed under the ticker of the row above.
            for holding in reversed(list(holdings.values())):
                if not holding.cusip:
                    holding.cusip = token
                break
            continue
        if not _TICKER_RE.match(token) or token in CASH_SYMBOLS:
            continue

        quantity = _quantity(line, anchor.quantity_x1)
        if quantity is None:
            continue
        existing = holdings.get(token)
        if existing is None:
            holdings[token] = Holding(symbol=token, quantity=quantity)
        else:
            # The same ticker can appear in two tables (Apex splits its
            # portfolio across pages); the later figure restates, not adds.
            existing.quantity = quantity
    return list(holdings.values())


def _read_header(line: Line, following: Line | None) -> _Anchor | None:
    """Anchor on a portfolio header, which may wrap onto a second line."""
    words = list(line.words) + list(following.words if following else [])
    labels = {normalize(word.text): word for word in words}
    if ACTIVITY_LABELS & set(labels):
        return None
    symbol = next((labels[name] for name in SYMBOL_LABELS if name in labels), None)
    quantity = next((labels[name] for name in QUANTITY_LABELS if name in labels), None)
    if symbol is None or quantity is None:
        return None
    return _Anchor(symbol_x0=symbol.x0, quantity_x1=quantity.x1)


def _quantity(line: Line, x1: float) -> Decimal | None:
    for word in line.words:
        if abs(word.x1 - x1) > QUANTITY_TOLERANCE:
            continue
        value = parse_number(word.text)
        if value is not None:
            return value
    return None
