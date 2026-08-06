"""Ticker discovery for papers the portfolio never traded.

Everything else in ``app/market`` starts from an :class:`~app.db.models.Asset`
row that an import created. Watching an asset you do not own inverts that: the
ticker arrives first, from a search box or a typed URL, and has to be resolved
against the market before any row exists. Yahoo's search endpoint does that
resolution — same infrastructure as the quote provider, no key required.

Only B3 (``.SA``) and plain US symbols are accepted. Other exchanges would need
per-exchange currency knowledge the app does not have, and a wrong currency
silently corrupts every BRL conversion downstream — better to not find a ticker
than to book it in the wrong money.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.domain.enums import AssetKind

logger = get_logger(__name__)

SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GumbInvest/1.0)"}

#: Families the watch-only flow supports. Currencies, futures and crypto pairs
#: come back from the same search and would need handling this app reserves for
#: its importers.
_ALLOWED_TYPES = {"EQUITY", "ETF"}


@dataclass(frozen=True)
class MarketHit:
    """One search result, already translated to the app's vocabulary."""

    ticker: str  # app-side ticker: PETR4, AAPL
    name: str
    kind: str  # AssetKind value
    currency: str  # BRL for B3, USD otherwise
    market_symbol: str  # what the quote provider is asked for: PETR4.SA
    exchange: str


def _fetch_raw(query: str, limit: int) -> list[dict]:
    """The raw ``quotes`` array from Yahoo search; [] on any failure."""
    params = {"q": query, "quotesCount": limit, "newsCount": 0, "listsCount": 0}
    try:
        with httpx.Client(timeout=settings.request_timeout, follow_redirects=True) as client:
            response = client.get(SEARCH_URL, params=params, headers=HEADERS)
            response.raise_for_status()
            return response.json().get("quotes") or []
    except Exception as exc:  # noqa: BLE001 — discovery failing must never break local search
        logger.warning("market lookup failed for %r: %s", query, exc)
        return []


def _to_hit(item: dict) -> MarketHit | None:
    """Map one Yahoo result to a :class:`MarketHit`; None when unsupported."""
    symbol = (item.get("symbol") or "").upper()
    if not symbol or item.get("quoteType") not in _ALLOWED_TYPES:
        return None
    name = item.get("longname") or item.get("shortname") or symbol

    if symbol.endswith(".SA"):
        ticker = symbol[:-3]
        currency = "BRL"
        # The importer already knows how to read a B3 ticker's suffix (PETR4 →
        # stock, HGLG11 + "FII" in the name → FII); reuse it rather than
        # duplicate the table. Local import: this is the one place the market
        # layer needs it.
        from app.importer.parser import classify_asset_kind

        kind = classify_asset_kind(ticker, name).value
    elif "." not in symbol and "-" not in symbol:
        ticker = symbol
        currency = "USD"
        kind = AssetKind.ETF.value if item.get("quoteType") == "ETF" else AssetKind.STOCK.value
    else:
        return None  # other exchanges: unknown currency, see module docstring

    return MarketHit(
        ticker=ticker,
        name=name,
        kind=kind,
        currency=currency,
        market_symbol=symbol,
        exchange=item.get("exchDisp") or item.get("exchange") or "",
    )


def search_market(query: str, limit: int = 8) -> list[MarketHit]:
    """Tickers and companies the market knows for ``query``."""
    query = query.strip()
    if len(query) < 2:
        return []
    hits = (_to_hit(item) for item in _fetch_raw(query, limit))
    return [hit for hit in hits if hit is not None][:limit]


def resolve(ticker: str) -> MarketHit | None:
    """The market's answer for one exact ticker, or None.

    Used before creating a watch-only asset: a typo'd URL must 404, not mint a
    junk row. Exact match only — searching "PETR" finds PETR4 and PETR3, but
    resolving "PETR" finds nothing.
    """
    wanted = ticker.strip().upper()
    if not wanted:
        return None
    for hit in search_market(wanted):
        if wanted in (hit.ticker, hit.market_symbol):
            return hit
    return None
