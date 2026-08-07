"""Cold-start market data: per-pair FX bootstrap, the heal job, and the
headline Bitcoin price on a portfolio that holds no coin.

Network is always mocked — these tests must pass offline.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db.models import FxRate
from app.market import crypto, fx
from app.market.base import QuoteData
from app.market.service import heal_market_data


def _add_rate(db: Session, base: str, quote: str = "BRL") -> None:
    db.add(FxRate(base=base, quote=quote, date=date(2026, 8, 1), rate=Decimal("5.10"), source="bcb-ptax"))
    db.commit()


def test_missing_pairs_is_per_pair_not_per_table(db: Session):
    # Empty table: every supported pair is owed.
    assert fx.missing_pairs(db) == fx.supported_pairs()

    # The dollar alone must not count as "done" — that is exactly the bug that
    # left a fresh install without the euro until the next day's sync.
    _add_rate(db, "USD")
    assert fx.missing_pairs(db) == [("EUR", "BRL")]

    _add_rate(db, "EUR")
    assert fx.missing_pairs(db) == []


def test_heal_fetches_only_what_is_absent(db: Session, monkeypatch):
    synced: list[tuple[str, str]] = []
    monkeypatch.setattr(fx, "sync_fx", lambda _db, base, quote: synced.append((base, quote)))

    from app.market import benchmarks, indices

    monkeypatch.setattr(benchmarks, "missing", lambda _db: [])
    monkeypatch.setattr(indices, "index_status", lambda _db: [{"code": "CDI"}])

    _add_rate(db, "USD")
    healed = heal_market_data(db)

    assert synced == [("EUR", "BRL")]
    assert healed == {"fx": ["EUR/BRL"]}

    # Second pass: everything exists — a strict no-op, so the half-hourly
    # schedule can never hammer a provider that just rate-limited us.
    _add_rate(db, "EUR")
    synced.clear()
    assert heal_market_data(db) == {}
    assert synced == []


def test_heal_survives_one_group_failing(db: Session, monkeypatch):
    from app.market import benchmarks, indices

    def boom(_db, *args, **kwargs):
        raise RuntimeError("HTTP 429")

    monkeypatch.setattr(fx, "sync_fx", boom)
    monkeypatch.setattr(benchmarks, "missing", lambda _db: ["IBOV"])
    monkeypatch.setattr(benchmarks, "sync_benchmarks", lambda _db: {"points": 1})
    monkeypatch.setattr(indices, "index_status", lambda _db: [{"code": "CDI"}])

    healed = heal_market_data(db)
    # FX blew up, but the benchmark group still ran.
    assert healed == {"benchmarks": ["IBOV"]}


class _FakeProvider:
    def __init__(self, quote: QuoteData | None):
        self.quote = quote
        self.calls = 0

    def get_quotes(self, symbols):
        self.calls += 1
        if self.quote is None:
            raise TimeoutError("read timeout")
        return {symbols[0]: self.quote}


@pytest.fixture(autouse=True)
def _clear_headline_cache():
    # The conftest disables the network-reaching fallback suite-wide; these
    # tests exercise exactly that path, with the provider mocked.
    crypto.UNHELD_FETCH_ENABLED = True
    crypto._UNHELD_CACHE.clear()
    yield
    crypto._UNHELD_CACHE.clear()


def test_headline_btc_appears_without_holding_it(db: Session, monkeypatch):
    _add_rate(db, "USD")  # PTAX so the dollar price converts to reais
    provider = _FakeProvider(
        QuoteData(symbol="BTC-USD", price=Decimal("60000"), change_percent=Decimal("1.5"), currency="USD")
    )
    monkeypatch.setattr("app.market.providers.get_provider", lambda name=None: provider)

    prices = crypto.headline_prices(db)
    assert len(prices) == 1
    assert prices[0]["symbol"] == "BTC"
    assert prices[0]["price_base"] == Decimal("60000") * Decimal("5.10")

    # Cached: the sidebar polls every five minutes, the provider must not be
    # called again inside the TTL.
    crypto.headline_prices(db)
    assert provider.calls == 1


def test_headline_failure_serves_stale_price_then_backs_off(db: Session, monkeypatch):
    _add_rate(db, "USD")
    provider = _FakeProvider(QuoteData(symbol="BTC-USD", price=Decimal("60000"), currency="USD"))
    monkeypatch.setattr("app.market.providers.get_provider", lambda name=None: provider)
    assert crypto.headline_prices(db)  # primes the cache

    # Provider starts timing out; expire the cache to force a refetch.
    provider.quote = None
    symbol_quote, fetched_at, _ = crypto._UNHELD_CACHE["BTC"]
    crypto._UNHELD_CACHE["BTC"] = (symbol_quote, fetched_at, 0.0)

    prices = crypto.headline_prices(db)
    # The stale price keeps being served instead of the row blinking out…
    assert prices and prices[0]["price"] == Decimal("60000")
    calls_after_failure = provider.calls

    # …and the failure is cached: the next poll inside the backoff window must
    # not hit the provider again.
    crypto.headline_prices(db)
    assert provider.calls == calls_after_failure


def test_headline_failure_with_no_history_hides_the_row(db: Session, monkeypatch):
    provider = _FakeProvider(None)
    monkeypatch.setattr("app.market.providers.get_provider", lambda name=None: provider)
    assert crypto.headline_prices(db) == []
    # Backed off: a second poll does not retry immediately.
    crypto.headline_prices(db)
    assert provider.calls == 1
