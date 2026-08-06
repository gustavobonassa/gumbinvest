"""13F parsing and the /investors endpoints. All network is mocked."""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.market import superinvestors

INFO_TABLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>1000000</value>
    <shrsOrPrnAmt><sshPrnamt>5000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>500000</value>
    <shrsOrPrnAmt><sshPrnamt>2500</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>COCA COLA CO</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>191216100</cusip>
    <value>750000</value>
    <shrsOrPrnAmt><sshPrnamt>10000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>SOME BANK CORP</nameOfIssuer>
    <titleOfClass>CALL</titleOfClass>
    <cusip>999999999</cusip>
    <value>900000</value>
    <shrsOrPrnAmt><sshPrnamt>1</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall>Call</putCall>
  </infoTable>
</informationTable>
"""


class _FakeResponse:
    def __init__(self, payload=None, content: bytes = b""):
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Answers the exact EDGAR URLs the module hits."""

    def __init__(self, routes: dict):
        self.routes = routes

    def get(self, url, headers=None):
        for needle, response in self.routes.items():
            if needle in url:
                return response
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture(autouse=True)
def _fresh_cache():
    superinvestors.clear_cache()
    yield
    superinvestors.clear_cache()


def test_holdings_aggregates_and_skips_options():
    client = _FakeClient(
        {
            "index.json": _FakeResponse(
                {"directory": {"item": [
                    {"name": "primary_doc.xml", "size": "5000"},
                    {"name": "table.xml", "size": "45000"},
                ]}}
            ),
            "table.xml": _FakeResponse(content=INFO_TABLE_XML),
        }
    )
    rows = superinvestors._holdings(client, "0001067983", "0001-11-000001")
    assert set(rows) == {"037833100", "191216100"}  # the call option never enters
    apple = rows["037833100"]
    assert apple["value"] == Decimal(1_500_000)  # two manager rows folded together
    assert apple["shares"] == Decimal(7_500)


def test_normalize_survives_13f_spelling():
    normalize = superinvestors._normalize
    assert normalize("BANK AMERICA CORP") == normalize("Bank of America Corp")
    assert normalize("COCA COLA CO") == normalize("Coca-Cola Co")
    assert normalize("Apple Inc.") == "APPLE"


def test_registry_lists_investors(db):
    with TestClient(app) as client:
        body = client.get("/api/investors").json()
    slugs = {item["slug"] for item in body}
    assert {"buffett", "burry", "ackman"} <= slugs
    assert all(item["manager"] and item["description"] for item in body)


def test_wallet_endpoint_serves_parsed_payload(monkeypatch, db):
    def fake_wallet(slug: str) -> dict:
        if slug != "buffett":
            raise KeyError(slug)
        return {"slug": slug, "holdings": [], "total_value": 0.0}

    monkeypatch.setattr(superinvestors, "wallet", fake_wallet)
    with TestClient(app) as client:  # noqa: F841 — db fixture creates the schema
        assert client.get("/api/investors/buffett").json()["slug"] == "buffett"
        assert client.get("/api/investors/nobody").status_code == 404


def test_unknown_slug_is_404(db):
    with TestClient(app) as client:
        assert client.get("/api/investors/zezinho").status_code == 404
