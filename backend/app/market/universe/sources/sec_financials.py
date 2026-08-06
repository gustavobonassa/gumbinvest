"""US company fundamentals from the SEC's bulk XBRL datasets.

The exact American counterpart of what CVM publishes for Brazil, and the reason
the US leg can exist at all under this feature's no-per-ticker rule: the SEC
republishes *every numeric fact from every filing* as one ZIP per quarter. A
quarter is ~85 MB holding ~3,7 million facts for ~5 700 filers, and it parses in
under ten seconds. The alternative — XBRL ``companyfacts`` per company — would
be thousands of requests for the same information.

The layout does the hard part itself. Each fact carries ``qtrs``, the number of
quarters it spans, so a trailing-twelve-month figure needs no assembly where a
company files one: ``qtrs=4`` *is* twelve months ending on ``ddate``. Verified
against NVIDIA's 10-K, whose ``qtrs=4`` net income for 2025-01-31 reads
72,88 bi — its published fiscal-year figure. Balance-sheet items carry
``qtrs=0`` and are simply the newest. Companies that only filed 10-Qs in the
window have their four most recent quarters summed instead.

**What this cannot give: prices.** No free bulk source publishes US closes —
the one candidate probed returns HTML rather than data — so US rows have a
share count but no market capitalisation, and therefore no P/L, no P/VP and no
twelve-month return. Everything price-independent is here and is real: ROE,
margins, growth, leverage. Revenue stands in as the size ranking. A US paper an
AI wallet actually buys is priced through the ordinary quote provider at that
moment, which is what that provider is reserved for.
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.core.config import settings
from app.core.logging import get_logger
from app.domain.enums import AssetKind

from . import SourceShapeError, fetch_bytes

logger = get_logger(__name__)

DATASET_URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{quarter}.zip"

SOURCE = "sec-xbrl"

#: Quarters scanned, newest first. Five covers every filer: a company appears in
#: the quarter it filed, so a full year plus one guarantees at least one annual
#: report each, and gives 10-Q-only filers the four quarters a trailing figure
#: needs. Fewer would silently drop whoever filed longest ago.
QUARTERS_TO_SCAN = 5

SUB_COLUMNS = {"adsh", "cik", "name", "sic", "form", "period", "fy", "fp", "filed"}
NUM_COLUMNS = {"adsh", "tag", "ddate", "qtrs", "uom", "segments", "coreg", "value"}

#: XBRL tags per figure, most preferred first. US GAAP offers several spellings
#: for the same line and companies genuinely differ: a product company files
#: ``RevenueFromContractWithCustomer…``, an older filer ``Revenues``. First
#: match wins, per company, so nothing is mixed.
_TAGS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "TotalRevenuesAndOtherIncome",
    ),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "gross_profit": ("GrossProfit",),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "shares_outstanding": (
        "EntityCommonStockSharesOutstanding",
        "CommonStockSharesOutstanding",
    ),
    "dividends_paid": (
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfDividends",
    ),
}

#: Figures that accumulate over a period (``qtrs`` > 0) rather than describing
#: a moment. Only these can be rolled into a trailing twelve months.
_FLOWS = frozenset({"revenue", "net_income", "gross_profit", "dividends_paid"})

#: Standard Industrial Classification 6798 is how a real-estate investment
#: trust identifies itself to the SEC. That is the whole REIT test — no name
#: matching, no guessing from the business description.
SIC_REIT = "6798"

#: SIC major group -> sector, so a US row reads like a B3 one. Anything
#: unmapped stays absent rather than being forced into a bucket it does not
#: belong to.
#:
#: **Order matters — narrow ranges come first.** The first match wins, and
#: without that Microsoft (7372, prepackaged software) lands in the generic
#: 7300-7399 "business services" bucket and Apple (3571, electronic computers)
#: in "machinery". Both are technology companies and a sector filter has to
#: say so.
_SIC_SECTORS: tuple[tuple[int, int, str], ...] = (
    (3570, 3579, "Tecnologia (Hardware)"),
    (3670, 3679, "Semicondutores"),
    (7370, 7379, "Software e Tecnologia"),
    (2833, 2836, "Biotecnologia e Farma"),
    (100, 999, "Agricultura"),
    (1000, 1499, "Extração Mineral"),
    (1500, 1799, "Construção Civil"),
    (2000, 2199, "Alimentos e Fumo"),
    (2200, 2399, "Têxtil e Vestuário"),
    (2400, 2699, "Madeira, Papel e Celulose"),
    (2800, 2899, "Química"),
    (2900, 2999, "Petróleo e Gás"),
    (3000, 3299, "Materiais Básicos"),
    (3300, 3399, "Metalurgia e Siderurgia"),
    (3400, 3599, "Máquinas e Equipamentos"),
    (3600, 3699, "Eletroeletrônicos"),
    (3700, 3799, "Veículos e Peças"),
    (3800, 3899, "Instrumentos e Saúde"),
    (4000, 4799, "Transporte e Logística"),
    (4800, 4899, "Telecomunicações"),
    (4900, 4999, "Utilidade Pública"),
    (5000, 5199, "Comércio (Atacado)"),
    (5200, 5999, "Comércio (Varejo)"),
    (6000, 6199, "Bancos"),
    (6200, 6299, "Serviços Financeiros"),
    (6300, 6499, "Seguradoras"),
    (6500, 6599, "Imobiliário"),
    (6700, 6799, "Holdings e Fundos"),
    (7000, 7299, "Serviços"),
    (7300, 7399, "Serviços Empresariais"),
    (7800, 7999, "Mídia e Entretenimento"),
    (8000, 8099, "Serviços Médicos"),
    (8700, 8799, "Serviços Profissionais"),
)


def sector_for(sic: str | None) -> str | None:
    try:
        code = int(sic or "")
    except ValueError:
        return None
    for low, high, label in _SIC_SECTORS:
        if low <= code <= high:
            return label
    return None


def kind_for(sic: str | None) -> str:
    """REIT or ordinary foreign equity, by the filer's own classification."""
    return AssetKind.REIT.value if (sic or "").strip() == SIC_REIT else AssetKind.STOCK_INTL.value


