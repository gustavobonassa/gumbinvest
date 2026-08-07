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

    def supports_history(self) -> bool:
        return False
