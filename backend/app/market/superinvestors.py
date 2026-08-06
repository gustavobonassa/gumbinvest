"""Public portfolios of famous investors, straight from SEC 13F filings.

Any manager running more than US$ 100M in US-listed equities must file a
quarterly 13F with the SEC. EDGAR serves those filings as JSON indexes plus an
XML "information table" — free, no key, just a declared User-Agent. That is the
same raw material sites like Dataroma resell views of.

Honest limits of the data, surfaced in the API payload so the UI can repeat
them: a 13F is a snapshot of the quarter's last day, published up to 45 days
late, and covers only US-listed long positions — no cash, bonds, shorts or
non-US listings. It says where a great investor *was*, never where they are.

Everything is fetched on demand and kept in a process-local cache for a day:
filings change once a quarter, so a stale-by-hours copy is simply the copy.
"""
from __future__ import annotations

import re
import threading
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: SEC blocks anonymous automation: the UA must name the client *and* carry an
#: e-mail-shaped contact, or www.sec.gov answers 403 (verified empirically —
#: data.sec.gov is more lenient, the Archives host is not).
HEADERS = {"User-Agent": settings.sec_user_agent}

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

#: Curated managers. CIKs verified against EDGAR (names must match the filer).
INVESTORS: list[dict] = [
    {
        "slug": "buffett",
        "manager": "Warren Buffett",
        "fund": "Berkshire Hathaway",
        "cik": "0001067983",
        "description": "O maior investidor de valor de todos os tempos. Poucas posições, "
        "enormes e mantidas por décadas — qualidade a preço justo.",
    },
    {
        "slug": "ackman",
        "manager": "Bill Ackman",
        "fund": "Pershing Square Capital",
        "cik": "0001336528",
        "description": "Ativista concentrado: raramente mais de dez posições, todas grandes "
        "empresas com marcas fortes e fluxo de caixa previsível.",
    },
    {
        "slug": "burry",
        "manager": "Michael Burry",
        "fund": "Scion Asset Management",
        "cik": "0001649339",
        "description": "O investidor de 'A Grande Aposta'. Carteira pequena, contrária e "
        "que muda rápido — um retrato por trimestre envelhece depressa aqui.",
    },
    {
        "slug": "terry-smith",
        "manager": "Terry Smith",
        "fund": "Fundsmith",
        "cik": "0001569205",
        "description": "O 'Buffett inglês': compre empresas excelentes, não pague caro "
        "demais e — principalmente — não faça nada.",
    },
    {
        "slug": "li-lu",
        "manager": "Li Lu",
        "fund": "Himalaya Capital",
        "cik": "0001709323",
        "description": "Gestor que Charlie Munger escolheu para cuidar do próprio "
        "dinheiro. Pouquíssimas posições, prazo de década.",
    },
    {
        "slug": "druckenmiller",
        "manager": "Stanley Druckenmiller",
        "fund": "Duquesne Family Office",
        "cik": "0001536411",
        "description": "Décadas sem um ano negativo. Macro e concentração: aposta grande "
        "quando tem convicção, sai rápido quando muda de ideia.",
    },
    {
        "slug": "tepper",
        "manager": "David Tepper",
        "fund": "Appaloosa",
        "cik": "0001656456",
        "description": "Especialista em comprar no pânico — dívidas e ações em crise. "
        "Um dos melhores retornos da história dos hedge funds.",
    },
    {
        "slug": "marks",
        "manager": "Howard Marks",
        "fund": "Oaktree Capital",
        "cik": "0000949509",
        "description": "Referência em ciclos de mercado e crédito. As cartas dele são "
        "leitura obrigatória; a carteira 13F mostra só a ponta acionária.",
    },
    {
        "slug": "dalio",
        "manager": "Ray Dalio",
        "fund": "Bridgewater Associates",
        "cik": "0001350694",
        "description": "O maior hedge fund do mundo. Diversificação sistemática entre "
        "centenas de posições — o oposto dos concentrados desta lista.",
    },
    {
        "slug": "klarman",
        "manager": "Seth Klarman",
        "fund": "Baupost Group",
        "cik": "0001061768",
        "description": "Autor de 'Margin of Safety'. Valor profundo com paciência e "
        "caixa em abundância quando não há o que comprar.",
    },
]

