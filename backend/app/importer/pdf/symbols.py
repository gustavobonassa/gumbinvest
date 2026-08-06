"""Resolving a statement row to a ticker.

Apex's English statements identify securities only by description and CUSIP, so
the ticker has to be recovered from somewhere. Three sources, in order of trust:

1. the row's own symbol, when the format prints one;
2. the CUSIP, via the seed table below plus anything learned from statements
   that print symbol and CUSIP together (stored on ``Asset.cusip``);
3. the description, matched against names already known for an asset.

Failing all three the row is reported unidentified rather than guessed at.
Some rows genuinely have no security to attribute: Avenue's April 2025
statement lists 27 withholding reversals with the ticker column simply left
blank. The import service decides what to do with them — a movement that
carries quantity is too important to drop, a stray cash line is not worth
inventing an asset for.
"""
from __future__ import annotations

import re

from app.importer.pdf.layout import normalize

#: CUSIP -> ticker for every security in the imported history. Extracted from
#: the statements themselves (Nomad's Apex Ascend files print both), so this is
#: a cache of observed facts rather than a hand-maintained security master.
CUSIP_TO_TICKER: dict[str, str] = {
    "01609W102": "BABA",  # Alibaba Group Holding Ltd ADR
    "03762U105": "ARI",  # Apollo Commercial Real Estate Finance
    "060505104": "BAC",  # Bank of America Corp
    "064058100": "BK",  # Bank of New York Mellon Corp
    "110448107": "BTI",  # British American Tobacco ADR
    "11135B100": "BRMK",  # Broadmark Realty Capital (merged into RC, 2023-05)
    "178587101": "CIO",  # City Office REIT
    "30303M102": "META",  # Meta Platforms Inc
    "554382101": "MAC",  # Macerich Co
    "58463J304": "MPW",  # Medical Properties Trust
    "595112103": "MU",  # Micron Technology Inc
    "65339F101": "NEE",  # NextEra Energy Inc
    "654106103": "NKE",  # Nike Inc Class B
    "74348A467": "NOBL",  # ProShares S&P 500 Dividend Aristocrats ETF
    "75574U101": "RC",  # Ready Capital Corporation
    "756109104": "O",  # Realty Income Corp
    "78440X887": "SLG",  # SL Green Realty Corp
    "85254J102": "STAG",  # STAG Industrial Inc
    "862121100": "STOR",  # Store Capital Corporation
    "91324P102": "UNH",  # UnitedHealth Group Inc
    "921932505": "VOOG",  # Vanguard S&P 500 Growth ETF
    "922908363": "VOO",  # Vanguard S&P 500 ETF
    "922908538": "VOT",  # Vanguard Mid-Cap Growth ETF
    "922908553": "VNQ",  # Vanguard Real Estate ETF
    "922908595": "VBK",  # Vanguard Small-Cap Growth ETF
    "92343V104": "VZ",  # Verizon Communications Inc
    "92936U109": "WPC",  # W. P. Carey Inc
    "949746101": "WFC",  # Wells Fargo & Co
    "G85158106": "STNE",  # StoneCo Ltd Class A
}

#: Tickers one broker spells differently, folded onto one name. Without this the
#: same holding would split into two assets with half the history each.
TICKER_ALIASES: dict[str, str] = {
    # Medical Properties Trust and Bank of New York Mellon both changed ticker.
    # The *new* symbol wins: the statements going back to 2021 say MPW and BK,
    # but the broker, the market and every quote now say MPT and BNY, and an app
    # that shows a ticker nobody uses any more is just confusing. Both spellings
    # fold onto one asset either way, so no history is split.
    "MPW": "MPT",
    "BK": "BNY",
}

#: Tickers that are REITs.
#:
#: This is a list rather than a rule because being a REIT is a tax structure,
#: not something a company puts in its name: "SL Green Realty", "STAG
#: Industrial" and "W. P. Carey" are all REITs and none of them says so. The
#: description heuristic below catches the few that do; everything else has to
#: be known.
REIT_TICKERS: frozenset[str] = frozenset(
    {
        "ARI",  # Apollo Commercial Real Estate Finance
        "BPYU",  # Brookfield Property REIT
        "BRMK",  # Broadmark Realty Capital
        "CIO",  # City Office REIT
        "MAC",  # Macerich
        "MPT",  # Medical Properties Trust
        "O",  # Realty Income
        "RC",  # Ready Capital
        "SLG",  # SL Green Realty
        "STAG",  # STAG Industrial
        "STOR",  # Store Capital
        "WPC",  # W. P. Carey
    }
)

