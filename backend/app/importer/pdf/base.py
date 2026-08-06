"""The shape every statement parser produces.

A parsed PDF row is deliberately made to look exactly like a row of the B3
``Movimentação`` CSV — a movement label, a direction and a positive amount — so
that classification, de-duplication and persistence stay in one place instead of
growing a second implementation per broker. See
:mod:`app.importer.pdf.movements` for the label vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.domain.enums import Direction

ZERO = Decimal(0)
#: Statement totals are printed to the cent; anything under this is rounding.
RECONCILE_TOLERANCE = Decimal("0.02")


class PdfFormatError(ValueError):
    """Raised when a PDF is not a statement any parser recognises."""


@dataclass(slots=True)
class StatementRow:
    """One movement read from a statement, normalised but not yet classified."""

    trade_date: date
    #: Canonical movement label — the key the classifier maps to an operation.
    movement: str
    direction: Direction
    #: Positive magnitude; ``direction`` carries the sign, as in the B3 export.
    amount: Decimal = ZERO
    quantity: Decimal = ZERO
    unit_price: Decimal | None = None
    symbol: str = ""
    description: str = ""
    cusip: str = ""
    settle_date: date | None = None
    #: Statement section the row came from, kept for the audit trail.
    section: str = ""
    #: Verbatim text of the row, so an odd import can always be traced back.
    raw_text: str = ""
    page_number: int | None = None


@dataclass(slots=True)
class SectionTotals:
    """A section's printed control totals against what the parser actually read.

    Every one of these statements prints per-section totals. Comparing them with
    the parsed rows turns a silent mis-parse — a missed line, a number read in
    the wrong locale, an amount put in the wrong column — into a loud, specific
    import warning instead of a quietly wrong portfolio.
    """

    section: str
    printed_debit: Decimal | None = None
    printed_credit: Decimal | None = None
    parsed_debit: Decimal = ZERO
    parsed_credit: Decimal = ZERO

    def discrepancies(self) -> list[str]:
        problems: list[str] = []
        for label, printed, parsed in (
            ("débitos", self.printed_debit, self.parsed_debit),
            ("créditos", self.printed_credit, self.parsed_credit),
        ):
            if printed is None:
                continue
            if abs(printed - parsed) > RECONCILE_TOLERANCE:
                problems.append(
                    f"{self.section}: {label} somam {parsed} mas o extrato informa {printed}"
                )
        return problems


@dataclass(slots=True)
class ParsedStatement:
    """Everything one statement PDF yields."""

    #: Identifier of the parser that produced this (e.g. ``"apex-en"``).
    format: str
    #: Canonical broker name — the custody the movements belong to.
    broker: str
    #: Broker string as printed, kept on the transaction for traceability.
    institution_raw: str
    currency: str
    period_start: date | None = None
    period_end: date | None = None
    account_ref: str = ""
    #: Total account equity at the start/end of the period. Used to detect
    #: months missing from the archive — see :mod:`app.importer.coverage`.
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    rows: list[StatementRow] = field(default_factory=list)
    totals: list[SectionTotals] = field(default_factory=list)
    #: Positions the statement itself reports at the end of the period. Not
    #: imported as movements — used to check the replayed history against what
    #: the broker says you actually hold.
    holdings: list = field(default_factory=list)
    #: Problems worth showing the user but not worth failing the import over.
    warnings: list[str] = field(default_factory=list)

    def reconciliation_warnings(self) -> list[str]:
        return [problem for total in self.totals for problem in total.discrepancies()]

    def summary(self) -> dict:
        return {
            "format": self.format,
            "broker": self.broker,
            "account": self.account_ref,
            "currency": self.currency,
            "period": {
                "start": self.period_start.isoformat() if self.period_start else None,
                "end": self.period_end.isoformat() if self.period_end else None,
            },
            "opening_balance": str(self.opening_balance) if self.opening_balance is not None else None,
            "closing_balance": str(self.closing_balance) if self.closing_balance is not None else None,
            "holdings": [
                {"symbol": h.symbol, "quantity": str(h.quantity), "cusip": h.cusip}
                for h in self.holdings
            ],
            "sections": [
                {
                    "section": total.section,
                    "printed_debit": str(total.printed_debit) if total.printed_debit is not None else None,
                    "printed_credit": (
                        str(total.printed_credit) if total.printed_credit is not None else None
                    ),
                    "parsed_debit": str(total.parsed_debit),
                    "parsed_credit": str(total.parsed_credit),
                }
                for total in self.totals
            ],
        }


class StatementParser:
    """Interface implemented by every broker/format parser.

    Parsers are stateless; :meth:`matches` is cheap (a text sniff) and
    :meth:`parse` does the real work.
    """

    #: Stable identifier stored on the import batch.
    format: str = ""
    #: Canonical broker name for movements this parser reads.
    broker: str = ""

    def matches(self, document) -> bool:  # pragma: no cover - trivial
        raise NotImplementedError

    def parse(self, document) -> ParsedStatement:  # pragma: no cover - trivial
        raise NotImplementedError
