"""Company fundamentals from the filings themselves — CVM DFP and ITR.

Every listed Brazilian company files its statements with the CVM, and the CVM
republishes them as open data: one zip per year holding the balance sheet, the
income statement and the cash-flow statement for every filer, as line items with
their account codes. That is where P/L, ROE and margins actually come from, and
computing them here rather than borrowing a provider's numbers means each one is
traceable to an account code in a filing anyone can download.

**The annual statement alone is too old to screen on.** A DFP filed for the
financial year is the newest annual figure available, which by the middle of the
next year is eight months behind. The quarterly ITR closes that gap, and its
layout makes a trailing-twelve-month figure derivable rather than guessed:
alongside the current year-to-date it files *the same span of the previous
year*, so

    UDM = exercício anterior − acumulado do ano anterior + acumulado do ano

is arithmetic over three published numbers, not an estimate. Vale's Q2 2026
filing, for instance, carries both 2026-01-01→06-30 and 2025-01-01→06-30, which
with FY2025 gives twelve months ending on the most recent quarter.

Balance-sheet items need no such assembly: equity, debt and share count are
taken from the newest ITR directly, which is strictly fresher than the year-end.

Two guards, because a subtly wrong P/L is worse than an openly old one. The
trailing figure is only built when all three components belong to the periods
they should, and the result is sanity-checked against the annual one; anything
that fails falls back to the annual figure. :attr:`Fundamentals.period` always
says which it is ("2026T2 (UDM)" or "2025"), so the age of every number on the
screener is visible rather than implied.

Amounts are published in thousands (``ESCALA_MOEDA``), which is scaled here — a
missed factor of a thousand is the kind of error that makes a company look a
thousand times cheaper than it is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.core.logging import get_logger

from . import (
    SourceShapeError,
    digits,
    fetch_bytes,
    newest_year_file,
    normalize,
    read_zip_csv,
    to_decimal,
)

logger = get_logger(__name__)

DFP_DIR = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/"
DFP_PATTERN = r"dfp_cia_aberta_(\d{4})\.zip"

SOURCE = "cvm-dfp"

#: Columns every statement member carries. Missing any of them means the
#: published layout changed and the stage must be skipped, not guessed at.
STATEMENT_COLUMNS = {
    "CNPJ_CIA",
    "DT_REFER",
    "VERSAO",
    "ORDEM_EXERC",
    "CD_CONTA",
    "DS_CONTA",
    "VL_CONTA",
    "ESCALA_MOEDA",
    "MOEDA",
}
CAPITAL_COLUMNS = {
    "CNPJ_CIA",
    "DT_REFER",
    "VERSAO",
    "QT_ACAO_TOTAL_CAP_INTEGR",
    "QT_ACAO_TOTAL_TESOURO",
}

#: Where each figure lives in the filing.
#:
#: Account *codes* are not enough, and assuming they were is a real bug this
#: caught: a non-financial company files equity at ``2.03``, but a bank files
#: ``2.03`` as interbank funding and puts equity at ``2.08``. Reading by code
#: alone gave Itaú a P/VP of 0,20 — a liability divided into a price.
#:
#: So the *description* is authoritative where the chart varies between
#: industries, and the code prefix is only a guard. Where the slot is stable
#: across industries (revenue and gross profit at 3.01/3.03, which banks fill
#: with "Receitas de Intermediação Financeira") the code is used directly.
#:
#: ``ST_CONTA_FIXA = S`` marks the standard chart lines, which is what makes
#: matching on a description safe rather than a guess about free text.
_BY_CODE = {
    "3.01": "revenue",
    "3.03": "gross_profit",
}

#: field -> (code prefix, fully-anchored pattern over the normalised
#: description). The match with the shortest account code wins, so a total is
#: never confused with a line inside its own breakdown.
#:
#: Patterns rather than a list of literals because the same line is spelled
#: differently by industry: a retailer files "Lucro/Prejuízo Consolidado do
#: Período" and a bank "Lucro ou Prejuízo Líquido Consolidado do Período".
#: Both are the same figure. The patterns are anchored end to end and the
#: optional groups are enumerated, so they cannot drift onto a neighbouring
#: line — "Lucro Básico por Ação" and "Lucro antes das Participações" both
#: fail to match, which is the point.
_BY_DESCRIPTION: dict[str, tuple[str, re.Pattern[str]]] = {
    "equity": ("2.", re.compile(r"^patrimonio liquido( consolidado)?$")),
    "net_income": (
        "3.",
        re.compile(r"^lucro( ou)? prejuizo( liquido)?( consolidado)? do (periodo|exercicio)$"),
    ),
}

#: Borrowings, for debt/equity. Left by code on purpose: a bank's deposits are
#: not "debt" in the sense this ratio means, so financials simply get no
#: gearing figure rather than a number that cannot be compared to a retailer's.
_DEBT_ACCOUNTS = {
    "2.01.04": "debt_current",
    "2.02.01": "debt_noncurrent",
}

#: Dividends are a financing-activity line whose sub-code varies by filer, so
#: this one is matched on the description within the 6.03 block.
_DIVIDEND_PARENT = "6.03"
_DIVIDEND_WORDS = re.compile(r"dividendo|juros sobre (o )?capital", re.IGNORECASE)


@dataclass(slots=True)
class Fundamentals:
    """One company's figures for one financial year, already scaled to reais."""

    cnpj: str
    #: What the figures describe: "2025" for a financial year, "2026T2 (UDM)"
    #: for twelve months ending on a quarter. Shown on the screener, so the
    #: age of every number is visible rather than implied.
    period: str
    #: "anual" | "udm" — which of the two the figures above actually are.
    basis: str = "anual"
    revenue: Decimal | None = None
    gross_profit: Decimal | None = None
    net_income: Decimal | None = None
    equity: Decimal | None = None
    #: The two borrowing lines are read separately and folded into ``debt``
    #: once both are known; a company reporting only one still gets a total.
    debt_current: Decimal | None = None
    debt_noncurrent: Decimal | None = None
    debt: Decimal | None = None
    dividends_paid: Decimal | None = None
    #: **As filed** — CVM publishes no scale for this and filers disagree, some
    #: reporting units and some thousands. It is resolved against the market
    #: price in ``universe.compute.resolve_share_scale``, which is the only
    #: place with enough evidence to tell them apart.
    shares_outstanding: Decimal | None = None
    # Prior-year comparatives, from the same filing (ORDEM_EXERC=PENÚLTIMO).
    prior_revenue: Decimal | None = None
    prior_net_income: Decimal | None = None
    #: Growth computed at the source, set only on the trailing basis. Once the
    #: headline figures are twelve months ending mid-year, the annual
    #: comparative no longer lines up with them; the quarterly filing's own
    #: year-to-date pair does, so growth is taken from that instead of from two
    #: spans that merely happen to be a year long.
    revenue_growth_pct: Decimal | None = None
    earnings_growth_pct: Decimal | None = None