_BY_SLUG = {item["slug"]: item for item in INVESTORS}

CACHE_TTL = timedelta(hours=24)
_CACHE: dict[str, tuple[datetime, dict]] = {}
_TICKER_CACHE: dict[str, str] | None = None
_LOCK = threading.Lock()


def clear_cache() -> None:
    """Tests wipe the module state between cases."""
    global _TICKER_CACHE
    with _LOCK:
        _CACHE.clear()
        _TICKER_CACHE = None


def list_investors() -> list[dict]:
    return [
        {k: item[k] for k in ("slug", "manager", "fund", "description")} for item in INVESTORS
    ]


# ---------------------------------------------------------------------------
# EDGAR plumbing
# ---------------------------------------------------------------------------
def _get_json(client: httpx.Client, url: str) -> dict:
    response = client.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.json()


def _latest_filings(client: httpx.Client, cik: str) -> list[dict]:
    """The newest filing of each of the two most recent quarters.

    Amendments (13F-HR/A) replace the original, so within a quarter the last
    filing by date wins. Two quarters are needed because the interesting part
    of a snapshot is what changed since the previous one.
    """
    data = _get_json(client, SUBMISSIONS_URL.format(cik=cik))
    recent = data.get("filings", {}).get("recent", {})
    by_quarter: dict[str, dict] = {}
    for form, accession, filed, report in zip(
        recent.get("form", []),
        recent.get("accessionNumber", []),
        recent.get("filingDate", []),
        recent.get("reportDate", []),
    ):
        if form not in ("13F-HR", "13F-HR/A"):
            continue
        current = by_quarter.get(report)
        if current is None or filed > current["filed"]:
            by_quarter[report] = {"accession": accession, "filed": filed, "report": report}
    ordered = sorted(by_quarter.values(), key=lambda item: item["report"], reverse=True)
    return ordered[:2]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _holdings(client: httpx.Client, cik: str, accession: str) -> dict[str, dict]:
    """The filing's information table, aggregated by CUSIP.

    Big managers split one issuer across several rows (different discretion or
    sub-managers); the reader wants the position, so rows fold together. Put
    and call entries are skipped — they are bets on the price, not the stake
    itself, and mixing them in silently inflates the 'portfolio'.
    """
    cik_int = str(int(cik))
    accession_flat = accession.replace("-", "")
    index = _get_json(client, f"{ARCHIVES_URL.format(cik_int=cik_int, accession=accession_flat)}/index.json")
    tables = [
        item["name"]
        for item in index.get("directory", {}).get("item", [])
        if item["name"].lower().endswith(".xml") and "primary_doc" not in item["name"].lower()
    ]
    if not tables:
        return {}
    # The information table is the only sizeable XML in the folder.
    tables.sort(key=lambda name: int(next((i["size"] for i in index["directory"]["item"] if i["name"] == name), 0) or 0), reverse=True)
    response = client.get(
        f"{ARCHIVES_URL.format(cik_int=cik_int, accession=accession_flat)}/{tables[0]}", headers=HEADERS
    )
    response.raise_for_status()

    rows: dict[str, dict] = {}
    root = ET.fromstring(response.content)
    for table in root.iter():
        if _local(table.tag) != "infoTable":
            continue
        fields = {_local(child.tag): child for child in table}
        if fields.get("putCall") is not None and (fields["putCall"].text or "").strip():
            continue
        cusip = (fields["cusip"].text or "").strip() if "cusip" in fields else ""
        if not cusip:
            continue
        issuer = (fields["nameOfIssuer"].text or "").strip() if "nameOfIssuer" in fields else cusip
        value = Decimal((fields["value"].text or "0").strip() or "0") if "value" in fields else Decimal(0)
        shares = Decimal(0)
        amount = fields.get("shrsOrPrnAmt")
        if amount is not None:
            for child in amount:
                if _local(child.tag) == "sshPrnamt":
                    shares = Decimal((child.text or "0").strip() or "0")
        row = rows.setdefault(cusip, {"issuer": issuer, "value": Decimal(0), "shares": Decimal(0)})
        row["value"] += value
        row["shares"] += shares
    return rows


