"""Crypto exchange imports.

The B3 export and the broker statements both describe a market where cash is the
counterparty of every trade. An exchange does not: it reports swaps between two
tracked assets, prices everything in dollars (or in a stablecoin standing in for
them), and never mentions the money sitting in the account. This package turns
that into the same normalised movements the rest of the app already consumes —
see :mod:`app.importer.crypto.base` for the model and
:meth:`app.importer.service.ImportService.import_crypto_csv` for how a swap
becomes one, two or three transactions.
"""
from __future__ import annotations

from app.importer.crypto.base import (
    CryptoEvent,
    CryptoFormatError,
    CryptoTrade,
    ParsedTradeFile,
    parse_amount,
    parse_timestamp,
)
from app.importer.crypto.registry import (
    available_formats,
    parse_crypto_csv,
    sniff_format,
)

__all__ = [
    "CryptoEvent",
    "CryptoFormatError",
    "CryptoTrade",
    "ParsedTradeFile",
    "available_formats",
    "parse_amount",
    "parse_crypto_csv",
    "parse_timestamp",
    "sniff_format",
]