@dataclass(slots=True)
class UsFundamentals:
    """One filer's newest figures, already trailing where possible."""

    cik: str
    name: str = ""
    sic: str | None = None
    #: "2026Q1" / "2025FY" — which filing the figures come from.
    period: str | None = None
    basis: str = "anual"
    revenue: Decimal | None = None
    gross_profit: Decimal | None = None
    net_income: Decimal | None = None
    equity: Decimal | None = None
    assets: Decimal | None = None
    liabilities: Decimal | None = None
    shares_outstanding: Decimal | None = None
    dividends_paid: Decimal | None = None
    prior_revenue: Decimal | None = None
    prior_net_income: Decimal | None = None
    #: Debt is not filed as one line in US GAAP the way it is under CVM's
    #: chart, so leverage uses total liabilities over equity instead — a
    #: different, coarser measure, and named accordingly downstream.
    debt: Decimal | None = None


@dataclass(slots=True)
class _Facts:
    """Raw facts for one company, keyed by figure, before they are reduced."""

    #: field -> {(ddate, qtrs): value}, for the winning tag only.
    values: dict[str, dict[tuple[str, int], Decimal]] = field(default_factory=dict)
    #: field -> the tag that won, so a company never mixes two spellings.
    tag_used: dict[str, str] = field(default_factory=dict)
    name: str = ""
    sic: str | None = None
    period: str | None = None
    latest_filed: str = ""


def _recent_quarters(today: date, count: int) -> list[str]:
    """The ``2026q1``-style labels of the last ``count`` published quarters."""
    year, quarter = today.year, (today.month - 1) // 3 + 1
    out: list[str] = []
    for _ in range(count):
        quarter -= 1
        if quarter == 0:
            year, quarter = year - 1, 4
        out.append(f"{year}q{quarter}")
    return out


