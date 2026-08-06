"""Binance spot exports.

Two files come out of *Orders → Spot Order History*, and they describe the same
events at different resolutions:

``Binance-Spot-Trade-History-….csv``
    One row per **fill**, with the fee actually charged. This is the record of
    what happened and the one worth importing.

``Binance-Spot-Order-History-….csv``
    One row per **order**, aggregating its fills, with no fee column and with
    orders that never executed. Useful only when the trade file is missing.

Both are supported because a user exports whichever tab they were looking at,
and the import service reconciles them by quantity so having both loaded does
not book the same purchase twice (see ``ImportService._crypto_coverage``).

Format notes
------------
* Every quantity carries its unit glued to the number (``0.00308BTC``,
  ``1000.8306BRL``). That is what makes ``USDTBRL`` unambiguous — the pair
  column alone cannot say where the base ends and the quote begins.
* Headers carry footnote marks (``Type¹``, ``Executed²``, ``Trading total³``)
  and the order export repeats the ``Time`` column twice, so columns are matched
  on a normalised prefix rather than by exact name.
* Times are local to the account's timezone (the filename says which); only the
  date is used, so the offset never changes which day a trade lands on by more
  than the export itself already decided.
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

from app.importer.crypto.base import (
    CryptoFormatError,
    CryptoTrade,
    ParsedTradeFile,
    parse_amount,
    parse_timestamp,
)
from app.importer.crypto.symbols import split_pair
from app.importer.parser import normalize_key

EXCHANGE = "Binance"

TRADES_FORMAT = "binance-spot-trades"
ORDERS_FORMAT = "binance-spot-orders"

#: Columns that identify each export, after normalisation.
_TRADE_COLUMNS = frozenset({"time", "pair", "side", "executed", "amount", "fee"})
_ORDER_COLUMNS = frozenset({"time", "orderno", "pair", "side", "executed", "tradingtotal"})


def _canonical(header: str) -> str:
    """``"Trading total³"`` -> ``"tradingtotal"``.

    ``normalize_key`` folds the footnote mark into a digit (NFKD turns ``³``
    into ``3``), so the trailing digits are stripped off again. No real column
    in these exports ends in a number.
    """
    return normalize_key(header).rstrip("0123456789")


def _columns(header: list[str]) -> dict[str, int]:
    """Column name -> index, first occurrence winning.

    The order export prints ``Time`` twice — the order's own time and the time
    of its last fill. The first is the one every other column is about.
    """
    mapping: dict[str, int] = {}
    for index, name in enumerate(header):
        key = _canonical(name)
        if key and key not in mapping:
            mapping[key] = index
    return mapping


def _read(payload: bytes | str) -> tuple[list[str], list[list[str]]]:
    from app.importer.parser import decode_bytes, sniff_delimiter

    text = decode_bytes(payload) if isinstance(payload, (bytes, bytearray)) else payload
    text = text.lstrip("﻿")
    if not text.strip():
        raise CryptoFormatError("the file is empty")
    reader = csv.reader(io.StringIO(text), delimiter=sniff_delimiter(text))
    rows = [row for row in reader if any((cell or "").strip() for cell in row)]
    if not rows:
        raise CryptoFormatError("the file has no rows")
    return rows[0], rows[1:]


def detect_format(header: list[str]) -> str | None:
    """Which Binance export this is, or ``None`` when it is neither."""
    columns = set(_columns(header))
    if _TRADE_COLUMNS <= columns and "orderno" not in columns:
        return TRADES_FORMAT
    if _ORDER_COLUMNS <= columns:
        return ORDERS_FORMAT
    return None


def matches(payload: bytes | str) -> str | None:
    try:
        header, _ = _read(payload)
    except CryptoFormatError:
        return None
    return detect_format(header)


def parse(payload: bytes | str) -> ParsedTradeFile:
    """Read either Binance spot export into a :class:`ParsedTradeFile`."""
    header, rows = _read(payload)
    fmt = detect_format(header)
    if fmt is None:
        raise CryptoFormatError(
            "unexpected CSV layout — expected a Binance spot trade or order history export, "
            f"found columns: {', '.join(h for h in header if h)}"
        )
    columns = _columns(header)
    result = ParsedTradeFile(format=fmt, exchange=EXCHANGE, total_rows=len(rows))

    # The two exports differ only in which column holds the executed quantity,
    # the amount paid and the price, so one loop reads both.
    amount_column = "amount" if fmt == TRADES_FORMAT else "tradingtotal"
    price_column = "price" if fmt == TRADES_FORMAT else "averageprice"

    for offset, row in enumerate(rows, start=2):  # line 1 is the header

        def cell(name: str) -> str:
            index = columns.get(name)
            return (row[index] or "").strip() if index is not None and index < len(row) else ""

        raw_text = ",".join(row)[:200]
        try:
            quantity, base_symbol = parse_amount(cell("executed"))
            amount, quote_symbol = parse_amount(cell(amount_column))
            side = cell("side").strip().upper()

            if quantity is None or amount is None or quantity <= 0 or amount <= 0:
                # An order that never filled, or filled for nothing. Counted so
                # the import log's arithmetic adds up, not treated as an error:
                # a cancelled order is a normal thing to find in this export.
                result.skipped_rows += 1
                continue
            if side not in {"BUY", "SELL"}:
                raise CryptoFormatError(f"unrecognised side {side!r}")

            pair = cell("pair").strip().upper()
            base_symbol, quote_symbol = split_pair(pair, base_symbol, quote_symbol)
            if not base_symbol or not quote_symbol:
                raise CryptoFormatError(f"could not split the pair {pair!r} into two symbols")

            fee_amount, fee_symbol = parse_amount(cell("fee"))
            price, _ = parse_amount(cell(price_column))
            executed_at = parse_timestamp(cell("time"))

            result.trades.append(
                CryptoTrade(
                    trade_date=executed_at.date(),
                    executed_at=executed_at,
                    base_symbol=base_symbol,
                    base_quantity=quantity,
                    quote_symbol=quote_symbol,
                    quote_amount=amount,
                    side=side,
                    price=price,
                    fees=(
                        ((fee_amount, fee_symbol),)
                        if fee_amount and fee_amount > 0 and fee_symbol
                        else ()
                    ),
                    pair=pair,
                    order_ref=cell("orderno"),
                    line_number=offset,
                    raw_text=raw_text,
                )
            )
        except Exception as exc:  # noqa: BLE001 — one bad row must not kill the import
            result.errors.append({"line": offset, "error": str(exc), "raw": raw_text})

    if fmt == ORDERS_FORMAT:
        result.warnings.append(
            "Binance order history carries no trading fees — import the spot trade history "
            "for the exact cost of each purchase"
        )
    return result
