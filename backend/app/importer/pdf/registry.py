"""Picks the parser that understands a given statement PDF.

Order matters: the two Apex families are tested before the broker-branded
formats, because an Avenue statement mentions Apex in its footer disclosures and
would otherwise be claimed by the wrong parser.
"""
from __future__ import annotations

from app.importer.pdf.apex_ascend import ApexAscendParser
from app.importer.pdf.apex_en import ApexEnglishParser
from app.importer.pdf.avenue_pt import AvenuePortugueseParser
from app.importer.pdf.base import ParsedStatement, PdfFormatError, StatementParser
from app.importer.pdf.drivewealth import DriveWealthParser
from app.importer.pdf.layout import Document, load_document

#: Most specific first.
PARSERS: tuple[StatementParser, ...] = (
    ApexAscendParser(),
    AvenuePortugueseParser(),
    DriveWealthParser(),
    ApexEnglishParser(),
)


def sniff_parser(document: Document) -> StatementParser | None:
    return next((parser for parser in PARSERS if parser.matches(document)), None)


def parse_pdf(payload: bytes) -> ParsedStatement:
    """Read a statement PDF into a :class:`ParsedStatement`."""
    try:
        document = load_document(payload)
    except Exception as exc:  # noqa: BLE001 — a corrupt upload is a user error
        raise PdfFormatError(f"não foi possível ler o PDF: {exc}") from exc

    if not document.pages:
        raise PdfFormatError("o PDF está vazio")

    parser = sniff_parser(document)
    if parser is None:
        raise PdfFormatError(
            "formato de extrato não reconhecido — os formatos suportados são "
            "Avenue (Apex e Avenue Securities) e Nomad (DriveWealth e Apex Ascend)"
        )

    statement = parser.parse(document)
    if statement.period_start is None or statement.period_end is None:
        raise PdfFormatError(
            f"não foi possível identificar o período do extrato ({parser.format})"
        )
    return statement


def available_formats() -> list[dict]:
    """Descriptions of the supported formats (Settings/diagnostics page)."""
    return [
        {"format": parser.format, "broker": parser.broker or "Avenue / Nomad"}
        for parser in PARSERS
    ]