def _decimal(value: str) -> Decimal | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _read_quarter(raw: bytes, facts: dict[str, _Facts]) -> None:
    """Fold one quarterly dataset into the per-company accumulators."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = set(archive.namelist())
        for member in ("sub.txt", "num.txt"):
            if member not in names:
                raise SourceShapeError(f"conjunto da SEC sem o membro {member}")

        #: adsh -> cik, for the facts pass. Also carries the filing metadata.
        submissions: dict[str, tuple[str, str, str, str, str]] = {}
        with archive.open("sub.txt") as handle:
            reader = csv.DictReader(
                io.TextIOWrapper(handle, encoding="utf-8", errors="replace"), delimiter="\t"
            )
            missing = SUB_COLUMNS - set(reader.fieldnames or ())
            if missing:
                raise SourceShapeError(f"sub.txt sem colunas: {', '.join(sorted(missing))}")
            for row in reader:
                cik = (row.get("cik") or "").strip()
                if cik:
                    submissions[row["adsh"]] = (
                        cik,
                        (row.get("name") or "").strip(),
                        (row.get("sic") or "").strip(),
                        f"{row.get('fy') or ''}{row.get('fp') or ''}".strip(),
                        (row.get("filed") or "").strip(),
                    )

        #: field -> preference index, for the "first spelling wins" rule.
        preference = {
            tag: (figure, rank)
            for figure, tags in _TAGS.items()
            for rank, tag in enumerate(tags)
        }

        with archive.open("num.txt") as handle:
            reader = csv.DictReader(
                io.TextIOWrapper(handle, encoding="utf-8", errors="replace"), delimiter="\t"
            )
            missing = NUM_COLUMNS - set(reader.fieldnames or ())
            if missing:
                raise SourceShapeError(f"num.txt sem colunas: {', '.join(sorted(missing))}")
            for row in reader:
                # A segment or co-registrant breakdown is a slice of the
                # company, not the company. Only the consolidated total counts.
                if row.get("segments") or row.get("coreg"):
                    continue
                match = preference.get(row.get("tag") or "")
                if match is None:
                    continue
                submission = submissions.get(row.get("adsh") or "")
                if submission is None:
                    continue
                figure, rank = match
                unit = (row.get("uom") or "").strip().upper()
                if figure == "shares_outstanding":
                    if unit not in ("SHARES", "PURE"):
                        continue
                elif unit != "USD":
                    continue  # a figure in another currency cannot be compared
                value = _decimal(row.get("value") or "")
                if value is None:
                    continue
                try:
                    quarters = int(row.get("qtrs") or 0)
                except ValueError:
                    continue

                cik, name, sic, period, filed = submission
                record = facts.get(cik)
                if record is None:
                    record = _Facts()
                    facts[cik] = record
                if filed >= record.latest_filed:
                    record.latest_filed, record.name, record.sic = filed, name, sic
                    record.period = period or record.period

                previous_tag = record.tag_used.get(figure)
                if previous_tag is not None and previous_tag != row["tag"]:
                    previous_rank = _TAGS[figure].index(previous_tag)
                    if previous_rank <= rank:
                        continue  # a more-preferred spelling already won
                    record.values[figure] = {}
                record.tag_used[figure] = row["tag"]
                record.values.setdefault(figure, {})[
                    ((row.get("ddate") or "").strip(), quarters)
                ] = value


def _reduce(cik: str, record: _Facts) -> UsFundamentals:
    """Turn one company's raw facts into trailing figures."""
    out = UsFundamentals(cik=cik, name=record.name, sic=record.sic, period=record.period)

    for figure, observations in record.values.items():
        if not observations:
            continue
        if figure not in _FLOWS:
            # A moment in time: take the newest reported date.
            newest = max(observations, key=lambda key: key[0])
            setattr(out, figure, observations[newest])
            continue

        annual = {key: value for key, value in observations.items() if key[1] == 4}
        if annual:
            newest = max(annual, key=lambda key: key[0])
            setattr(out, figure, annual[newest])
            out.basis = "anual" if figure == "revenue" else out.basis
            prior = [key for key in annual if key[0] < newest[0]]
            if prior and figure in ("revenue", "net_income"):
                setattr(out, f"prior_{figure}", annual[max(prior, key=lambda key: key[0])])
            continue

        # No annual figure filed in the window: sum the four newest distinct
        # quarters. Fewer than four is not a year and is left absent.
        quarterly = sorted(
            ((key[0], value) for key, value in observations.items() if key[1] == 1),
            reverse=True,
        )
        deduped: dict[str, Decimal] = {}
        for ddate, value in quarterly:
            deduped.setdefault(ddate, value)
        recent = sorted(deduped.items(), reverse=True)[:4]
        if len(recent) == 4:
            setattr(out, figure, sum((value for _, value in recent), Decimal(0)))
            out.basis = "udm" if figure == "revenue" else out.basis
            older = sorted(deduped.items(), reverse=True)[4:8]
            if len(older) == 4 and figure in ("revenue", "net_income"):
                setattr(out, f"prior_{figure}", sum((value for _, value in older), Decimal(0)))

    # US GAAP files no single "borrowings" line the way CVM's chart does, so
    # leverage here is total liabilities over equity — coarser, and labelled
    # as such wherever it is shown.
    if out.liabilities is not None and out.liabilities >= 0:
        out.debt = out.liabilities
    return out


def fetch(
    quarters: int = QUARTERS_TO_SCAN, db=None
) -> tuple[dict[str, UsFundamentals], list[str]]:
    """Every US filer's newest figures, keyed by CIK (unpadded, as filed)."""
    from app.core.dates import local_today

    from . import sec

    # The same gate the ticker registry applies, and for the same reason: this
    # pulls hundreds of megabytes from the SEC, and doing that behind the
    # shipped placeholder is what gets an address range blocked — which would
    # take the superinvestors feature down with it. Checking it in only one of
    # the two US sources would leave the protection half-applied.
    headers = {"User-Agent": sec.check_user_agent(db)}
    facts: dict[str, _Facts] = {}
    warnings: list[str] = []
    read = 0

    for label in _recent_quarters(local_today(), quarters):
        try:
            raw = fetch_bytes(DATASET_URL.format(quarter=label), headers=headers)
        except Exception as exc:  # noqa: BLE001 — the newest may not be out yet
            logger.info("sec dataset %s unavailable: %s", label, exc)
            continue
        try:
            _read_quarter(raw, facts)
            read += 1
        except SourceShapeError as exc:
            warnings.append(f"SEC {label}: {exc}")
            logger.info("sec dataset %s unusable: %s", label, exc)

    if not read:
        raise SourceShapeError("SEC: nenhum conjunto trimestral pôde ser lido")
    if not facts:
        raise SourceShapeError("SEC: nenhum fato financeiro lido dos conjuntos")

    return {cik: _reduce(cik, record) for cik, record in facts.items()}, warnings
