"""The Binance pipeline's own file format, and how it meets the manual exports.

The API and an uploaded ledger describe the same account, so the property that
matters most is that importing both, in either order, books every event once.
All data here is synthetic.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.models import FxRate, Transaction
from app.importer.crypto import parse_crypto_csv, sniff_format
from app.importer.crypto.binance_api import API_FORMAT, write
from app.importer.crypto.binance_ledger import LEDGER_FORMAT
from app.importer.service import ImportService
from app.portfolio.service import PortfolioService

#: The same month as the ledger below, as the API reports it: a BRL deposit
#: (cash, not held), one fill, a staking round trip and a reward.
API_ROWS = [
    {"Source": "fiat-orders", "Time": "2024-03-01 09:00:00", "Operation": "Fiat Deposit",
     "Coin": "BRL", "Change": "5000", "Ref": "fiat:1"},
    {"Source": "myTrades", "Time": "2024-03-02 10:00:00", "Operation": "Spot Trade",
     "Coin": "DOT", "Change": "100", "Pair": "DOTBRL", "Side": "BUY", "Quote_Coin": "BRL",
     "Quote_Qty": "5000", "Price": "50", "Fee": "0.1", "Fee_Coin": "DOT", "Ref": "DOTBRL:11"},
    {"Source": "earn", "Time": "2024-03-03 11:00:00", "Operation": "Staking Purchase",
     "Coin": "DOT", "Change": "-99.9", "Ref": "sub:1"},
    {"Source": "earn", "Time": "2024-03-10 13:00:00", "Operation": "Staking Rewards",
     "Coin": "DOT", "Change": "2", "Ref": "rew:1"},
    {"Source": "earn", "Time": "2024-03-20 14:00:00", "Operation": "Staking Redemption",
     "Coin": "DOT", "Change": "99.9", "Ref": "red:1"},
]

LEDGER = (
    "User_ID,UTC_Time,Account,Operation,Coin,Change,Remark\n"
    "1,2024-03-01 09:00:00,Spot,Deposit,BRL,5000,\n"
    "1,2024-03-02 10:00:00,Spot,Transaction Buy,DOT,100,\n"
    "1,2024-03-02 10:00:00,Spot,Transaction Spend,BRL,-5000,\n"
    "1,2024-03-02 10:00:00,Spot,Transaction Fee,DOT,-0.1,\n"
    "1,2024-03-03 11:00:00,Spot,Staking Purchase,DOT,-99.9,\n"
    "1,2024-03-10 12:00:00,Earn,Simple Earn Locked - Rewards Income,DOT,2,\n"
    "1,2024-03-10 13:00:00,Spot,Staking Rewards,DOT,2,\n"
    "1,2024-03-20 14:00:00,Spot,Staking Redemption,DOT,99.9,\n"
)


@pytest.fixture
def rates(db):
    day = date(2024, 1, 1)
    while day <= date(2024, 12, 31):
        db.add(FxRate(base="USD", quote="BRL", date=day, rate=Decimal("5")))
        day += timedelta(days=1)
    db.commit()


def _import(db, portfolio, payload: bytes | str, name: str):
    raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    return ImportService(db, portfolio).import_crypto_csv(raw, name)


def _position(db, portfolio, ticker: str):
    service = PortfolioService(db, portfolio.id)
    assets = service.assets()
    return next(p for aid, p in service.positions().items() if assets[aid].ticker == ticker)


def _count(db) -> int:
    return db.scalar(select(func.count()).select_from(Transaction))


def test_the_api_file_is_recognised_as_its_own_format():
    assert sniff_format(write(API_ROWS)) == API_FORMAT
    assert sniff_format(LEDGER) == LEDGER_FORMAT


def test_a_fill_becomes_a_trade_with_its_fee(db, portfolio, rates):
    _import(db, portfolio, write(API_ROWS), "binance-api.csv")
    position = _position(db, portfolio, "DOT")
    assert position.quantity == Decimal("101.9")  # 100 - 0,1 fee + 2 reward
    assert position.cost_basis == Decimal("1000")  # R$ 5.000 at 5,00


def test_reimporting_the_api_file_adds_nothing(db, portfolio, rates):
    first = _import(db, portfolio, write(API_ROWS), "binance-api-1.csv")
    assert first.rows_imported > 0
    again = _import(db, portfolio, write(API_ROWS), "binance-api-2.csv")
    assert again.rows_imported == 0


@pytest.mark.parametrize("order", ["ledger-first", "api-first"])
def test_ledger_and_api_book_each_event_once(db, portfolio, rates, order):
    """An uploaded ledger and an API run of the same weeks must not add up."""
    sources = [(LEDGER, "ledger.csv"), (write(API_ROWS), "binance-api.csv")]
    if order == "api-first":
        sources.reverse()

    _import(db, portfolio, *sources[0])
    booked = _count(db)
    second = _import(db, portfolio, *sources[1])

    assert second.rows_imported == 0
    assert _count(db) == booked
    position = _position(db, portfolio, "DOT")
    assert position.quantity == Decimal("101.9")
    assert position.cost_basis == Decimal("1000")


def test_a_convert_reads_like_the_ledger_reads_it(db, portfolio, rates):
    rows = [
        {"Source": "convert", "Time": "2024-04-01 10:00:00", "Operation": "Binance Convert",
         "Coin": "ETH", "Change": "0.05", "Quote_Coin": "USDT", "Quote_Qty": "100", "Ref": "cv:1"},
    ]
    parsed = parse_crypto_csv(write(rows))
    (trade,) = parsed.trades
    assert (trade.side, trade.base_symbol, trade.quote_symbol) == ("BUY", "ETH", "USDT")
    assert trade.base_quantity == Decimal("0.05")
    assert trade.quote_amount == Decimal("100")


def test_a_bad_row_is_reported_without_losing_the_rest():
    rows = [
        {**API_ROWS[1], "Side": "SIDEWAYS"},
        API_ROWS[3],
    ]
    parsed = parse_crypto_csv(write(rows))
    assert len(parsed.errors) == 1 and parsed.errors[0]["line"] == 2
    assert len(parsed.events) == 1


def test_a_fiat_only_trade_holds_nothing():
    rows = [
        {"Source": "myTrades", "Time": "2024-05-01 10:00:00", "Operation": "Spot Trade",
         "Coin": "EUR", "Change": "10", "Pair": "EURBRL", "Side": "BUY", "Quote_Coin": "BRL",
         "Quote_Qty": "60", "Ref": "EURBRL:1"},
    ]
    parsed = parse_crypto_csv(write(rows))
    assert parsed.trades == [] and parsed.skipped_rows == 1
