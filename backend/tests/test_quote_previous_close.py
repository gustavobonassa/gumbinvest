"""The quote's previous close must be the previous *session's* close.

Yahoo's chart endpoint reports ``meta.chartPreviousClose`` relative to the
requested range: it is the close immediately before the window starts, not
before the latest session. Asking for five days therefore answers with a
five-day-old price, and every "variação do dia" in the app silently becomes a
five-day move — a stock that fell 11% three days ago and rose 1% today reads
as -12.9%.

Nothing about that failure is visible in the response: the field is present
and plausible, only measured from the wrong day. These tests pin the range, so
widening it for any other reason has to fail here first.
"""
from __future__ import annotations

from decimal import Decimal

import pytest


class FakeResponse:
    status_code = 200
    headers: dict = {}

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class RecordingClient:
    """Captures the query params every quote fetch sends."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.params: list[dict] = []

    def __enter__(self) -> "RecordingClient":
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def get(self, url, params=None, headers=None):  # noqa: ANN001 — httpx signature
        self.params.append(dict(params or {}))
        return FakeResponse(self.payload)


def test_the_quote_range_is_one_day() -> None:
    """A wider range makes chartPreviousClose older than the last session."""
    from app.market.providers import YahooChartProvider

    assert YahooChartProvider.QUOTE_PARAMS["range"] == "1d"


def test_previous_close_comes_from_the_last_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """MPT's real numbers on the day the five-day range was found.

    4.02 was the previous session's close and 4.66 the close from a week
    earlier; the day's move is +1%, not -12.9%.
    """
    import app.market.providers as providers

    client = RecordingClient(
        {"chart": {"result": [{"meta": {"regularMarketPrice": 4.06, "chartPreviousClose": 4.02}}]}}
    )
    monkeypatch.setattr(providers.httpx, "Client", lambda **_kwargs: client)

    quotes = providers.YahooChartProvider().fetch_quotes(["MPT"]).quotes

    assert client.params, "no request was made"
    # The range is what decides which close the provider answers with.
    assert client.params[0]["range"] == "1d"

    quote = quotes["MPT"]
    assert quote.previous_close == Decimal("4.02")
    assert quote.change == Decimal("4.06") - Decimal("4.02")
    assert quote.change_percent is not None
    assert round(quote.change_percent, 1) == Decimal("1.0")
