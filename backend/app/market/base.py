"""Market data provider contract.

Swapping providers is a configuration change (``MARKET_DATA_PROVIDER``); no
application code depends on a concrete implementation.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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


class MarketDataProvider(ABC):
    """Interface every provider implements."""

    name: str = "base"

    @abstractmethod
    def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        """Return the latest price for each symbol it can resolve.

        Implementations must skip unknown symbols rather than raising, so one
        delisted ticker never blocks a portfolio-wide refresh.
        """

    def get_history(self, symbol: str, start: date | None = None) -> list[HistoricalPoint]:
        """Daily closes. Providers without history return an empty list."""
        return []

    def supports_history(self) -> bool:
        return False
