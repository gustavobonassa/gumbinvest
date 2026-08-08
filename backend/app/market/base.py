"""Market data provider contract.

Swapping providers is a configuration change (``MARKET_DATA_PROVIDER``); no
application code depends on a concrete implementation.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(slots=True)
class QuoteData:
    symbol: str
    price: Decimal
    previous_close: Decimal | None = None
    change: Decimal | None = None
    change_percent: Decimal | None = None
    currency: str = "BRL"
    long_name: str | None = None


@dataclass(slots=True)
class HistoricalPoint:
    day: date
    close: Decimal


@dataclass(slots=True)
class QuoteBatch:
    """The outcome of one fetch, with the two kinds of absence kept apart.

    A symbol missing from ``quotes`` used to mean either "this paper has no
    public price" or "the request failed" — and the caller could not tell,
    so a timeout was presented to the user as a permanently unpriced asset.
    ``failed`` carries the second case: symbols worth asking about again.
    """

    quotes: dict[str, QuoteData]
    #: Symbols whose fetch failed for a transient reason, mapped to that reason
    #: (``"HTTP 429"``, ``"ReadTimeout: …"``). Never contains a symbol the
    #: provider answered "unknown" for — those are absent from both fields.
    failed: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class HistorySeries:
    """Daily closes plus the share splits that shaped them.

    The two belong together because one explains the other: a provider's series
    is stated in *today's* shares, so every close before a split has already
    been divided by its ratio. A caller that stores the closes without the
    splits cannot later tell that series apart from one that never split — and
    will value a past holding at a fraction of what it was worth.
    """

    points: list[HistoricalPoint]
    #: ``(day, ratio)`` — 6.0 for a 6-for-1, 0.1 for a 1-for-10 grouping.
    splits: list[tuple[date, Decimal]] = field(default_factory=list)


class MarketDataProvider(ABC):
    """Interface every provider implements."""

    name: str = "base"

    @abstractmethod
    def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        """Return the latest price for each symbol it can resolve.

        Implementations must skip unknown symbols rather than raising, so one
        delisted ticker never blocks a portfolio-wide refresh.
        """

    def fetch_quotes(self, symbols: list[str]) -> QuoteBatch:
        """:meth:`get_quotes` plus which symbols failed transiently.

        The default reports no failures, which is the honest answer for a
        provider that cannot distinguish them; overriding it is what buys an
        asset a retry instead of a "sem cotação" badge.
        """
        return QuoteBatch(quotes=self.get_quotes(symbols))

    def get_history(self, symbol: str, start: date | None = None) -> list[HistoricalPoint]:
        """Daily closes. Providers without history return an empty list."""
        return []

    def get_splits(self, symbol: str) -> list[tuple[date, Decimal]]:
        """Declared share splits, without downloading the price series.

        Separate from :meth:`fetch_history` because the two are needed at very
        different rates: history is a heavy one-off, while splits have to be
        re-checked periodically — a split declared next month invalidates every
        stored close before it, and an install that only ever fetches history
        for *new* assets would never find out.
        """
        return []

    def fetch_history(self, symbol: str, start: date | None = None) -> HistorySeries:
        """:meth:`get_history` plus the splits behind it.

        The default reports no splits, which is the honest answer for a
        provider that does not publish them — and leaves that provider's
        history valued as it always was.
        """
        return HistorySeries(points=self.get_history(symbol, start))

    def supports_history(self) -> bool:
        return False