#: Every numeric field carried between statements when they are merged.
MERGEABLE: tuple[str, ...] = (
    "revenue",
    "gross_profit",
    "net_income",
    "equity",
    "debt_current",
    "debt_noncurrent",
    "dividends_paid",
    "shares_outstanding",
    "prior_revenue",
    "prior_net_income",
)

_SCALE = {"MIL": Decimal(1000), "MILHAR": Decimal(1000), "UNIDADE": Decimal(1)}


def _scaled(row: dict[str, str]) -> Decimal | None:
    """A line item's value in reais, or None when it is unusable."""
    if (row.get("MOEDA") or "").strip().upper() not in ("REAL", ""):
        return None  # a foreign-currency filing cannot be compared to a BRL price
    value = to_decimal(row.get("VL_CONTA"))
    if value is None:
        return None
    return value * _SCALE.get((row.get("ESCALA_MOEDA") or "").strip().upper(), Decimal(1))


def _is_last(row: dict[str, str]) -> bool:
    """ÚLTIMO = the year being reported; PENÚLTIMO = the comparative."""
    return (row.get("ORDEM_EXERC") or "").strip().upper().startswith("ÚLTIMO") or (
        (row.get("ORDEM_EXERC") or "").strip().upper() == "ULTIMO"
    )


def _year(row: dict[str, str]) -> str:
    return (row.get("DT_REFER") or "")[:4]


