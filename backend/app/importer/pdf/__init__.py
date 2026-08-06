"""Broker statement (PDF) importers.

The B3 export is a CSV; every other broker in this portfolio ships PDFs, and
each one changed layout at least once. This package turns any of them into the
same :class:`~app.importer.pdf.base.StatementRow` stream that the import service
already knows how to persist.

See :mod:`app.importer.pdf.registry` for how a file is matched to a parser.
"""
from __future__ import annotations

from app.importer.pdf.base import (
    ParsedStatement,
    PdfFormatError,
    StatementParser,
    StatementRow,
)
from app.importer.pdf.registry import available_formats, parse_pdf, sniff_parser

__all__ = [
    "ParsedStatement",
    "PdfFormatError",
    "StatementParser",
    "StatementRow",
    "available_formats",
    "parse_pdf",
    "sniff_parser",
]
