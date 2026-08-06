"""Parsing of the B3 export dialect: numbers, dates, products, brokers, XLSX."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.domain.enums import AssetKind
from app.importer.parser import (
    CsvFormatError,
    is_xlsx,
    normalize_broker,
    parse_csv,
    parse_date,
    parse_decimal,
    parse_movements,
    parse_product,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" R$ 0,07 ", Decimal("0.07")),
        (" R$ 1.232,00 ", Decimal("1232.00")),
        (" R$ 116.720,00 ", Decimal("116720.00")),
        ("0,17", Decimal("0.17")),
        ("63,32", Decimal("63.32")),
        (" R$ 0,33991806 ", Decimal("0.33991806")),
        (" -", None),
        ("", None),
        (None, None),
        ("R$ (12,50)", Decimal("-12.50")),
    ],
)
def test_parse_decimal(raw, expected):
    assert parse_decimal(raw) == expected


def test_parse_date_accepts_brazilian_and_iso():
    assert parse_date("27/07/2026") == date(2026, 7, 27)
    assert parse_date("2026-07-27") == date(2026, 7, 27)
    with pytest.raises(CsvFormatError):
        parse_date("not a date")


@pytest.mark.parametrize(
    ("product", "ticker", "kind"),
    [
        ("BBAS3 - BANCO DO BRASIL S/A", "BBAS3", AssetKind.STOCK),
        ("MXRF11 - MAXI RENDA FDO INV IMOB - FII", "MXRF11", AssetKind.FII),
        ("SMAL11 - ISHARES BM&FBOVESPA SMALL CAP FUNDO DE INDICE", "SMAL11", AssetKind.ETF),
        ("SNAG11 - SUNO AGRO - FIAGRO-IMOBILIARIO", "SNAG11", AssetKind.FII),
        ("ITSA2 - ITAUSA S.A.", "ITSA2", AssetKind.SUBSCRIPTION),
        # A unit is a bundle of shares of one company; it trades, pays and is
        # taxed like a share, so it is classified as one rather than as a class
        # of its own.
        ("TAEE11 - TRANSMISSORA ALIANCA DE ENERGIA ELETRICA S.A.", "TAEE11", AssetKind.STOCK),
        ("ALUP11 - ALUPAR INVESTIMENTO S/A", "ALUP11", AssetKind.STOCK),
        ("CDB - CDB7246C5YO - BANCO GUANABARA S/A", "CDB7246C5YO", AssetKind.FIXED_INCOME),
        ("Futuro - WING21", "WING21", AssetKind.FUTURE),
        ("Opção de Compra - PETRM59 - PETR", "PETRM59", AssetKind.OPTION),
        ("Tesouro Renda+ Aposentadoria Extra 2065", "TESOURO-RENDA-APOSENTADORIA-EXTRA-2065", AssetKind.TREASURY),
    ],
)
def test_parse_product(product, ticker, kind):
    parsed = parse_product(product)
    assert parsed.ticker == ticker
    assert parsed.kind is kind


def test_normalize_broker_collapses_spellings():
    names = [
        "XP INVESTIMENTOS CCTVM S/A",
        "XP INVESTIMENTOS CCTVM S/A.",
        "XP INVESTIMENTOS CORRETORA DE CAMBIO TITULOS E VALORES MOBILIARIOS S/A",
        "XP INVESTIMENTOS CORRETORA DE CAMBIO, TITULOS E VALORES MOBI",
    ]
    assert {normalize_broker(n) for n in names} == {"XP Investimentos"}
    assert normalize_broker("NU INVESTIMENTOS S.A. - CTVM") == "Nu Invest"
    assert normalize_broker("CLEAR CORRETORA - GRUPO XP") == "Clear Corretora"
    assert normalize_broker("") == "Desconhecida"


CSV_SAMPLE = """Entrada/Saída,Data,Movimentação,Produto,Instituição,Quantidade,Preço unitário,Valor da Operação
Credito,27/07/2026,Rendimento,KISU11 - KILIMA FII,XP INVESTIMENTOS CCTVM S/A.,5142," R$ 0,07 "," R$ 359,94 "
Credito,17/07/2026,Transferência - Liquidação,BBAS3 - BANCO DO BRASIL S/A,XP INVESTIMENTOS CCTVM S/A.,7," R$ 20,65 "," R$ 144,55 "
Debito,10/07/2026,Venda,Futuro - WING21,CLEAR CORRETORA - GRUPO XP,0," R$ 116.720,00 ", -
"""


def test_parse_csv_reads_every_row():
    result = parse_csv(CSV_SAMPLE)
    assert result.total_lines == 3
    assert len(result.rows) == 3
    assert result.errors == []
    first = result.rows[0]
    assert first.trade_date == date(2026, 7, 27)
    assert first.product.ticker == "KISU11"
    assert first.quantity == Decimal("5142")
    assert first.total == Decimal("359.94")
    assert result.rows[2].total is None


def test_parse_csv_rejects_unknown_layout():
    with pytest.raises(CsvFormatError):
        parse_csv("foo,bar\n1,2\n")


def test_parse_csv_handles_bom_and_latin1():
    payload = CSV_SAMPLE.encode("latin-1", errors="replace")
    assert len(parse_csv(payload).rows) == 3
    assert len(parse_csv(b"\xef\xbb\xbf" + CSV_SAMPLE.encode("utf-8")).rows) == 3


# --- the spreadsheet form of the same export -------------------------------


def _workbook(rows: list[list], header: list | None = None) -> bytes:
    """Build an .xlsx in memory with the B3 columns."""
    openpyxl = pytest.importorskip("openpyxl")
    from io import BytesIO

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(
        header
        or [
            "Entrada/Saída",
            "Data",
            "Movimentação",
            "Produto",
            "Instituição",
            "Quantidade",
            "Preço unitário",
            "Valor da Operação",
        ]
    )
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_a_spreadsheet_is_recognised_by_its_own_bytes():
    """No filename involved: a zip header means a workbook."""
    assert is_xlsx(_workbook([]))
    assert not is_xlsx(CSV_SAMPLE.encode("utf-8"))
    assert not is_xlsx(b"")


def test_xlsx_and_csv_of_the_same_export_parse_identically():
    """The two downloads are the same report, so they must agree row for row.

    B3 types the spreadsheet's cells — the date is a real date and the amounts
    are numbers — while the CSV carries ``27/07/2026`` and `` R$ 359,94 `` as
    text. Both have to arrive as the same movement.
    """
    from datetime import datetime

    payload = _workbook(
        [
            ["Credito", datetime(2026, 7, 27), "Rendimento", "KISU11 - KILIMA FII",
             "XP INVESTIMENTOS CCTVM S/A.", 5142, 0.07, 359.94],
            ["Credito", datetime(2026, 7, 17), "Transferência - Liquidação",
             "BBAS3 - BANCO DO BRASIL S/A", "XP INVESTIMENTOS CCTVM S/A.", 7, 20.65, 144.55],
            ["Debito", datetime(2026, 7, 10), "Venda", "Futuro - WING21",
             "CLEAR CORRETORA - GRUPO XP", 0, 116720.00, "-"],
        ]
    )

    from_sheet = parse_movements(payload)
    from_text = parse_movements(CSV_SAMPLE.encode("utf-8"))

    assert [r.trade_date for r in from_sheet.rows] == [r.trade_date for r in from_text.rows]
    assert [r.quantity for r in from_sheet.rows] == [r.quantity for r in from_text.rows]
    assert [r.unit_price for r in from_sheet.rows] == [r.unit_price for r in from_text.rows]
    assert [r.total for r in from_sheet.rows] == [r.total for r in from_text.rows]
    assert [r.product.ticker for r in from_sheet.rows] == [r.product.ticker for r in from_text.rows]
    assert [r.broker for r in from_sheet.rows] == [r.broker for r in from_text.rows]
    assert from_sheet.errors == []


def test_a_numeric_cell_keeps_the_value_the_sheet_shows():
    """Money is Decimal, and a float must not leak its binary expansion.

    ``Decimal(0.07)`` is 0.07000000000000000666..., which would ruin an average
    price; ``Decimal(str(0.07))`` is 0.07. Small quantities are the other trap:
    ``str(1e-05)`` is ``'1e-05'``, and the pt-BR number parser strips the ``e``.
    """
    payload = _workbook(
        [["Credito", datetime(2026, 7, 27), "Rendimento", "KISU11 - KILIMA FII",
          "XP", 0.00001, 0.07, 1234.56]]
    )
    row = parse_movements(payload).rows[0]
    assert row.unit_price == Decimal("0.07")
    assert row.total == Decimal("1234.56")
    assert row.quantity == Decimal("0.00001")


def test_a_spreadsheet_with_a_title_row_still_finds_its_header():
    payload = _workbook(
        [
            ["Movimentação - Relatório", None, None, None, None, None, None, None],
            ["Entrada/Saída", "Data", "Movimentação", "Produto", "Instituição",
             "Quantidade", "Preço unitário", "Valor da Operação"],
            ["Credito", datetime(2026, 7, 27), "Rendimento", "KISU11 - KILIMA FII",
             "XP", 5142, 0.07, 359.94],
        ],
        header=["Relatório de movimentações", None, None, None, None, None, None, None],
    )
    result = parse_movements(payload)
    assert len(result.rows) == 1
    assert result.rows[0].product.ticker == "KISU11"


def test_a_spreadsheet_that_is_not_a_b3_export_is_rejected():
    payload = _workbook([[1, 2]], header=["foo", "bar"])
    with pytest.raises(CsvFormatError):
        parse_movements(payload)


def _lie_about_the_extent(payload: bytes) -> bytes:
    """Rewrite the sheet's declared extent to a single cell, as B3 does."""
    import re as _re
    import zipfile
    from io import BytesIO

    source = zipfile.ZipFile(BytesIO(payload))
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename.startswith("xl/worksheets/sheet"):
                data = _re.sub(rb"<dimension[^>]*/>", b'<dimension ref="A1"/>', data)
            target.writestr(item, data)
    return buffer.getvalue()


def test_a_sheet_that_understates_its_own_size_is_still_read_in_full():
    """B3's real download says it is one cell wide and then fills 53 rows.

    Its ``<dimension ref="A1"/>`` is simply wrong, and openpyxl's read-only
    mode trusts it: the import stopped at the first cell and reported a file
    whose only content was the words "Entrada/Saída". The extent is recomputed
    from the rows that are actually there.
    """
    honest = _workbook(
        [
            ["Credito", datetime(2026, 7, 31), "Juros Sobre Capital Próprio",
             "BBDC3 - BANCO BRADESCO S/A", "XP INVESTIMENTOS CCTVM S/A.", 1566, 0.351, 467.47],
            ["Credito", datetime(2026, 7, 27), "Rendimento", "KISU11 - KILIMA FII",
             "XP INVESTIMENTOS CCTVM S/A.", 5142, 0.07, 359.94],
        ]
    )
    lying = _lie_about_the_extent(honest)

    result = parse_movements(lying)
    assert len(result.rows) == 2, "the sheet was truncated at its declared extent"
    assert result.errors == []
    assert [r.product.ticker for r in result.rows] == ["BBDC3", "KISU11"]
    assert result.rows[0].total == Decimal("467.47")