def _version(row: dict[str, str]) -> int:
    try:
        return int((row.get("VERSAO") or "0").strip())
    except ValueError:
        return 0


class _Latest:
    """Keeps only the newest filing per company, resolving retransmissions.

    A company can refile the same year; CVM keeps every version, so a naive
    read mixes restated and superseded figures in the same row. Newest
    ``DT_REFER`` wins, and within it the highest ``VERSAO``.
    """

    def __init__(self) -> None:
        self._best: dict[str, tuple[str, int]] = {}

    def accept(self, cnpj: str, row: dict[str, str]) -> bool:
        stamp = (row.get("DT_REFER") or "").strip()
        version = _version(row)
        current = self._best.get(cnpj)
        if current is None or (stamp, version) > current:
            self._best[cnpj] = (stamp, version)
            return True
        return (stamp, version) == current

    def key(self, cnpj: str) -> tuple[str, int] | None:
        return self._best.get(cnpj)


def _matched_field(code: str, description: str, accounts: dict[str, str]) -> str | None:
    """Which figure this line item is, by code where stable, else by name."""
    field = accounts.get(code)
    if field is not None:
        return field
    normalized = normalize(description)
    for candidate, (prefix, pattern) in _BY_DESCRIPTION.items():
        if code.startswith(prefix) and pattern.fullmatch(normalized):
            return candidate
    return None


def _collect(
    raw: bytes,
    member: str,
    accounts: dict[str, str],
    out: dict[str, Fundamentals],
    columns: set[str] = STATEMENT_COLUMNS,
) -> None:
    """Fold one statement member into the per-company accumulators.

    Two passes over the member: the first settles which filing version is the
    live one per company, the second reads only that one. Streaming twice is
    cheaper than holding a 20 MB member's rows in memory on a laptop.

    Within a company, a description match with a shorter account code wins, so
    "Patrimônio Líquido Consolidado" at 2.08 is preferred over any deeper line
    that happens to share the name.
    """
    latest = _Latest()
    for row in read_zip_csv(raw, member, columns):
        cnpj = digits(row.get("CNPJ_CIA"))
        if cnpj:
            latest.accept(cnpj, row)

    #: (cnpj, field, is_last) -> the code that supplied the current value.
    chosen: dict[tuple[str, str, bool], str] = {}
    for row in read_zip_csv(raw, member, columns):
        cnpj = digits(row.get("CNPJ_CIA"))
        if not cnpj or latest.key(cnpj) != ((row.get("DT_REFER") or "").strip(), _version(row)):
            continue
        code = (row.get("CD_CONTA") or "").strip()
        field = _matched_field(code, row.get("DS_CONTA") or "", accounts)
        if field is None:
            continue
        value = _scaled(row)
        if value is None:
            continue
        record = out.get(cnpj)
        if record is None:
            record = Fundamentals(cnpj=cnpj, period=_year(row))
            out[cnpj] = record

        last = _is_last(row)
        target = field if last else f"prior_{field}"
        if not last and field not in ("revenue", "net_income"):
            continue
        key = (cnpj, field, last)
        incumbent = chosen.get(key)
        if incumbent is not None and len(incumbent) <= len(code):
            continue
        chosen[key] = code
        setattr(record, target, value)


