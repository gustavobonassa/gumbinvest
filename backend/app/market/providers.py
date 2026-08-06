"""Concrete market data providers.

* :class:`YahooChartProvider` — Yahoo Finance chart API, no key required
  (the default: the app has live prices straight after ``docker compose up``).
* :class:`BrapiProvider`      — brapi.dev, richest B3 data; needs a free token.
* :class:`YFinanceProvider`   — the ``yfinance`` package, kept as an alternative.
* :class:`NullProvider`       — disables live pricing (positions valued at cost).
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.market.base import HistoricalPoint, MarketDataProvider, QuoteData

logger = get_logger(__name__)

#: brapi/Yahoo accept at most a handful of symbols per call.
BATCH_SIZE = 15


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class NullProvider(MarketDataProvider):
    name = "none"

    def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        return {}


class BrapiProvider(MarketDataProvider):
    """brapi.dev — https://brapi.dev/docs

    Free tier works without a token but is rate limited; set ``BRAPI_TOKEN``
    for reliable refreshes.
    """

    name = "brapi"

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or settings.brapi_base_url).rstrip("/")
        self.token = token if token is not None else settings.brapi_token

    def _params(self, extra: dict | None = None) -> dict:
        params = dict(extra or {})
        if self.token:
            params["token"] = self.token
        return params

    def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        results: dict[str, QuoteData] = {}
        if not symbols:
            return results
        with httpx.Client(timeout=settings.request_timeout) as client:
            for start in range(0, len(symbols), BATCH_SIZE):
                batch = symbols[start : start + BATCH_SIZE]
                url = f"{self.base_url}/quote/{','.join(batch)}"
                try:
                    response = client.get(url, params=self._params())
                    if response.status_code == 404:
                        logger.info("brapi: no data for %s", batch)
                        continue
                    response.raise_for_status()
                    payload = response.json()
                except Exception as exc:  # noqa: BLE001 — a failed batch must not abort the refresh
                    logger.warning("brapi quote request failed for %s: %s", batch, exc)
                    continue
                for item in payload.get("results", []) or []:
                    symbol = (item.get("symbol") or "").upper()
                    price = _dec(item.get("regularMarketPrice"))
                    if not symbol or price is None:
                        continue
                    results[symbol] = QuoteData(
                        symbol=symbol,
                        price=price,
                        previous_close=_dec(item.get("regularMarketPreviousClose")),
                        change=_dec(item.get("regularMarketChange")),
                        change_percent=_dec(item.get("regularMarketChangePercent")),
                        currency=item.get("currency") or "BRL",
                        long_name=item.get("longName") or item.get("shortName"),
                    )
        return results

    def supports_history(self) -> bool:
        return True

    def get_history(self, symbol: str, start: date | None = None) -> list[HistoricalPoint]:
        params = self._params({"range": "max", "interval": "1d"})
        url = f"{self.base_url}/quote/{symbol}"
        try:
            with httpx.Client(timeout=settings.request_timeout) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("brapi history failed for %s: %s", symbol, exc)
            return []

        points: list[HistoricalPoint] = []
        for item in payload.get("results", []) or []:
            for candle in item.get("historicalDataPrice", []) or []:
                close = _dec(candle.get("close"))
                stamp = candle.get("date")
                if close is None or stamp is None:
                    continue
                day = datetime.fromtimestamp(int(stamp), tz=UTC).date()
                if start and day < start:
                    continue
                points.append(HistoricalPoint(day=day, close=close))
        return points


class YahooChartProvider(MarketDataProvider):
    """Yahoo Finance chart endpoint — works with no API key.

    ``/v8/finance/chart/{symbol}`` returns the last price, the previous close
    and the daily candles in one call, which covers both quotes and history.
    B3 tickers take the ``.SA`` suffix. This is the default provider because it
    needs zero configuration; brapi gives better B3 metadata once you have a
    token.
    """

    name = "yahoo"
    BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
    HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GumbInvest/1.0)"}

    @staticmethod
    def market_symbol(symbol: str) -> str:
        """B3 tickers need the ``.SA`` suffix; US ones must not get it.

        Callers pass symbols already resolved by
        :func:`app.market.service.resolve_market_symbol`, which knows the
        asset's currency. The suffix is only added here as a fallback for a
        bare B3-shaped ticker (four letters and a digit) — ``BAC`` and ``VOO``
        are left alone, because ``BAC.SA`` is a different company entirely.
        """
        symbol = symbol.upper()
        if "." in symbol:
            return symbol
        return f"{symbol}.SA" if re.fullmatch(r"[A-Z]{4}\d{1,2}", symbol) else symbol

    def _fetch(self, client: httpx.Client, symbol: str, params: dict) -> dict | None:
        url = f"{self.BASE_URL}/{self.market_symbol(symbol)}"
        try:
            response = client.get(url, params=params, headers=self.HEADERS)
            if response.status_code >= 400:
                logger.info("yahoo: %s returned %s", symbol, response.status_code)
                return None
            results = (response.json().get("chart") or {}).get("result") or []
            return results[0] if results else None
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not stop the batch
            logger.warning("yahoo request failed for %s: %s", symbol, exc)
            return None

    def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        results: dict[str, QuoteData] = {}
        if not symbols:
            return results
        # The chart endpoint is single-symbol; fetch a handful at a time so a
        # full-portfolio refresh takes seconds, not minutes.
        with httpx.Client(timeout=settings.request_timeout, follow_redirects=True) as client:
            with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
                payloads = pool.map(
                    lambda s: (s, self._fetch(client, s, {"range": "5d", "interval": "1d"})),
                    symbols,
                )
                for symbol, payload in payloads:
                    if not payload:
                        continue
                    meta = payload.get("meta") or {}
                    price = _dec(meta.get("regularMarketPrice"))
                    if price is None:
                        continue
                    previous = _dec(meta.get("chartPreviousClose") or meta.get("previousClose"))
                    change = price - previous if previous else None
                    results[symbol.upper()] = QuoteData(
                        symbol=symbol.upper(),
                        price=price,
                        previous_close=previous,
                        change=change,
                        change_percent=(change / previous * 100) if change is not None and previous else None,
                        currency=meta.get("currency") or "BRL",
                        long_name=meta.get("longName") or meta.get("shortName"),
                    )
        return results

    def supports_history(self) -> bool:
        return True

    def get_history(self, symbol: str, start: date | None = None) -> list[HistoricalPoint]:
        with httpx.Client(timeout=settings.request_timeout, follow_redirects=True) as client:
            payload = self._fetch(client, symbol, {"range": "10y", "interval": "1d"})
        if not payload:
            return []
        timestamps = payload.get("timestamp") or []
        quote = ((payload.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        points: list[HistoricalPoint] = []
        for stamp, close in zip(timestamps, closes):
            value = _dec(close)
            if stamp is None or value is None:
                continue
            day = datetime.fromtimestamp(int(stamp), tz=UTC).date()
            if start and day < start:
                continue
            points.append(HistoricalPoint(day=day, close=value))
        return points


class YFinanceProvider(MarketDataProvider):
    """Yahoo Finance. B3 tickers must carry the ``.SA`` suffix."""

    name = "yfinance"

    @staticmethod
    def _yahoo_symbol(symbol: str) -> str:
        return YahooChartProvider.market_symbol(symbol)

    def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        if not symbols:
            return {}
        try:
            import yfinance as yf
        except ImportError:  # pragma: no cover - dependency is in requirements
            logger.error("yfinance is not installed")
            return {}

        results: dict[str, QuoteData] = {}
        mapping = {self._yahoo_symbol(s): s for s in symbols}
        try:
            tickers = yf.Tickers(" ".join(mapping))
        except Exception as exc:  # noqa: BLE001
            logger.warning("yfinance batch failed: %s", exc)
            return {}

        for yahoo_symbol, original in mapping.items():
            try:
                info = tickers.tickers[yahoo_symbol].fast_info
                price = _dec(info.get("last_price") if hasattr(info, "get") else info.last_price)
                previous = _dec(
                    info.get("previous_close") if hasattr(info, "get") else info.previous_close
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("yfinance: no data for %s (%s)", yahoo_symbol, exc)
                continue
            if price is None:
                continue
            change = price - previous if previous else None
            results[original.upper()] = QuoteData(
                symbol=original.upper(),
                price=price,
                previous_close=previous,
                change=change,
                change_percent=(change / previous * 100) if change is not None and previous else None,
                currency="BRL",
            )
        return results

    def supports_history(self) -> bool:
        return True

    def get_history(self, symbol: str, start: date | None = None) -> list[HistoricalPoint]:
        try:
            import yfinance as yf
        except ImportError:  # pragma: no cover
            return []
        try:
            frame = yf.Ticker(self._yahoo_symbol(symbol)).history(
                period="max", interval="1d", auto_adjust=False
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("yfinance history failed for %s: %s", symbol, exc)
            return []
        points: list[HistoricalPoint] = []
        for stamp, row in frame.iterrows():
            day = stamp.date()
            if start and day < start:
                continue
            close = _dec(row.get("Close"))
            if close is not None:
                points.append(HistoricalPoint(day=day, close=close))
        return points


_REGISTRY: dict[str, type[MarketDataProvider]] = {
    "yahoo": YahooChartProvider,
    "brapi": BrapiProvider,
    "yfinance": YFinanceProvider,
    "none": NullProvider,
    "null": NullProvider,
}


def get_provider(name: str | None = None) -> MarketDataProvider:
    """Instantiate the configured provider (falls back to :class:`NullProvider`)."""
    key = (name or settings.market_data_provider or "yahoo").lower()
    provider_cls = _REGISTRY.get(key)
    if provider_cls is None:
        logger.warning("unknown market data provider %r — live pricing disabled", key)
        return NullProvider()
    return provider_cls()


def available_providers() -> list[str]:
    return ["yahoo", "brapi", "yfinance", "none"]
