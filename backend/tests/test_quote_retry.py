"""The retry queue behind quote fetches that fail transiently.

The bug these pin down: a fresh install asks the provider for every held ticker
at once, Yahoo throttles part of the batch, and the assets that lost the race
were reported to the user as "sem cotação — avaliado pelo preço médio" — the
same wording a genuinely unquotable paper gets. One is the user's problem to
solve, the other resolves itself in two minutes, and the app could not tell
them apart because the provider returned a dict with the symbol simply absent.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Asset, Portfolio, Quote, QuoteAttempt, Transaction, WatchlistItem
from app.market import service as market_service
from app.market.base import MarketDataProvider, QuoteBatch, QuoteData


class StubProvider(MarketDataProvider):
    """Answers each symbol however the test says: a price, silence or a fault."""

    name = "stub"

    def __init__(self) -> None:
        #: symbol -> price. Anything absent is answered "unknown".
        self.prices: dict[str, Decimal] = {}
        #: symbol -> reason. Reported as a transient failure.
        self.faults: dict[str, str] = {}
        self.calls: list[list[str]] = []

    def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        return self.fetch_quotes(symbols).quotes

    def fetch_quotes(self, symbols: list[str]) -> QuoteBatch:
        self.calls.append(list(symbols))
        quotes = {
            symbol: QuoteData(symbol=symbol, price=self.prices[symbol])
            for symbol in symbols
            if symbol in self.prices
        }
        failed = {s: reason for s, reason in self.faults.items() if s in symbols}
        return QuoteBatch(quotes=quotes, failed=failed)


@pytest.fixture
def provider(monkeypatch) -> StubProvider:
    stub = StubProvider()
    monkeypatch.setattr(market_service, "get_provider", lambda: stub)
    return stub


def _tracked(db: Session, ticker: str, kind: str = "STOCK") -> Asset:
    """An asset someone asked to keep current, which is what gets refreshed."""
    asset = Asset(ticker=ticker, name=ticker, kind=kind, currency="BRL")
    db.add(asset)
    db.commit()
    db.add(WatchlistItem(ticker=ticker))
    db.commit()
    return asset


def _held(db: Session, portfolio: Portfolio, ticker: str, kind: str = "STOCK") -> Asset:
    """An open position, so the asset reaches the dashboard warning."""
    asset = Asset(ticker=ticker, name=ticker, kind=kind, currency="BRL")
    db.add(asset)
    db.commit()
    db.add(
        Transaction(
            portfolio_id=portfolio.id, asset_id=asset.id, broker_id=None, import_batch_id=None,
            trade_date=date(2024, 1, 2), direction="CREDIT", op_type="BUY", effect="ACQUIRE",
            quantity=Decimal(10), unit_price=Decimal(10), gross_amount=Decimal(100),
            fees=Decimal(0), taxes=Decimal(0), net_amount=Decimal(100), currency="BRL",
            fx_rate=None, raw_movement="Compra", raw_product=ticker, raw_institution="i",
            source_line=None, occurrence=0, dedup_key=f"{ticker}-buy",
        )
    )
    db.commit()
    return asset


def test_a_transient_failure_is_queued_not_reported_as_missing(db: Session, portfolio, provider):
    """The whole point: a timeout must be told apart from "no such price"."""
    asset = _tracked(db, "PETR4")
    provider.faults["PETR4.SA"] = "HTTP 429"

    result = market_service.refresh_quotes(db, portfolio.id, force=True)

    assert result["queued"] == ["PETR4"]
    assert result["missing"] == []  # not the same thing, and no longer conflated
    row = db.get(QuoteAttempt, asset.id)
    assert row is not None
    assert row.attempts == 1
    assert row.last_error == "HTTP 429"
    # First retry two minutes out: the user is looking at the screen right now.
    delay = row.next_attempt_at.replace(tzinfo=UTC) - datetime.now(UTC)
    assert timedelta(seconds=60) < delay <= timedelta(seconds=market_service.RETRY_SCHEDULE[0])


def test_an_unknown_symbol_is_never_queued(db: Session, portfolio, provider):
    """A delisted ticker must not be re-requested forever."""
    _tracked(db, "AESB3")  # the provider answers nothing for it

    result = market_service.refresh_quotes(db, portfolio.id, force=True)

    assert result["missing"] == ["AESB3"]
    assert result["queued"] == []
    assert db.scalars(select(QuoteAttempt)).all() == []


def test_backoff_grows_with_each_consecutive_failure(db: Session, portfolio, provider):
    asset = _tracked(db, "PETR4")
    provider.faults["PETR4.SA"] = "ReadTimeout"

    market_service.refresh_quotes(db, portfolio.id, force=True)
    market_service.refresh_quotes(db, portfolio.id, force=True)

    row = db.get(QuoteAttempt, asset.id)
    assert row.attempts == 2
    delay = row.next_attempt_at.replace(tzinfo=UTC) - datetime.now(UTC)
    assert delay <= timedelta(seconds=market_service.RETRY_SCHEDULE[1])
    assert delay > timedelta(seconds=market_service.RETRY_SCHEDULE[0])


def test_a_recovered_price_empties_the_queue(db: Session, portfolio, provider):
    asset = _tracked(db, "PETR4")
    provider.faults["PETR4.SA"] = "HTTP 503"
    market_service.refresh_quotes(db, portfolio.id, force=True)

    # The provider comes good, and the retry falls due.
    provider.faults.clear()
    provider.prices["PETR4.SA"] = Decimal("38.20")
    row = db.get(QuoteAttempt, asset.id)
    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    result = market_service.retry_pending_quotes(db)

    assert result["recovered"] == 1
    assert result["recovered_tickers"] == ["PETR4"]
    assert db.get(QuoteAttempt, asset.id) is None
    assert db.get(Quote, asset.id).price == Decimal("38.20")


def test_a_retry_that_is_not_due_yet_is_left_alone(db: Session, portfolio, provider):
    _tracked(db, "PETR4")
    provider.faults["PETR4.SA"] = "HTTP 429"
    market_service.refresh_quotes(db, portfolio.id, force=True)
    provider.calls.clear()

    assert market_service.retry_pending_quotes(db)["retried"] == 0
    assert provider.calls == []  # the queue is a schedule, not a spin loop


def test_a_retry_answered_unknown_stops_being_queued(db: Session, portfolio, provider):
    """Late is not the same as absent, and the queue must notice the change."""
    asset = _tracked(db, "XPTO3")
    provider.faults["XPTO3.SA"] = "HTTP 502"
    market_service.refresh_quotes(db, portfolio.id, force=True)

    provider.faults.clear()  # this time the provider simply does not know it
    row = db.get(QuoteAttempt, asset.id)
    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    market_service.retry_pending_quotes(db)

    assert db.get(QuoteAttempt, asset.id) is None


def test_a_queued_asset_is_not_reported_as_unpriced(db: Session, portfolio, provider):
    """The user-visible half: no alarm about a price that is merely late."""
    from app.portfolio.service import PortfolioService

    asset = _held(db, portfolio, "NKE")
    provider.faults["NKE.SA"] = "HTTP 429"
    market_service.refresh_quotes(db, portfolio.id, force=True)

    overview = PortfolioService(db, portfolio.id).overview()

    assert "NKE" not in overview["unpriced_positions"]
    assert overview["pending_quotes"] == ["NKE"]

    # ...but once the schedule is spent, it is reported honestly.
    row = db.get(QuoteAttempt, asset.id)
    row.attempts = len(market_service.RETRY_SCHEDULE) + 1
    db.commit()
    from app.portfolio.service import clear_replay_cache

    clear_replay_cache()
    overview = PortfolioService(db, portfolio.id).overview()
    assert overview["unpriced_positions"] == ["NKE"]
    assert overview["pending_quotes"] == []


def test_treasury_is_never_reported_as_missing_a_quote(db: Session, portfolio):
    """We never ask for a Tesouro quote, so complaining about it made no sense."""
    from app.portfolio.service import PortfolioService

    _held(db, portfolio, "TESOURO-RENDA-APOSENTADORIA-EXTRA-2065", kind="TREASURY")

    overview = PortfolioService(db, portfolio.id).overview()

    assert overview["unpriced_positions"] == []


def test_the_notification_feed_shows_the_queue(db: Session, portfolio, provider):
    from app.services.notifications import feed

    _tracked(db, "PETR4")
    _tracked(db, "VALE3")
    provider.prices["VALE3.SA"] = Decimal("60")
    provider.faults["PETR4.SA"] = "HTTP 429"
    market_service.refresh_quotes(db, portfolio.id, force=True)

    items = feed(db, portfolio.id)

    assert [item["kind"] for item in items] == ["quotes.retry"]
    entry = items[0]
    assert entry["items"] == ["PETR4"]
    assert entry["progress"] == {"done": 1, "total": 2, "label": "atualizadas"}
    assert entry["at"] is not None


def test_the_feed_is_empty_when_nothing_is_wrong(db: Session, portfolio, provider):
    from app.services.notifications import feed

    _tracked(db, "PETR4")
    provider.prices["PETR4.SA"] = Decimal("38")
    market_service.refresh_quotes(db, portfolio.id, force=True)

    assert feed(db, portfolio.id) == []


# --- The other half: the provider retrying before it ever reports a failure ---


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """Serves a scripted sequence; an ``Exception`` in it is raised instead."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, headers=None):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