def _collect_dividends(raw: bytes, member: str, out: dict[str, Fundamentals]) -> None:
    """Cash actually paid to shareholders, from the financing section.

    This is a better dividend figure than a declared-schedule scrape: it is what
    left the company's bank account over the year, already audited. It is
    published negative (an outflow); the magnitude is what a yield needs.
    """
    latest = _Latest()
    for row in read_zip_csv(raw, member, STATEMENT_COLUMNS):
        cnpj = digits(row.get("CNPJ_CIA"))
        if cnpj:
            latest.accept(cnpj, row)

    totals: dict[str, Decimal] = {}
    for row in read_zip_csv(raw, member, STATEMENT_COLUMNS):
        cnpj = digits(row.get("CNPJ_CIA"))
        if not cnpj or not _is_last(row):
            continue
        if latest.key(cnpj) != ((row.get("DT_REFER") or "").strip(), _version(row)):
            continue
        code = (row.get("CD_CONTA") or "").strip()
        # Only leaves under the financing block, never the block's own total.
        if not code.startswith(_DIVIDEND_PARENT + ".") or code.count(".") > 3:
            continue
        if not _DIVIDEND_WORDS.search(row.get("DS_CONTA") or ""):
            continue
        value = _scaled(row)
        if value is None:
            continue
        totals[cnpj] = totals.get(cnpj, Decimal(0)) + abs(value)

    for cnpj, total in totals.items():
        record = out.get(cnpj)
        if record is not None and total > 0:
            record.dividends_paid = total


def _collect_shares(raw: bytes, member: str, out: dict[str, Fundamentals]) -> None:
    """Shares actually outstanding: issued capital less treasury stock.

    Treasury shares are owned by the company itself and earn nothing for an
    investor, so counting them would inflate every market cap and deflate every
    per-share figure.
    """
    latest = _Latest()
    for row in read_zip_csv(raw, member, CAPITAL_COLUMNS):
        cnpj = digits(row.get("CNPJ_CIA"))
        if cnpj:
            latest.accept(cnpj, row)

    for row in read_zip_csv(raw, member, CAPITAL_COLUMNS):
        cnpj = digits(row.get("CNPJ_CIA"))
        if not cnpj or latest.key(cnpj) != ((row.get("DT_REFER") or "").strip(), _version(row)):
            continue
        issued = to_decimal(row.get("QT_ACAO_TOTAL_CAP_INTEGR"))
        treasury = to_decimal(row.get("QT_ACAO_TOTAL_TESOURO")) or Decimal(0)
        if issued is None or issued <= 0:
            continue
        record = out.get(cnpj)
        if record is None:
            record = Fundamentals(cnpj=cnpj, period=_year(row))
            out[cnpj] = record
        outstanding = issued - treasury
        if outstanding > 0:
            record.shares_outstanding = outstanding


def _fold_year(year: int, records: dict[str, Fundamentals], warnings: list[str]) -> None:
    """Read one DFP year-file into ``records``, overwriting older entries.

    Each statement is read consolidated first, then individual for the
    companies that filed no consolidated one. Verified on the 2025 file: 437
    companies file ``_con`` and 228 file only ``_ind`` — a company with no
    subsidiaries has nothing to consolidate, and its individual statement *is*
    its complete picture. Reading only ``_con``, the obvious choice, would drop
    a third of the market.
    """
    raw = fetch_bytes(DFP_DIR + f"dfp_cia_aberta_{year}.zip")

    def member(name: str) -> str:
        return f"dfp_cia_aberta_{name}_{year}.csv"

    def both(name: str, accounts: dict[str, str], required: bool) -> None:
        """Consolidated wins; individual fills the companies it left out."""
        consolidated: dict[str, Fundamentals] = {}
        try:
            _collect(raw, member(f"{name}_con"), accounts, consolidated)
        except SourceShapeError as exc:
            if required:
                raise
            warnings.append(f"CVM DFP {year} ({name}_con): {exc}")
        individual: dict[str, Fundamentals] = {}
        try:
            _collect(raw, member(f"{name}_ind"), accounts, individual)
        except SourceShapeError as exc:
            logger.info("cvm dfp %s %s_ind unusable: %s", year, name, exc)
        for cnpj, record in {**individual, **consolidated}.items():
            existing = records.get(cnpj)
            if existing is None:
                records[cnpj] = record
                continue
            # Merge every figure, not only the ones this call matched by code:
            # equity and net income are matched by description and would
            # otherwise be silently dropped on the second statement.
            for field in MERGEABLE:
                value = getattr(record, field, None)
                if value is not None:
                    setattr(existing, field, value)
            existing.period = record.period

    # The income statement is the one that must work: without earnings there is
    # nothing worth screening.
    both("DRE", _BY_CODE, required=True)
    both("BPP", _DEBT_ACCOUNTS, required=False)

    # The rest is enrichment — losing one costs the metrics it feeds, not the
    # stage. A missing cash-flow member should cost the dividend yield, not P/L.
    optional = (
        ("DFC_MI_con", lambda: _collect_dividends(raw, member("DFC_MI_con"), records)),
        ("composicao_capital", lambda: _collect_shares(raw, member("composicao_capital"), records)),
    )
    for name, run in optional:
        try:
            run()
        except SourceShapeError as exc:
            warnings.append(f"CVM DFP {year} ({name}): {exc}")
            logger.info("cvm dfp %s member %s unusable: %s", year, name, exc)


