"""De-duplication keys shared by the CSV and PDF importers.

Two different problems, two different keys.

**The same file, again.** Solved by an exact fingerprint plus an occurrence
counter: re-importing produces identical keys, so every row is a duplicate,
while genuinely repeated movements (one April statement lists the same $5.00
withholding reversal nine times) get occurrences 0..8 and are all kept.

**The same event, from two different statements.** The offshore brokers issue
more than one report per month and they do not agree with each other:

* Apex dates a Verizon dividend 2025-02-03 and Avenue dates it 2025-02-04;
* Apex books a purchase at $165.17 and Avenue books the same purchase, same
  quantity, same day, at $162.67 — Apex includes the $2.50 commission.

An exact key sees four movements where there were two. So rows arriving from a
*different source format* for the same broker are additionally matched on a
fuzzy key: same broker, same operation, same asset, same quantity, within a few
days. Trades also tolerate a differing amount, because that difference is the
commission and nothing else; income and tax rows do not, because a statement can
legitimately repeat an identical amount many times and only the amount tells two
of them apart.

Fuzzy matching is never applied within one file or between two files of the same
format — there, a repeat is a real repeat.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.domain.enums import PositionEffect

#: How far apart two statements may date the same event.
FUZZY_DATE_WINDOW = timedelta(days=3)
#: Effects whose amount may legitimately differ between sources (commission).
AMOUNT_TOLERANT_EFFECTS = frozenset({PositionEffect.ACQUIRE.value, PositionEffect.DISPOSE.value})


def _number(value: Decimal | None) -> str:
    """Format a Decimal without scientific notation or trailing-zero noise."""
    if value is None:
        return "0"
    return format(value.normalize(), "f")


def exact_fingerprint(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]


@dataclass(frozen=True, slots=True)
class FuzzyKey:
    """Identity of an event, independent of which statement reported it."""

    broker: str
    op_type: str
    ticker: str
    quantity: str
    #: Empty for trades, where the two sources disagree by the commission.
    amount: str


def fuzzy_key(
    broker: str, op_type: str, effect: str, ticker: str, quantity: Decimal, amount: Decimal
) -> FuzzyKey:
    return FuzzyKey(
        broker=broker.upper(),
        op_type=op_type,
        ticker=ticker.upper(),
        quantity=_number(quantity),
        amount="" if effect in AMOUNT_TOLERANT_EFFECTS else _number(amount),
    )


class FuzzyIndex:
    """Dated occurrences of every fuzzy key already stored for a portfolio.

    Count-aware on purpose: when a statement lists nine identical reversals and
    another source reports three of them, three are duplicates and six are new.
    """

    def __init__(self) -> None:
        self._dates: dict[FuzzyKey, list[date]] = {}

    def add(self, key: FuzzyKey, day: date) -> None:
        self._dates.setdefault(key, []).append(day)

    def take(self, key: FuzzyKey, day: date) -> bool:
        """Consume a match near ``day``; ``True`` when the row is a duplicate."""
        days = self._dates.get(key)
        if not days:
            return False
        best: int | None = None
        best_distance = FUZZY_DATE_WINDOW
        for index, candidate in enumerate(days):
            distance = abs(candidate - day)
            if distance <= best_distance:
                best, best_distance = index, distance
        if best is None:
            return False
        days.pop(best)
        return True
