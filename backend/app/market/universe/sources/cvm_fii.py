"""FII fundamentals from the monthly informe every fund files with the CVM.

Real-estate funds do not file company statements, so nothing in
``cvm_statements`` reaches them — and FIIs are a large part of what a Brazilian
portfolio screens for. Their equivalent is the *informe mensal*: patrimônio
líquido, cotas emitidas, valor patrimonial da cota and the month's distribution
yield, filed every month by every fund. It is an 870 KB zip for the whole
market.

The join is exact and needs no name matching: the informe carries
``Codigo_ISIN`` and COTAHIST carries the same ISIN for the traded quota, so a
fund's filing and its ticker line up by identifier.

Two figures come straight from the filing rather than being derived, and are
better for it: ``Valor_Patrimonial_Cotas`` is the fund's own published book
value per quota (so P/VP is exact, not reconstructed), and
``Percentual_Dividend_Yield_Mes`` is the month's distribution, which summed over
the trailing twelve months gives a yield built from twelve filed figures.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.core.logging import get_logger

from . import SourceShapeError, digits, fetch_bytes, newest_year_file, read_zip_csv, to_decimal

logger = get_logger(__name__)

FII_DIR = "https://dados.cvm.gov.br/dados/FII/DOC/INF_MENSAL/DADOS/"
FII_PATTERN = r"inf_mensal_fii_(\d{4})\.zip"

SOURCE = "cvm-fii"

GERAL_COLUMNS = {
    "CNPJ_Fundo_Classe",
    "Data_Referencia",
    "Versao",
    "Nome_Fundo_Classe",
    "Codigo_ISIN",
    "Segmento_Atuacao",
    "Tipo_Gestao",
}
COMPLEMENTO_COLUMNS = {
    "CNPJ_Fundo_Classe",
    "Data_Referencia",
    "Versao",
    "Patrimonio_Liquido",
    "Cotas_Emitidas",
    "Valor_Patrimonial_Cotas",
    "Percentual_Dividend_Yield_Mes",
}

#: Distributions are filed as fractions (0.004342 == 0,4342 % in the month).
_AS_FRACTION = Decimal(100)

#: A yield built from fewer months than this is not an annual yield.
MIN_MONTHS_FOR_YIELD = 6
MONTHS = 12


@dataclass(slots=True)
class FundInfo:
    """One fund's latest informe, plus its trailing distribution record."""

    cnpj: str
    isin: str | None = None
    name: str | None = None
    segment: str | None = None
    management: str | None = None
    net_assets: Decimal | None = None
    quotas: Decimal | None = None
    book_value_per_quota: Decimal | None = None
    #: (Data_Referencia, monthly yield as a percentage), newest last.
    monthly_yields: list[tuple[str, Decimal]] = field(default_factory=list)
    period: str | None = None

    @property
    def dividend_yield_pct(self) -> Decimal | None:
        """Trailing-twelve-month distribution, summed from monthly filings.

        Summed rather than compounded: this is a distribution rate on the
        quota, not a reinvested return, and compounding it would overstate what
        a holder actually received.
        """
        if len(self.monthly_yields) < MIN_MONTHS_FOR_YIELD:
            return None
        recent = self.monthly_yields[-MONTHS:]
        total = sum((value for _, value in recent), Decimal(0))
        if total <= 0:
            return None
        # Fewer than twelve months on file: annualise rather than understate.
        if len(recent) < MONTHS:
            total = total * MONTHS / Decimal(len(recent))
        return total


def _key(row: dict[str, str]) -> tuple[str, int]:
    try:
        version = int((row.get("Versao") or "0").strip())
    except ValueError:
        version = 0
    return (row.get("Data_Referencia") or "").strip(), version


