"""Picks the parser that understands a given exchange CSV.

Unlike the PDF side there is no document layout to sniff — an exchange export is
just a header row, so the header *is* the signature. A file that no parser
claims is not an error here: it is almost certainly the B3 export, which the
upload endpoint hands to :mod:`app.importer.parser` instead.
"""
from __future__ import annotations

from types import ModuleType

from app.importer.crypto import binance, binance_ledger
from app.importer.crypto.base import CryptoFormatError, ParsedTradeFile

#: Most specific first. The ledger is tried ahead of the spot exports because it
#: is the richer file and its header is unambiguous.
PARSERS: tuple[ModuleType, ...] = (binance_ledger, binance)


def sniff_format(payload: bytes | str) -> str | None:
    """The format id of an exchange export, or ``None`` when it is not one."""
    for parser in PARSERS:
        fmt = parser.matches(payload)
        if fmt:
            return fmt
    return None


def parse_crypto_csv(payload: bytes | str) -> ParsedTradeFile:
    """Read an exchange export into a :class:`ParsedTradeFile`."""
    for parser in PARSERS:
        if parser.matches(payload):
            return parser.parse(payload)
    raise CryptoFormatError(
        "formato de exportação de corretora cripto não reconhecido — os formatos suportados "
        "são o histórico de transações da Binance (recomendado) e os históricos de trades e "
        "de ordens spot"
    )


def available_formats() -> list[dict]:
    """Descriptions of the supported exports (Settings/diagnostics page)."""
    return [
        {"format": binance_ledger.LEDGER_FORMAT, "broker": binance_ledger.EXCHANGE},
        {"format": binance.TRADES_FORMAT, "broker": binance.EXCHANGE},
        {"format": binance.ORDERS_FORMAT, "broker": binance.EXCHANGE},
    ]
