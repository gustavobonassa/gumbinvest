"""Coin vocabulary: what a ticker on an exchange export actually is.

An exchange export names everything by symbol and nothing else — no ISIN, no
CUSIP, no product description — so this module is the whole security master for
the crypto side of the portfolio. It answers three questions:

* is this symbol money or an instrument (``BRL`` is cash, ``BTC`` is a holding);
* is it a dollar-pegged token, which is cash in all but name;
* what should the price provider be asked for.

Crypto is priced in **dollars**, not reais: ``BTC-USD`` exists on every provider
while ``BTC-BRL`` covers only the largest coins, and the portfolio already knows
how to carry a dollar-denominated holding (see :class:`app.db.models.Asset` and
:mod:`app.market.fx`). So a coin is booked exactly like a US share — USD asset,
converted to reais at PTAX — and every alt coin gets a quote instead of only the
handful with a local pair.
"""
from __future__ import annotations

from app.domain.enums import AssetKind

#: Government money. Never a holding: the portfolio tracks positions, not the
#: cash sitting in the exchange account (the same rule the broker statements
#: follow — see ``app.importer.pdf.movements.CASH_ONLY``).
FIAT: frozenset[str] = frozenset(
    {"BRL", "USD", "EUR", "GBP", "ARS", "AUD", "JPY", "TRY", "CHF", "CAD", "MXN", "ZAR"}
)

#: Tokens that track the dollar one-for-one. They are held (they show up in the
#: net worth) but they are not crypto exposure, hence their own family.
DOLLAR_PEGGED: frozenset[str] = frozenset(
    {"USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP", "PYUSD", "USDD", "GUSD"}
)

#: Names for the coins this portfolio has traded. Purely cosmetic — an unknown
#: symbol keeps its own ticker as the name rather than being rejected.
NAMES: dict[str, str] = {
    "ACA": "Acala",
    "ADA": "Cardano",
    "ADX": "AdEx",
    "AXS": "Axie Infinity",
    "BNB": "BNB",
    "BTC": "Bitcoin",
    "BUSD": "Binance USD",
    "CELO": "Celo",
    "DAI": "Dai",
    "DOGE": "Dogecoin",
    "DOT": "Polkadot",
    "ETH": "Ethereum",
    "FTM": "Fantom",
    "GALA": "Gala",
    "GLMR": "Moonbeam",
    "GMT": "STEPN",
    "IDEX": "IDEX",
    "ILV": "Illuvium",
    "KSM": "Kusama",
    "LINK": "Chainlink",
    "LUNA": "Terra",
    "MANA": "Decentraland",
    "MATIC": "Polygon",
    "NEAR": "NEAR Protocol",
    "NOT": "Notcoin",
    "PEPE": "Pepe",
    "PNUT": "Peanut the Squirrel",
    "SAND": "The Sandbox",
    "SC": "Siacoin",
    "SCRT": "Secret",
    "SHIB": "Shiba Inu",
    "SOL": "Solana",
    "THE": "Thena",
    "TON": "Toncoin",
    "UNI": "Uniswap",
    "USDC": "USD Coin",
    "USDT": "Tether",
    "VET": "VeChain",
    "XTZ": "Tezos",
}

#: Symbols a provider spells differently from the exchange. Yahoo files Polygon
#: under its new ticker and Terra Classic under the ``LUNC`` rename.
PROVIDER_SYMBOLS: dict[str, str] = {
    "MATIC": "POL",
    "LUNA": "LUNC",
}

#: Longest quote symbols first, so ``USDTBRL`` splits on ``BRL`` and not on a
#: shorter suffix that happens to match. Only used when a row does not carry the
#: unit next to the number, which the Binance exports normally do.
QUOTE_SYMBOLS: tuple[str, ...] = (
    "FDUSD",
    "BUSD",
    "USDT",
    "USDC",
    "TUSD",
    "TRY",
    "BRL",
    "EUR",
    "GBP",
    "AUD",
    "DAI",
    "BNB",
    "BTC",
    "ETH",
    "USD",
)


#: The instrument families a coin can land in.
CRYPTO_KINDS: frozenset[str] = frozenset({AssetKind.CRYPTO.value, AssetKind.STABLECOIN.value})

#: Appended to a coin's ticker when a security already holds that ticker.
#: Three-letter symbols are shared freely between the two worlds — SOL, LINK and
#: UNI are all listed equities somewhere — and ``Asset.ticker`` is unique, so
#: without this a coin and a share would silently merge into one position with
#: both histories in it.
TICKER_SUFFIX = ".CRYPTO"


def asset_symbol(ticker: str) -> str:
    """The exchange symbol behind a crypto asset's ticker."""
    upper = (ticker or "").upper()
    return upper[: -len(TICKER_SUFFIX)] if upper.endswith(TICKER_SUFFIX) else upper


def is_fiat(symbol: str) -> bool:
    return (symbol or "").upper() in FIAT


def is_stablecoin(symbol: str) -> bool:
    return (symbol or "").upper() in DOLLAR_PEGGED


def is_tracked(symbol: str) -> bool:
    """Whether the symbol becomes a position rather than a cash movement."""
    return bool(symbol) and not is_fiat(symbol)


def coin_kind(symbol: str) -> AssetKind:
    return AssetKind.STABLECOIN if is_stablecoin(symbol) else AssetKind.CRYPTO


def coin_name(symbol: str) -> str:
    upper = (symbol or "").upper()
    return NAMES.get(upper, upper)


def market_symbol_for(symbol: str) -> str:
    """The symbol a price provider should be asked for (``BTC`` -> ``BTC-USD``).

    Providers key crypto by pair, so the quote currency is part of the symbol.
    Dollars for the reason in the module docstring: it is the pair that exists
    for every coin, not just the top ten.
    """
    upper = (symbol or "").strip().upper()
    if not upper:
        return upper
    if "-" in upper:  # already a pair
        return upper
    return f"{PROVIDER_SYMBOLS.get(upper, upper)}-USD"


def split_pair(pair: str, base_hint: str = "", quote_hint: str = "") -> tuple[str, str]:
    """Split ``ETHUSDT`` into ``("ETH", "USDT")``.

    The hints come from the units printed next to the numbers ("0.00308BTC",
    "1000.8306BRL"), which is the only unambiguous source: ``USDTBRL`` and
    ``BTCBRL`` are the same shape, and a pair like ``BUSDUSDT`` can be read two
    ways by suffix alone. Splitting on the quote table is the fallback for an
    export that omits the units.
    """
    upper = (pair or "").strip().upper()
    base, quote = (base_hint or "").upper(), (quote_hint or "").upper()
    if base and quote:
        return base, quote
    if base and upper.startswith(base):
        return base, upper[len(base) :] or quote
    if quote and upper.endswith(quote):
        return upper[: -len(quote)] or base, quote
    for candidate in QUOTE_SYMBOLS:
        if upper.endswith(candidate) and len(upper) > len(candidate):
            return upper[: -len(candidate)], candidate
    return upper, ""