def fetch() -> tuple[dict[str, FundInfo], list[str]]:
    """Every FII's latest informe, keyed by digits-only CNPJ.

    Reads the newest year-file and, when the year is young, the one before it,
    so the trailing yield always has twelve months to work with rather than
    resetting to one every January.
    """
    newest = newest_year_file(FII_DIR, FII_PATTERN)
    if newest is None:
        raise SourceShapeError("informes de FII: nenhum arquivo no diretório publicado")
    _, year = newest

    funds: dict[str, FundInfo] = {}
    warnings: list[str] = []
    #: Newest filing seen per fund, so a retransmission cannot be mixed in.
    latest: dict[str, tuple[str, int]] = {}

    for target in (year - 1, year):
        try:
            raw = fetch_bytes(FII_DIR + f"inf_mensal_fii_{target}.zip")
        except Exception as exc:  # noqa: BLE001 — last year may not exist
            logger.info("cvm fii %s unavailable: %s", target, exc)
            continue
        try:
            _fold(raw, target, funds, latest)
        except SourceShapeError as exc:
            warnings.append(f"informes de FII {target}: {exc}")
            logger.info("cvm fii %s unusable: %s", target, exc)

    if not funds:
        raise SourceShapeError("informes de FII: nenhum fundo lido dos arquivos")
    return funds, warnings


def _fold(
    raw: bytes,
    year: int,
    funds: dict[str, FundInfo],
    latest: dict[str, tuple[str, int]],
) -> None:
    """Fold one year-file into the accumulators, oldest month first."""
    geral = f"inf_mensal_fii_geral_{year}.csv"
    complemento = f"inf_mensal_fii_complemento_{year}.csv"

    for row in read_zip_csv(raw, geral, GERAL_COLUMNS):
        cnpj = digits(row.get("CNPJ_Fundo_Classe"))
        if not cnpj:
            continue
        stamp = _key(row)
        record = funds.get(cnpj)
        if record is None:
            record = FundInfo(cnpj=cnpj)
            funds[cnpj] = record
        # Registry fields come from the newest informe only.
        if latest.get(cnpj) is None or stamp >= latest[cnpj]:
            latest[cnpj] = stamp
            record.isin = (row.get("Codigo_ISIN") or "").strip() or record.isin
            record.name = (row.get("Nome_Fundo_Classe") or "").strip() or record.name
            record.segment = (row.get("Segmento_Atuacao") or "").strip() or record.segment
            record.management = (row.get("Tipo_Gestao") or "").strip() or record.management
            record.period = stamp[0][:7] or record.period

    newest_seen: dict[str, tuple[str, int]] = {}
    for row in read_zip_csv(raw, complemento, COMPLEMENTO_COLUMNS):
        cnpj = digits(row.get("CNPJ_Fundo_Classe"))
        record = funds.get(cnpj) if cnpj else None
        if record is None:
            continue
        stamp = _key(row)

        monthly = to_decimal(row.get("Percentual_Dividend_Yield_Mes"))
        if monthly is not None:
            record.monthly_yields.append((stamp[0], monthly * _AS_FRACTION))

        # Balance figures describe a moment, so only the newest month's count.
        if newest_seen.get(cnpj) is not None and stamp < newest_seen[cnpj]:
            continue
        newest_seen[cnpj] = stamp
        record.net_assets = to_decimal(row.get("Patrimonio_Liquido")) or record.net_assets
        record.quotas = to_decimal(row.get("Cotas_Emitidas")) or record.quotas
        record.book_value_per_quota = (
            to_decimal(row.get("Valor_Patrimonial_Cotas")) or record.book_value_per_quota
        )

    # Monthly yields arrive in file order; sort so "the last twelve" is true.
    for record in funds.values():
        record.monthly_yields.sort(key=lambda item: item[0])
        # A fund can file the same month twice across versions; keep one each.
        deduped: dict[str, Decimal] = {}
        for month, value in record.monthly_yields:
            deduped[month[:7]] = value
        record.monthly_yields = sorted(deduped.items())


def by_isin(funds: dict[str, FundInfo]) -> dict[str, FundInfo]:
    """Re-key on ISIN — the identifier COTAHIST shares with these filings."""
    out: dict[str, FundInfo] = {}
    for record in funds.values():
        if record.isin:
            out[record.isin.strip().upper()] = record
    return out