#: Kept for a rename this portfolio should *not* follow — a holding whose
#: ticker must stay as filed while the provider is asked for something else.
#: Empty today: renames are handled by :data:`TICKER_ALIASES`, which moves the
#: asset itself onto the new symbol.
MARKET_SYMBOLS: dict[str, str] = {}


def market_symbol_for(ticker: str) -> str:
    """The symbol a price provider should be asked for."""
    upper = canonical_ticker(ticker)
    return MARKET_SYMBOLS.get(upper, upper)


#: Description fragments that identify a security when no CUSIP is printed.
#: Matched on the accent-free, alphanumeric form of the description's start.
DESCRIPTION_HINTS: tuple[tuple[str, str], ...] = (
    ("alibabagroupholding", "BABA"),
    ("apollocommercialrealestate", "ARI"),
    ("bankofamerica", "BAC"),
    ("bankamericacorp", "BAC"),
    ("banknewyorkmellon", "BNY"),
    ("bankofnewyorkmellon", "BNY"),
    ("britishamericantobacco", "BTI"),
    ("britishamerntob", "BTI"),
    ("broadmarkrealty", "BRMK"),
    ("cityofficereit", "CIO"),
    ("metaplatforms", "META"),
    ("macerich", "MAC"),
    ("medicalproperties", "MPT"),
    ("microntechnology", "MU"),
    ("nexteraenergy", "NEE"),
    ("nikeinc", "NKE"),
    ("proshares", "NOBL"),
    ("readycapital", "RC"),
    ("realtyincome", "O"),
    ("slgreenrealty", "SLG"),
    ("stagindustrial", "STAG"),
    ("stagindl", "STAG"),
    ("storecapital", "STOR"),
    ("storecapcorp", "STOR"),
    ("stoneco", "STNE"),
    ("unitedhealthgroup", "UNH"),
    ("vanguardadmiralfds", "VOOG"),
    ("vanguardsp500growth", "VOOG"),
    ("verizoncommunications", "VZ"),
    ("wellsfargo", "WFC"),
    ("wpcarey", "WPC"),
)

#: Vanguard funds all share the "VANGUARD INDEX FUNDS" prefix, so they are
#: separated by the fund name that follows it.
VANGUARD_HINTS: tuple[tuple[str, str], ...] = (
    ("smallcapgrowth", "VBK"),
    ("smlcpgrw", "VBK"),
    ("realestate", "VNQ"),
    ("midcapgrowth", "VOT"),
    ("mcapgr", "VOT"),
    ("sp500etf", "VOO"),
    ("sp500growth", "VOOG"),
    ("500grthidx", "VOOG"),
)

_SYNTHETIC_RE = re.compile(r"[^A-Z0-9]+")


def canonical_ticker(ticker: str) -> str:
    """Apply the alias table (``MPW`` -> ``MPT``)."""
    upper = (ticker or "").strip().upper()
    return TICKER_ALIASES.get(upper, upper)


def resolve_ticker(
    symbol: str,
    cusip: str,
    description: str,
    known_by_cusip: dict[str, str] | None = None,
) -> str | None:
    """The ticker for a row, or ``None`` when the statement does not say.

    ``known_by_cusip`` carries the mappings already learned from previously
    imported statements, so a security this module has never heard of only needs
    to appear once in a format that prints both identifiers.
    """
    if symbol:
        return canonical_ticker(symbol)

    if cusip:
        learned = (known_by_cusip or {}).get(cusip)
        if learned:
            return canonical_ticker(learned)
        seeded = CUSIP_TO_TICKER.get(cusip)
        if seeded:
            return canonical_ticker(seeded)

    key = normalize(description)
    if key.startswith("vanguard"):
        for needle, ticker in VANGUARD_HINTS:
            if needle in key:
                return ticker
    for needle, ticker in DESCRIPTION_HINTS:
        if key.startswith(needle):
            return ticker
    return None


def provisional_ticker(description: str, cusip: str = "") -> str:
    """A placeholder code for a movement that must not be lost.

    Used when a row changes a position but the security could not be
    identified: dropping it would corrupt the quantity held, so it is imported
    under a visible, obviously-provisional code instead.
    """
    source = cusip or description or "DESCONHECIDO"
    synthetic = _SYNTHETIC_RE.sub("-", source.upper()).strip("-")[:36]
    return f"?{synthetic}" if synthetic else "?DESCONHECIDO"
