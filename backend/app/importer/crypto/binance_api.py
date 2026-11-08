"""Binance account history collected through the API (the Binance pipeline).

The API has no endpoint that returns the *Transaction History* ledger, so
:mod:`app.pipelines.binance` assembles the account's history from the
endpoints that exist: spot fills, Convert, deposits, withdrawals, fiat
purchases, Simple Earn, distributions and dust. It writes what it collected to
a CSV in this format, and the import goes through this parser, which keeps the
pipeline bargain: the pipeline produces a file and the importer reads it. That
file, and the line each movement came from, is the audit trail.

One row per record::

    Source,Time,Timezone,Operation,Coin,Change,Pair,Side,Quote_Coin,Quote_Qty,Price,Fee,Fee_Coin,Ref

``Time`` is in the zone named by ``Timezone`` (``UTC-03:00``), not UTC. The
pipeline writes it in the zone of the user's own Binance exports, because the
reconciliation with them works day by day: Binance writes an export in the
account's local time (``…(UTC--3)….csv``), and a movement at 22:00 in São Paulo
is the next day in UTC.

``Operation`` decides how a row is read:

``Spot Trade``
    One fill from ``/api/v3/myTrades``. ``Coin``/``Change`` is the base asset
    and quantity, ``Quote_Coin``/``Quote_Qty`` the other side, ``Side`` is BUY or
    SELL and ``Fee``/``Fee_Coin`` the commission. A fill is a trade on its own,
    so nothing is grouped by timestamp. The ledger parser has to group by
    timestamp, and two symbols filled in the same second defeat that.

``Binance Convert``
    One Convert order. ``Coin``/``Change`` is what arrived and
    ``Quote_Coin``/``Quote_Qty`` what was given. Which of the two is the
    instrument is decided exactly as the ledger decides it, so the same Convert
    reads the same from either source.

anything else
    A single balance change worded the way the Binance ledger words it
    (``Deposit``, ``Withdraw``, ``Simple Earn Flexible Interest``…), with a signed
    ``Change``. These go through the ledger's own operation table, so a reward
    is a reward and an Earn subscription is an Earn transfer whichever file it
    arrived in.

Why a format of its own rather than the ledger's
------------------------------------------------
The same events are often already on file from an uploaded ledger or spot
export. The importer reconciles a day's totals per asset only against rows
from a *different* source format (``ImportService._crypto_coverage``). That is
what lets an API run and a ledger upload of the same weeks meet without either
being booked twice, and it only works if the two formats are different.
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation

from app.importer.crypto import binance_ledger as ledger
from app.importer.crypto.base import (
    CryptoFormatError,
    CryptoTrade,
    ParsedTradeFile,
    parse_timestamp,
)
from app.importer.crypto.symbols import is_tracked
from app.importer.parser import normalize_key

EXCHANGE = "Binance"
API_FORMAT = "binance-api"

COLUMNS = (
    "Source",
    "Time",
    "Timezone",
    "Operation",
    "Coin",
    "Change",
    "Pair",
    "Side",
    "Quote_Coin",
    "Quote_Qty",
    "Price",
    "Fee",
    "Fee_Coin",
    "Ref",
)

TRADE_OPERATION = "Spot Trade"
CONVERT_OPERATION = "Binance Convert"

#: The columns that identify this file. ``Source`` and ``Ref`` are what set it
#: apart from the ledger export, which shares the time/operation/coin/change
#: columns.
_SIGNATURE = frozenset({"source", "time", "timezone", "operation", "coin", "change", "ref"})

#: The account every API row belongs to. The ledger parser keys its mirror and
#: derivatives rules on the account name; everything collected here is spot.
_ACCOUNT = "Spot"


def _read(payload: bytes | str) -> tuple[list[str], list[list[str]]]:
    from app.importer.parser import decode_bytes

    text = decode_bytes(payload) if isinstance(payload, (bytes, bytearray)) else payload
    text = text.lstrip("﻿")
    if not text.strip():
        raise CryptoFormatError("the file is empty")
    try:
        rows = [row for row in csv.reader(io.StringIO(text)) if any((cell or "").strip() for cell in row)]
    except csv.Error as exc:  # not delimited text at all — e.g. a binary file
        raise CryptoFormatError(f"the file is not readable as CSV: {exc}") from exc
    if not rows:
        raise CryptoFormatError("the file has no rows")
    return rows[0], rows[1:]


def _columns(header: list[str]) -> dict[str, int]:
    return {normalize_key(name): index for index, name in enumerate(header) if name}


def matches(payload: bytes | str) -> str | None:
    try:
        header, _ = _read(payload)
    except CryptoFormatError:
        return None
    return API_FORMAT if _SIGNATURE <= set(_columns(header)) else None


def write(rows: list[dict]) -> bytes:
    """Serialise collected records into this format (the pipeline's side)."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _cell(row.get(key)) for key in COLUMNS})
    return buffer.getvalue().encode("utf-8")


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def _decimal(text: str, column: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise CryptoFormatError(f"{column} is not a number: {text!r}") from exc


def parse(payload: bytes | str) -> ParsedTradeFile:
    """Read a pipeline-written Binance API file into a :class:`ParsedTradeFile`."""
    header, rows = _read(payload)
    if matches(payload) is None:
        raise CryptoFormatError(
            "unexpected CSV layout — expected a file written by the Binance pipeline, "
            f"found columns: {', '.join(h for h in header if h)}"
        )
    columns = _columns(header)
    result = ParsedTradeFile(format=API_FORMAT, exchange=EXCHANGE, total_rows=len(rows))

    for offset, row in enumerate(rows, start=2):  # line 1 is the header

        def cell(name: str) -> str:
            index = columns.get(name)
            return (row[index] or "").strip() if index is not None and index < len(row) else ""

        text = ",".join(row)[:200]
        try:
            at = parse_timestamp(cell("time"))
            operation = cell("operation")
            coin = cell("coin").upper()
            change = _decimal(cell("change"), "Change")
            if change == 0:
                result.skipped_rows += 1
                continue

            if operation == TRADE_OPERATION:
                trade = _spot_trade(cell, at, coin, change, offset, text)
                if is_tracked(trade.base_symbol):
                    result.trades.append(trade)
                else:
                    # Both sides were cash (a fiat-to-fiat pair). Nothing to hold.
                    result.skipped_rows += 1
                continue

            if operation == CONVERT_OPERATION:
                quote_coin = cell("quotecoin").upper()
                given = _decimal(cell("quoteqty"), "Quote_Qty")
                legs = [
                    ledger._Row(at, _ACCOUNT, normalize_key(operation), operation, coin, abs(change), offset, text),
                    ledger._Row(at, _ACCOUNT, normalize_key(operation), operation, quote_coin, -abs(given), offset, text),
                ]
                trade = ledger._as_trade(legs)
                if trade is None:
                    raise CryptoFormatError("a Convert needs one coin in and one coin out")
                if is_tracked(trade.base_symbol):
                    result.trades.append(trade)
                else:
                    result.skipped_rows += 1
                continue

            single = ledger._Row(at, _ACCOUNT, normalize_key(operation), operation, coin, change, offset, text)
            result.events.extend(ledger._ledger_event(single, result))
        except Exception as exc:  # noqa: BLE001 — one bad row must not kill the import
            result.errors.append({"line": offset, "error": str(exc), "raw": text})
    return result


def _spot_trade(cell, at, base: str, quantity: Decimal, line: int, text: str) -> CryptoTrade:
    side = cell("side").upper()
    if side not in {"BUY", "SELL"}:
        raise CryptoFormatError(f"unrecognised side {side!r}")
    quote = cell("quotecoin").upper()
    amount = _decimal(cell("quoteqty"), "Quote_Qty")
    if not base or not quote or amount <= 0:
        raise CryptoFormatError("a spot trade needs both coins and a positive amount")
    fee_text = cell("fee")
    fee = _decimal(fee_text, "Fee") if fee_text else Decimal(0)
    fee_coin = cell("feecoin").upper()
    price_text = cell("price")
    return CryptoTrade(
        trade_date=at.date(),
        executed_at=at,
        base_symbol=base,
        base_quantity=abs(quantity),
        quote_symbol=quote,
        quote_amount=amount,
        side=side,
        price=_decimal(price_text, "Price") if price_text else None,
        fees=((fee, fee_coin),) if fee > 0 and fee_coin else (),
        pair=cell("pair").upper() or f"{base}{quote}",
        order_ref=cell("ref"),
        line_number=line,
        raw_text=text,
    )