ITR_DIR = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/ITR/DADOS/"
ITR_PATTERN = r"itr_cia_aberta_(\d{4})\.zip"

SOURCE_TTM = "cvm-itr"

#: Income figures that accumulate over a period, and can therefore be rolled
#: into a trailing twelve months. Balance-sheet items are a snapshot and are
#: simply taken from the newest filing instead.
_FLOW_FIELDS = ("revenue", "gross_profit", "net_income", "dividends_paid")

#: A trailing figure this far from the annual one means the periods did not
#: line up the way the arithmetic assumed. Wide enough for a company that
#: genuinely doubled or halved; narrow enough to catch a mismatch.
_TTM_SANITY = (Decimal("0.25"), Decimal(4))


@dataclass(slots=True)
class Quarterly:
    """One company's newest ITR: year-to-date, its prior-year twin, balances."""

    cnpj: str
    #: The quarter this describes, e.g. "2026-06-30".
    refer: str
    #: Financial year the year-to-date belongs to.
    fiscal_year: int
    ytd: dict[str, Decimal]
    ytd_prior: dict[str, Decimal]
    equity: Decimal | None = None
    debt_current: Decimal | None = None
    debt_noncurrent: Decimal | None = None
    shares_outstanding: Decimal | None = None

    @property
    def label(self) -> str:
        """"2026T2" — the quarter, as a Brazilian reader writes it."""
        month = int(self.refer[5:7])
        return f"{self.refer[:4]}T{(month - 1) // 3 + 1}"


def _period_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (
        (row.get("DT_INI_EXERC") or "").strip(),
        (row.get("DT_FIM_EXERC") or "").strip(),
        (row.get("ORDEM_EXERC") or "").strip().upper(),
    )


def _collect_quarterly_flows(
    raw: bytes, member: str, out: dict[str, Quarterly], accounts: dict[str, str]
) -> None:
    """Read the year-to-date column, and the same span of the year before.

    A Q2 filing carries four variants of every income line: this year and last,
    each as the quarter alone and as the year to date. Only the year-to-date
    pair is wanted, and it is identified structurally — the row whose period
    *ends* with the filing and *begins* earliest. Picking the quarter by
    mistake would under-report a half-year by half.
    """
    latest = _Latest()
    for row in read_zip_csv(raw, member, STATEMENT_COLUMNS):
        cnpj = digits(row.get("CNPJ_CIA"))
        if cnpj:
            latest.accept(cnpj, row)

    #: (cnpj, field, is_last) -> (period start chosen, account code chosen)
    chosen: dict[tuple[str, str, bool], tuple[str, str]] = {}
    for row in read_zip_csv(raw, member, STATEMENT_COLUMNS):
        cnpj = digits(row.get("CNPJ_CIA"))
        if not cnpj:
            continue
        stamp = (row.get("DT_REFER") or "").strip()
        if latest.key(cnpj) != (stamp, _version(row)):
            continue
        code = (row.get("CD_CONTA") or "").strip()
        field = _matched_field(code, row.get("DS_CONTA") or "", accounts)
        if field is None:
            continue
        value = _scaled(row)
        if value is None:
            continue
        start, end, order = _period_key(row)
        if not start or not end:
            continue
        last = order.startswith("ÚLTIMO") or order == "ULTIMO"
        # The year-to-date row ends with the reported quarter (this year) or on
        # its prior-year twin; either way it is the one that starts earliest.
        record = out.get(cnpj)
        if record is None:
            try:
                fiscal_year = int(stamp[:4])
            except ValueError:
                continue
            record = Quarterly(cnpj=cnpj, refer=stamp, fiscal_year=fiscal_year, ytd={}, ytd_prior={})
            out[cnpj] = record

        # Earliest start wins — that is the accumulated column rather than the
        # quarter alone. Ties go to the shorter account code, so a total is
        # never displaced by a line inside its own breakdown.
        key = (cnpj, field, last)
        incumbent = chosen.get(key)
        if incumbent is not None and (incumbent[0], len(incumbent[1])) <= (start, len(code)):
            continue
        chosen[key] = (start, code)
        (record.ytd if last else record.ytd_prior)[field] = value