# ---------------------------------------------------------------------------
# Issuer -> ticker (best effort)
# ---------------------------------------------------------------------------
_SUFFIXES = frozenset(
    "INC CORP CORPORATION CO COMPANY LTD PLC LP LLC SA NV AG THE OF NEW COM CL A B".split()
)


def _normalize(name: str) -> str:
    tokens = re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split()
    return " ".join(token for token in tokens if token not in _SUFFIXES)


def _ticker_map(client: httpx.Client) -> dict[str, str]:
    """Normalized company name -> ticker, from the SEC's own registry.

    13F issuer names are typed by the filer ("OCCIDENTAL PETE CORP"), so only
    exact normalized matches get a ticker — a wrong link is worse than none.
    The registry is cap-ordered, so the first name wins (Alphabet -> GOOGL).
    """
    global _TICKER_CACHE
    if _TICKER_CACHE is not None:
        return _TICKER_CACHE
    mapping: dict[str, str] = {}
    try:
        data = _get_json(client, TICKERS_URL)
        for item in data.values():
            key = _normalize(item.get("title", ""))
            if key and key not in mapping:
                mapping[key] = item.get("ticker", "")
    except Exception as exc:  # noqa: BLE001 — tickers are decoration, not data
        logger.warning("superinvestors: ticker registry unavailable: %s", exc)
    _TICKER_CACHE = mapping
    return mapping


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def wallet(slug: str) -> dict:
    """One investor's latest disclosed portfolio, with quarter-over-quarter changes."""
    investor = _BY_SLUG.get(slug)
    if investor is None:
        raise KeyError(slug)

    with _LOCK:
        cached = _CACHE.get(slug)
        if cached and datetime.now(UTC) - cached[0] < CACHE_TTL:
            return cached[1]

    with httpx.Client(timeout=settings.request_timeout, follow_redirects=True) as client:
        filings = _latest_filings(client, investor["cik"])
        if not filings:
            raise LookupError(f"no 13F filings for {slug}")
        current = _holdings(client, investor["cik"], filings[0]["accession"])
        previous = (
            _holdings(client, investor["cik"], filings[1]["accession"]) if len(filings) > 1 else {}
        )
        tickers = _ticker_map(client)

    total = sum((row["value"] for row in current.values()), Decimal(0))
    holdings = []
    for cusip, row in sorted(current.items(), key=lambda item: item[1]["value"], reverse=True):
        prior = previous.get(cusip)
        if prior is None:
            change = "new"
        elif row["shares"] > prior["shares"]:
            change = "increased"
        elif row["shares"] < prior["shares"]:
            change = "reduced"
        else:
            change = "unchanged"
        holdings.append(
            {
                "issuer": row["issuer"],
                "ticker": tickers.get(_normalize(row["issuer"])) or None,
                "cusip": cusip,
                "value": float(row["value"]),
                "shares": float(row["shares"]),
                "pct": float(row["value"] / total * 100) if total else 0.0,
                "change": change if previous else None,
            }
        )
    exits = [
        {"issuer": row["issuer"], "ticker": tickers.get(_normalize(row["issuer"])) or None}
        for cusip, row in sorted(previous.items(), key=lambda item: item[1]["value"], reverse=True)
        if cusip not in current
    ]

    payload = {
        **{k: investor[k] for k in ("slug", "manager", "fund", "description", "cik")},
        "quarter": filings[0]["report"],
        "filed_at": filings[0]["filed"],
        "previous_quarter": filings[1]["report"] if len(filings) > 1 else None,
        "total_value": float(total),
        "positions": len(holdings),
        "holdings": holdings,
        "exits": exits,
        "caveats": "Retrato do último dia do trimestre, publicado com até 45 dias de atraso. "
        "Somente posições compradas em bolsas dos EUA — sem caixa, renda fixa ou vendas a descoberto.",
    }
    with _LOCK:
        _CACHE[slug] = (datetime.now(UTC), payload)
    return payload
