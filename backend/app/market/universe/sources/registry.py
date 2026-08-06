"""Who the ticker belongs to: B3's listed-company roster and CVM's registry.

COTAHIST says what trades; it does not say which company is behind ``PETR4`` or
what sector it is in. Two published sources answer that, and — this is the point
— they can be joined *exactly*, with no name matching anywhere:

* **B3's listed-companies proxy** maps a four-letter ticker root to a company,
  its CNPJ and its listing segment. Same proxy family, and the same base64'd
  JSON path segment, as the dividend endpoint ``app.market.fundamentals``
  already uses.
* **CVM's ``cad_cia_aberta.csv``** keys on CNPJ and adds the sector, the CVM
  code and the registration status. 2 677 rows, of which 757 are ``ATIVO``.

B3 publishes the CNPJ bare (``46639922000144``) and CVM punctuated
(``08.773.135/0001-00``); reduced to digits they are the same key. That is why
there is no fuzzy matcher here: a ticker either resolves to a company or it does
not, and the ones that do not are counted rather than guessed at.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from app.core.logging import get_logger

from . import BROWSER_UA, SourceShapeError, digits, fetch_bytes, read_csv

logger = get_logger(__name__)

B3_COMPANIES_URL = (
    "https://sistemaswebb3-listados.b3.com.br/listedCompaniesProxy/CompanyCall/GetInitialCompanies"
)
CVM_REGISTRY_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv"
#: Every listed paper's index memberships, for the whole market, in one call.
B3_INDEXES_URL = (
    "https://sistemaswebb3-listados.b3.com.br/indexProxy/indexCall/GetStockIndex"
)

SOURCE_B3 = "b3-listadas"
SOURCE_CVM = "cvm-cadastro"

#: B3 pages this endpoint; 120 keeps the run to ~30 requests for 3 500 records.
PAGE_SIZE = 120
#: A ceiling so a paging bug can never spin forever against B3.
MAX_PAGES = 60

CVM_COLUMNS = {"CNPJ_CIA", "DENOM_SOCIAL", "CD_CVM", "SIT", "SETOR_ATIV"}


@dataclass(frozen=True, slots=True)
class Company:
    """One issuer, as the two registries jointly describe it."""

    root: str  # the 4-letter ticker root: PETR, HGLG
    name: str
    cnpj: str | None
    cvm_code: str | None
    segment: str | None
    status: str


def _b64(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _clean(value: object) -> str | None:
    """B3 serves latin-1 bytes inside a utf-8-declared JSON body.

    The mojibake that produces ("N\\ufffdo Classificados") is cosmetic but it
    would be stored and shown, so the replacement characters are dropped rather
    than persisted.
    """
    text = str(value or "").strip()
    if not text:
        return None
    return text.replace("�", "") or None


def fetch_b3_companies() -> list[Company]:
    """Every issuer B3 lists, paged. Returns [] rather than raising on refusal.

    B3's proxies are occasionally unavailable and this is enrichment, not the
    roster — a run without it still produces a usable universe, just one
    without CNPJs for the companies. The caller records that as a warning.
    """
    companies: list[Company] = []
    seen: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        payload = {"language": "pt-br", "pageNumber": page, "pageSize": PAGE_SIZE}
        try:
            raw = fetch_bytes(
                f"{B3_COMPANIES_URL}/{_b64(payload)}",
                headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            )
            body = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 — enrichment must not sink the run
            logger.info("b3 listed companies page %s failed: %s", page, exc)
            break
        results = body.get("results") or []
        if not results:
            break
        for item in results:
            root = _clean(item.get("issuingCompany"))
            if not root or root in seen:
                continue
            seen.add(root)
            companies.append(
                Company(
                    root=root.upper(),
                    name=_clean(item.get("companyName")) or root,
                    cnpj=digits(str(item.get("cnpj") or "")) if item.get("cnpj") not in (None, "", "0") else None,
                    cvm_code=_clean(item.get("codeCVM")),
                    segment=_clean(item.get("segment")),
                    # B3 files "A" for active; anything else is not tradable.
                    status="ATIVO" if str(item.get("status") or "").upper() == "A" else "INATIVO",
                )
            )
        info = body.get("page") or {}
        if page >= int(info.get("totalPages") or 0):
            break
    return companies


@dataclass(frozen=True, slots=True)
class CvmCompany:
    """A CVM registration, keyed by CNPJ."""

    cnpj: str
    name: str
    cvm_code: str | None
    sector: str | None
    #: ATIVO / CANCELADA / SUSPENSO(A) — declared by the registry, not inferred.
    status: str


def fetch_cvm_registry() -> dict[str, CvmCompany]:
    """CVM's open-company registry, keyed by digits-only CNPJ.

    Raises :class:`SourceShapeError` when the published columns have changed —
    the caller then skips the enrichment and leaves existing rows untouched.
    """
    raw = fetch_bytes(CVM_REGISTRY_URL)
    out: dict[str, CvmCompany] = {}
    for row in read_csv(raw, CVM_COLUMNS):
        cnpj = digits(row.get("CNPJ_CIA"))
        if not cnpj:
            continue
        status = (row.get("SIT") or "").strip().upper() or "DESCONHECIDO"
        existing = out.get(cnpj)
        # A CNPJ can appear more than once across registration categories;
        # an active registration always wins over a cancelled one.
        if existing is not None and existing.status.startswith("ATIVO"):
            continue
        out[cnpj] = CvmCompany(
            cnpj=cnpj,
            name=(row.get("DENOM_SOCIAL") or "").strip(),
            cvm_code=(row.get("CD_CVM") or "").strip() or None,
            sector=(row.get("SETOR_ATIV") or "").strip() or None,
            status=status,
        )
    if not out:
        raise SourceShapeError("cadastro da CVM: nenhuma companhia lida do arquivo")
    return out


def fetch_index_membership() -> dict[str, str]:
    """ticker -> ",IBOV,IBRA,IDIV," for every paper B3 indexes.

    One request covers the market — verified at 486 tickers — which is why
    this is worth having at all: index membership is the sharpest screening
    filter B3 publishes, and it costs a single call to keep current.

    The stored value is comma-*bounded* as well as comma-separated, so a
    membership test is ``LIKE '%,IBOV,%'``: an exact token match that works
    identically on SQLite and Postgres, where a bare ``%IBOV%`` would also
    match a longer code that merely starts the same way.

    Returns {} rather than raising — this is enrichment, and a run without it
    is a universe with one fewer filter, not a broken one.
    """
    try:
        raw = fetch_bytes(
            f"{B3_INDEXES_URL}/{_b64({'language': 'pt-br'})}",
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
        results = (json.loads(raw) or {}).get("results") or []
    except Exception as exc:  # noqa: BLE001 — enrichment must not sink the run
        logger.info("b3 index membership unavailable: %s", exc)
        return {}

    out: dict[str, str] = {}
    for item in results:
        ticker = _clean(item.get("code"))
        codes = [part.strip().upper() for part in str(item.get("indexes") or "").split(",")]
        codes = sorted({code for code in codes if code})
        if ticker and codes:
            out[ticker.upper()] = "," + ",".join(codes) + ","
    return out


def ticker_root(ticker: str) -> str:
    """``PETR4`` -> ``PETR``; ``HGLG11`` -> ``HGLG``; ``AAPL34`` -> ``AAPL``.

    B3 keys its company roster on this root, and every class of a company
    shares it, which is what lets one registry row serve PETR3 and PETR4 alike.
    """
    letters = []
    for char in ticker.strip().upper():
        if char.isdigit():
            break
        letters.append(char)
    return "".join(letters)