def _collect_quarterly_balance(raw: bytes, member: str, out: dict[str, Quarterly]) -> None:
    """Equity and borrowings from the newest quarter — a snapshot, not a flow."""
    latest = _Latest()
    for row in read_zip_csv(raw, member, STATEMENT_COLUMNS):
        cnpj = digits(row.get("CNPJ_CIA"))
        if cnpj:
            latest.accept(cnpj, row)

    chosen: dict[tuple[str, str], str] = {}
    for row in read_zip_csv(raw, member, STATEMENT_COLUMNS):
        cnpj = digits(row.get("CNPJ_CIA"))
        record = out.get(cnpj) if cnpj else None
        if record is None or latest.key(cnpj) != ((row.get("DT_REFER") or "").strip(), _version(row)):
            continue
        order = (row.get("ORDEM_EXERC") or "").strip().upper()
        if not (order.startswith("ÚLTIMO") or order == "ULTIMO"):
            continue  # the comparative balance is last year's, not this one's
        code = (row.get("CD_CONTA") or "").strip()
        field = _matched_field(code, row.get("DS_CONTA") or "", _DEBT_ACCOUNTS)
        if field is None:
            continue
        value = _scaled(row)
        if value is None:
            continue
        key = (cnpj, field)
        if key in chosen and len(chosen[key]) <= len(code):
            continue
        chosen[key] = code
        setattr(record, field, value)


def _collect_quarterly_shares(raw: bytes, member: str, out: dict[str, Quarterly]) -> None:
    latest = _Latest()
    for row in read_zip_csv(raw, member, CAPITAL_COLUMNS):
        cnpj = digits(row.get("CNPJ_CIA"))
        if cnpj:
            latest.accept(cnpj, row)

    for row in read_zip_csv(raw, member, CAPITAL_COLUMNS):
        cnpj = digits(row.get("CNPJ_CIA"))
        record = out.get(cnpj) if cnpj else None
        if record is None or latest.key(cnpj) != ((row.get("DT_REFER") or "").strip(), _version(row)):
            continue
        issued = to_decimal(row.get("QT_ACAO_TOTAL_CAP_INTEGR"))
        treasury = to_decimal(row.get("QT_ACAO_TOTAL_TESOURO")) or Decimal(0)
        if issued is not None and issued - treasury > 0:
            record.shares_outstanding = issued - treasury