OK = FakeResponse(200, {"chart": {"result": [{"meta": {"regularMarketPrice": 10}}]}})


@pytest.fixture
def no_sleep(monkeypatch):
    """Backoff is the behaviour under test; waiting for it is not."""
    import app.market.providers as providers

    monkeypatch.setattr(providers.time, "sleep", lambda _seconds: None)


def test_a_throttled_symbol_is_retried_before_being_given_up_on(no_sleep):
    from app.market.providers import YahooChartProvider

    client = FakeClient([FakeResponse(429, headers={"Retry-After": "1"}), OK])

    payload = YahooChartProvider()._fetch(client, "PETR4.SA", {})

    assert payload is not None
    assert client.calls == 2  # the first sync no longer loses to one 429


def test_a_symbol_that_stays_throttled_is_reported_as_transient(no_sleep):
    from app.market.providers import MAX_ATTEMPTS, TransientFetchError, YahooChartProvider

    client = FakeClient([FakeResponse(429) for _ in range(MAX_ATTEMPTS)])

    with pytest.raises(TransientFetchError):
        YahooChartProvider()._fetch(client, "PETR4.SA", {})
    assert client.calls == MAX_ATTEMPTS


def test_a_timeout_is_transient(no_sleep):
    import httpx

    from app.market.providers import TransientFetchError, YahooChartProvider

    client = FakeClient([httpx.ReadTimeout("too slow"), OK])

    assert YahooChartProvider()._fetch(client, "NKE", {}) is not None
    assert client.calls == 2

    client = FakeClient([httpx.ConnectError("no route")] * 3)
    with pytest.raises(TransientFetchError):
        YahooChartProvider()._fetch(client, "NKE", {})


def test_an_unknown_symbol_is_not_retried(no_sleep):
    """A 404 is an answer, not a fault — retrying it would never end."""
    from app.market.providers import YahooChartProvider

    client = FakeClient([FakeResponse(404)])

    assert YahooChartProvider()._fetch(client, "NOPE", {}) is None
    assert client.calls == 1