def fetch_quarterly() -> tuple[dict[str, Quarterly], list[str]]:
    """Every filer's newest quarterly statement, keyed by digits-only CNPJ.

    Each company's own latest filing is used, not a single market-wide quarter:
    verified against the live file, ~29 000 rows carry Q1 while only ~3 700
    carry Q2, because the filing deadline is 45 days after the quarter ends.
    Waiting for the whole market would throw away a quarter of freshness for
    everyone who filed on time.
    """
    newest = newest_year_file(ITR_DIR, ITR_PATTERN)
    if newest is None:
        raise SourceShapeError("CVM ITR: nenhum arquivo trimestral no diretório publicado")
    _, year = newest
    raw = fetch_bytes(ITR_DIR + f"itr_cia_aberta_{year}.zip")

    warnings: list[str] = []

    def member(name: str) -> str:
        return f"itr_cia_aberta_{name}_{year}.csv"

    def read(suffix: str) -> dict[str, Quarterly]:
        """One company set, read entirely from consolidated or entirely from
        individual statements."""
        out: dict[str, Quarterly] = {}
        _collect_quarterly_flows(raw, member(f"DRE_{suffix}"), out, _BY_CODE)
        try:
            _collect_quarterly_balance(raw, member(f"BPP_{suffix}"), out)
        except SourceShapeError as exc:
            warnings.append(f"CVM ITR (BPP_{suffix}): {exc}")
        return out

    # A company's consolidated record replaces its individual one *whole*.
    # Merging them field by field would be worse than either: consolidated
    # revenue over individual net income is a margin belonging to no company,
    # and a holding's individual statement excludes the subsidiaries that earn
    # the money. Reading only ``_con`` is not an option either — a third of
    # filers have nothing to consolidate and publish ``_ind`` alone.
    individual: dict[str, Quarterly] = {}
    try:
        individual = read("ind")
    except SourceShapeError as exc:
        logger.info("cvm itr individual statements unusable: %s", exc)
    records: dict[str, Quarterly] = {**individual, **read("con")}

    for name, run in (
        ("DFC_MI_con", lambda: _collect_quarterly_dividends(raw, member("DFC_MI_con"), records)),
        ("composicao_capital", lambda: _collect_quarterly_shares(raw, member("composicao_capital"), records)),
    ):
        try:
            run()
        except SourceShapeError as exc:
            warnings.append(f"CVM ITR ({name}): {exc}")
            logger.info("cvm itr member %s unusable: %s", name, exc)

    if not records:
        raise SourceShapeError("CVM ITR: nenhuma companhia lida do arquivo trimestral")
    return records, warnings


def _collect_quarterly_dividends(raw: bytes, member: str, out: dict[str, Quarterly]) -> None:
    """Dividends paid year-to-date, and the same span a year earlier."""
    latest = _Latest()
    for row in read_zip_csv(raw, member, STATEMENT_COLUMNS):
        cnpj = digits(row.get("CNPJ_CIA"))
        if cnpj:
            latest.accept(cnpj, row)

    totals: dict[tuple[str, bool, str], Decimal] = {}
    for row in read_zip_csv(raw, member, STATEMENT_COLUMNS):
        cnpj = digits(row.get("CNPJ_CIA"))
        if not cnpj or cnpj not in out:
            continue
        if latest.key(cnpj) != ((row.get("DT_REFER") or "").strip(), _version(row)):
            continue
        code = (row.get("CD_CONTA") or "").strip()
        if not code.startswith(_DIVIDEND_PARENT + ".") or code.count(".") > 3:
            continue
        if not _DIVIDEND_WORDS.search(row.get("DS_CONTA") or ""):
            continue
        value = _scaled(row)
        if value is None:
            continue
        start, _end, order = _period_key(row)
        last = order.startswith("ÚLTIMO") or order == "ULTIMO"
        key = (cnpj, last, start)
        totals[key] = totals.get(key, Decimal(0)) + abs(value)

    # Keep the accumulated column: the earliest start for each side.
    best: dict[tuple[str, bool], tuple[str, Decimal]] = {}
    for (cnpj, last, start), amount in totals.items():
        key = (cnpj, last)
        if key not in best or start < best[key][0]:
            best[key] = (start, amount)
    for (cnpj, last), (_start, amount) in best.items():
        record = out.get(cnpj)
        if record is not None and amount > 0:
            (record.ytd if last else record.ytd_prior)["dividends_paid"] = amount


def _apply_quarterly(annual: Fundamentals, quarter: Quarterly) -> bool:
    """Roll a company's annual figures forward to twelve months ending now.

    Returns True when the trailing figures were adopted. Every flow needs all
    three components — the annual total, this year's accumulation and last
    year's over the same span — and the annual statement must be the financial
    year immediately before the quarter, or the arithmetic is comparing the
    wrong twelve months.
    """
    if annual.period != str(quarter.fiscal_year - 1):
        return False

    rolled: dict[str, Decimal] = {}
    for field in _FLOW_FIELDS:
        base = getattr(annual, field, None)
        current = quarter.ytd.get(field)
        prior = quarter.ytd_prior.get(field)
        if base is None or current is None or prior is None:
            continue
        rolled[field] = base + current - prior

    # Revenue is the anchor: if the rolled revenue is implausible against the
    # annual one, the periods did not line up and nothing here is trustworthy.
    low, high = _TTM_SANITY
    revenue = rolled.get("revenue")
    if revenue is not None and annual.revenue and annual.revenue > 0:
        ratio = revenue / annual.revenue
        if not (low <= ratio <= high):
            return False
    if not rolled:
        return False

    for field, value in rolled.items():
        setattr(annual, field, value)

    # Year on year, like for like: the filing publishes this year's
    # accumulation and last year's over the identical span, which is a cleaner
    # comparison than anything derivable from the annual totals.
    for field, target in (("revenue", "revenue_growth_pct"), ("net_income", "earnings_growth_pct")):
        current = quarter.ytd.get(field)
        prior = quarter.ytd_prior.get(field)
        if current is not None and prior is not None and prior > 0:
            setattr(annual, target, (current - prior) / prior * Decimal(100))

    # Balance-sheet items are a snapshot — the newest one simply wins.
    if quarter.equity is not None:
        annual.equity = quarter.equity
    if quarter.debt_current is not None or quarter.debt_noncurrent is not None:
        parts = [v for v in (quarter.debt_current, quarter.debt_noncurrent) if v is not None]
        annual.debt = sum(parts, Decimal(0)) if parts else annual.debt
    if quarter.shares_outstanding is not None:
        annual.shares_outstanding = quarter.shares_outstanding

    annual.period = f"{quarter.label} (UDM)"
    annual.basis = "udm"
    return True


def fetch() -> tuple[dict[str, Fundamentals], list[str]]:
    """Every filer's latest annual figures, keyed by digits-only CNPJ.

    Reads the two newest year-files, older first, so the newer overwrites.
    Reading only the newest is a trap: ``dfp_cia_aberta_2026.zip`` holds
    *financial year 2026*, which in August contains the seven companies whose
    fiscal year does not end in December — verified, it really is seven. The
    year before is where essentially every company's latest complete statement
    lives, and the overlay means an early filer still gets its newer numbers.

    Returns ``(records, warnings)``.
    """
    newest = newest_year_file(DFP_DIR, DFP_PATTERN)
    if newest is None:
        raise SourceShapeError("CVM DFP: nenhum arquivo anual no diretório publicado")
    _, newest_year = newest

    records: dict[str, Fundamentals] = {}
    warnings: list[str] = []
    for year in (newest_year - 1, newest_year):
        try:
            _fold_year(year, records, warnings)
        except SourceShapeError as exc:
            warnings.append(f"CVM DFP {year}: {exc}")
        except Exception as exc:  # noqa: BLE001 — one absent year is survivable
            logger.info("cvm dfp %s unavailable: %s", year, exc)

    if not records:
        raise SourceShapeError("CVM DFP: nenhuma companhia lida dos arquivos anuais")

    # Roll each company forward to the twelve months ending on its own newest
    # quarter. Best-effort: a company whose ITR is missing, or whose periods do
    # not line up, keeps its annual figures and says so through ``period``.
    try:
        quarters, itr_warnings = fetch_quarterly()
        warnings.extend(itr_warnings)
        rolled = 0
        for cnpj, record in records.items():
            quarter = quarters.get(cnpj)
            if quarter is not None and _apply_quarterly(record, quarter):
                rolled += 1
        logger.info("cvm: %s of %s companies rolled to a trailing twelve months", rolled, len(records))
    except SourceShapeError as exc:
        warnings.append(f"CVM ITR: {exc} — usando apenas o balanço anual")
    except Exception as exc:  # noqa: BLE001 — fresher is better, not required
        logger.exception("cvm itr overlay failed")
        warnings.append(f"CVM ITR indisponível ({exc}) — usando apenas o balanço anual")

    # Gross debt is the sum of the two borrowing lines; either alone is a
    # half-answer, and "nothing reported" must stay None rather than become 0.
    for record in records.values():
        parts = [v for v in (record.debt_current, record.debt_noncurrent) if v is not None]
        record.debt = sum(parts, Decimal(0)) if parts else None

    return records, warnings
