"""Aporte inteligente: split the day's contribution across assets the user owns.

This is the one AI feature that DOES see the real portfolio — that is its whole
point: the user says how much new money arrived today and which classes it may
go to, and the model ranks the holdings they already have by what deserves the
marginal real right now. Nothing is executed — the output is a suggestion the
user carries to their broker.

By default no new tickers are proposed. When the user opts in, the model may
also name assets they do not hold yet; each one must survive the market gate
(:func:`verify_new_assets`) — resolved by the market, inside a selected
category — or it is dropped and surfaced, exactly like a hallucinated ticker.

The route builds the context and per-ticker facts here in the request, the
background job runs the model turn, and the same facts price the reply into
approximate quantities afterwards — the job itself never touches the database.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import SmartInvestRun
from app.importer.crypto import symbols as coins
from app.market import lookup
from app.market.fx import load_table
from app.portfolio.service import PortfolioService
from app.services import ai_wallet
from app.services.ai_research import decode_escapes
from app.services.jobs import _jsonable

logger = get_logger(__name__)

ZERO = Decimal(0)
CENT = Decimal("0.01")

#: Kinds an aporte can target, with the labels the UI and the model both see.
#: Deliberately excludes cash-like and residual kinds (stablecoins, rights,
#: OTHER): "invest more into" is not a decision they admit.
ELIGIBLE_KINDS: dict[str, str] = {
    "STOCK": "Ações",
    "FII": "FIIs",
    "ETF": "ETFs",
    "BDR": "BDRs",
    "STOCK_INTL": "Stocks (exterior)",
    "REIT": "REITs",
    "ETF_INTL": "ETFs internacionais",
    "CRYPTO": "Cripto",
    "FIXED_INCOME": "Renda fixa",
    "TREASURY": "Tesouro Direto",
}

#: Whole units on the B3, fractions abroad, satoshis in crypto. ``None`` means
#: the kind has no meaningful unit (renda fixa is bought in value).
_QTY_STEP: dict[str, Decimal | None] = {
    "STOCK": Decimal(1),
    "FII": Decimal(1),
    "ETF": Decimal(1),
    "BDR": Decimal(1),
    "STOCK_INTL": Decimal("0.0001"),
    "REIT": Decimal("0.0001"),
    "ETF_INTL": Decimal("0.0001"),
    "CRYPTO": Decimal("0.00000001"),
    "FIXED_INCOME": None,
    "TREASURY": None,
}

#: Kinds a *new* asset may be proposed in. Renda fixa and Tesouro have no
#: ticker the market can confirm — a "new CDB" would be unverifiable text.
NEW_ASSET_KINDS = frozenset(ELIGIBLE_KINDS) - {"FIXED_INCOME", "TREASURY"}

#: Upper bound on market lookups per run — each is a network round-trip, and
#: a reply naming twenty strangers is not a split of *this* aporte anyway.
MAX_NEW_ASSETS = 5


def _round2(value: Decimal | None) -> float | None:
    return None if value is None else round(float(value), 2)


def available_categories(db: Session, portfolio_id: int) -> list[dict]:
    """The kinds the user actually holds, with count and current value (BRL)."""
    service = PortfolioService(db, portfolio_id)
    out: dict[str, dict] = {}
    for pos in service.asset_positions():
        kind = pos.asset.kind
        if kind not in ELIGIBLE_KINDS:
            continue
        entry = out.setdefault(
            kind, {"kind": kind, "label": ELIGIBLE_KINDS[kind], "count": 0, "value": ZERO}
        )
        entry["count"] += 1
        entry["value"] += pos.market_value_base
    rows = sorted(out.values(), key=lambda entry: entry["value"], reverse=True)
    return [{**entry, "value": _round2(entry["value"])} for entry in rows]


def _price_in(
    price: Decimal | None, asset_currency: str, target_currency: str, usd_brl: Decimal | None
) -> Decimal | None:
    """``price`` converted into the aporte's currency — or None, never rate 1."""
    if price is None:
        return None
    if asset_currency == target_currency:
        return price
    if usd_brl is None or usd_brl <= ZERO:
        return None
    if asset_currency == "USD" and target_currency == "BRL":
        return price * usd_brl
    if asset_currency == "BRL" and target_currency == "USD":
        return price / usd_brl
    return None


def invest_context(
    db: Session, portfolio_id: int, kinds: list[str], currency: str
) -> tuple[list[dict], dict[str, dict]]:
    """(model context, per-ticker facts for pricing the reply afterwards).

    The context is what the model reasons over: each held position in the
    selected kinds with price, cost, weight and the cached fundamentals
    subset. The facts dict carries the Decimals the job needs later to turn
    suggested amounts into approximate quantities without reopening the DB.
    """
    service = PortfolioService(db, portfolio_id)
    positions = service.asset_positions()
    total_base = sum((pos.market_value_base for pos in positions), ZERO)
    usd_brl = load_table(db).latest

    wanted = set(kinds)
    context: list[dict] = []
    facts: dict[str, dict] = {}
    for pos in positions:
        asset = pos.asset
        if asset.kind not in wanted:
            continue
        label = ELIGIBLE_KINDS[asset.kind]
        item: dict = {
            "ticker": asset.ticker,
            "nome": asset.name,
            "categoria": label,
            "moeda": asset.currency,
            "quantidade": float(pos.position.quantity),
            "preco_medio": _round2(pos.position.average_price),
            "valor_de_mercado_brl": _round2(pos.market_value_base),
            "alocacao_na_carteira_pct": _round2(
                pos.market_value_base / total_base * 100 if total_base > ZERO else ZERO
            ),
            "resultado_nao_realizado_pct": _round2(
                pos.unrealized / pos.position.cost_basis * 100
                if pos.position.cost_basis > ZERO
                else ZERO
            ),
        }
        if pos.has_market_price:
            item["preco_atual"] = _round2(pos.effective_price)
        else:
            item["preco_atual"] = None
            item["observacao"] = "sem cotação neste momento"
        if asset.kind in ("FIXED_INCOME", "TREASURY"):
            item["observacao_categoria"] = "renda fixa: aporte em valor, sem quantidade de cotas"
        else:
            fundamentals = ai_wallet._fundamentals_for(db, asset)
            if fundamentals:
                item.update(fundamentals)
            else:
                # Missing data must read as missing, not as "nothing remarkable".
                item["observacao_fundamentos"] = "fundamentos indisponíveis neste momento"
        context.append(item)
        facts[asset.ticker] = {
            "name": asset.name,
            "kind": asset.kind,
            "label": label,
            "currency": asset.currency,
            "price": pos.effective_price if pos.has_market_price else None,
            "price_in_currency": _price_in(
                pos.effective_price if pos.has_market_price else None,
                asset.currency,
                currency,
                usd_brl,
            ),
        }
    return context, facts


def _selected_kind(hit_kind: str, kinds: set[str]) -> str | None:
    """The selected kind a market hit belongs to, or None when it fits none.

    Units (TAEE11, KLBN11) are shares in every sense an aporte cares about, but
    the lookup classifies them apart — fold them into Ações when selected.
    """
    if hit_kind in kinds:
        return hit_kind
    if hit_kind == "UNIT" and "STOCK" in kinds:
        return "STOCK"
    return None


def _quote_price(market_symbol: str) -> Decimal | None:
    from app.market.providers import get_provider  # local: avoids a cycle

    provider = get_provider()
    if provider.name == "none":
        return None
    try:
        quote = provider.get_quotes([market_symbol]).get(market_symbol)
    except Exception:  # noqa: BLE001 — an unpriced pick still shows, in value
        logger.warning("smart invest: quote failed for %s", market_symbol, exc_info=True)
        return None
    return quote.price if quote is not None and quote.price else None


def _new_fact(
    ticker: str, name: str, kind: str, asset_currency: str, price: Decimal | None,
    currency: str, usd_brl: Decimal | None,
) -> dict:
    return {
        "ticker": ticker,
        "name": name,
        "kind": kind,
        "label": ELIGIBLE_KINDS[kind],
        "currency": asset_currency,
        "price": price,
        "price_in_currency": _price_in(price, asset_currency, currency, usd_brl),
        "is_new": True,
    }


def _verify_one(
    ticker: str, kinds: set[str], currency: str, usd_brl: Decimal | None
) -> dict | None:
    """Market facts for one proposed stranger, or None when it fails the gate."""
    market_kinds = kinds & (NEW_ASSET_KINDS - {"CRYPTO"})
    if market_kinds:
        hit = lookup.resolve(ticker)
        if hit is not None:
            kind = _selected_kind(hit.kind, market_kinds)
            if kind is not None:
                return _new_fact(
                    hit.ticker, hit.name[:255], kind, hit.currency,
                    _quote_price(hit.market_symbol), currency, usd_brl,
                )
    if "CRYPTO" in kinds:
        symbol = coins.asset_symbol(ticker)
        if symbol and not coins.is_fiat(symbol) and not coins.is_stablecoin(symbol):
            price = _quote_price(coins.market_symbol_for(symbol))
            if price is not None:  # a coin nobody can price is as good as nonexistent
                return _new_fact(
                    symbol, coins.coin_name(symbol), "CRYPTO", "USD", price, currency, usd_brl
                )
    return None


def verify_new_assets(
    data: dict | None,
    facts: dict[str, dict],
    kinds: list[str],
    currency: str,
    usd_brl: Decimal | None,
    *,
    limit: int = MAX_NEW_ASSETS,
) -> tuple[dict[str, dict], dict[str, str]]:
    """(facts for verified new assets, raw ticker → canonical ticker).

    Runs in the job thread — network only, no database. A proposed ticker the
    market resolves to something already held (``ITSA4.SA``) is aliased to the
    held position instead of becoming a "new" one.
    """
    raw = data.get("allocations") if isinstance(data, dict) else None
    wanted = set(kinds)
    new_facts: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    tried = 0
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker") or "").strip().upper()
        if not ticker or ticker in facts or ticker in aliases or ticker in new_facts:
            continue
        if tried >= limit:
            break
        tried += 1
        fact = _verify_one(ticker, wanted, currency, usd_brl)
        if fact is None:
            continue
        canonical = fact["ticker"]
        if canonical in facts:
            aliases[ticker] = canonical
            continue
        new_facts.setdefault(canonical, fact)
        if canonical != ticker:
            aliases[ticker] = canonical
    return new_facts, aliases


def normalize_allocations(
    data: dict | None,
    facts: dict[str, dict],
    amount: Decimal,
    aliases: dict[str, str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Validate the model's reply into ``[{ticker, amount, rationale}]``.

    Unknown tickers are dropped and surfaced (either the model was told "only
    what the user holds", or the stranger failed :func:`verify_new_assets` —
    a hallucination, not a pick), and a sum over the aporte is scaled down
    proportionally rather than rejected.
    """
    raw = data.get("allocations") if isinstance(data, dict) else None
    aliases = aliases or {}
    items: list[dict] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker") or "").strip().upper()
        ticker = aliases.get(ticker, ticker)
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        if ticker not in facts:
            skipped.append(ticker)
            continue
        try:
            value = Decimal(str(entry.get("amount")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if value <= ZERO:
            continue
        items.append(
            {
                "ticker": ticker,
                "amount": value,
                "rationale": decode_escapes(str(entry.get("rationale") or "").strip()),
            }
        )

    total = sum((item["amount"] for item in items), ZERO)
    if total > amount and total > ZERO:
        for item in items:
            item["amount"] = (amount * item["amount"] / total).quantize(CENT, rounding=ROUND_DOWN)
    else:
        for item in items:
            item["amount"] = item["amount"].quantize(CENT, rounding=ROUND_HALF_UP)
    return [item for item in items if item["amount"] > ZERO], skipped


def record_run(db: Session, portfolio_id: int, result: dict) -> SmartInvestRun:
    """Persist a finished analysis — the registry alone forgets it in an hour."""
    row = SmartInvestRun(
        portfolio_id=portfolio_id,
        amount=Decimal(str(result["amount"])),
        currency=result["currency"],
        provider=result["provider"],
        model=result["model"],
        payload=_jsonable(result),
    )
    db.add(row)
    return row


def list_runs(db: Session, portfolio_id: int, limit: int = 30) -> list[dict]:
    rows = db.scalars(
        select(SmartInvestRun)
        .where(SmartInvestRun.portfolio_id == portfolio_id)
        .order_by(SmartInvestRun.created_at.desc(), SmartInvestRun.id.desc())
        .limit(limit)
    ).all()
    return [serialize_run(row) for row in rows]


def serialize_run(row: SmartInvestRun) -> dict:
    return {**row.payload, "id": row.id, "created_at": row.created_at}


def enrich_allocations(items: list[dict], facts: dict[str, dict]) -> list[dict]:
    """Attach name/category and the approximate quantity each amount buys."""
    out: list[dict] = []
    for item in items:
        fact = facts[item["ticker"]]
        step = _QTY_STEP.get(fact["kind"])
        price = fact["price_in_currency"]
        quantity = None
        if step is not None and price is not None and price > ZERO:
            quantity = (item["amount"] / price).quantize(step, rounding=ROUND_DOWN)
        out.append(
            {
                "ticker": item["ticker"],
                "name": fact["name"],
                "kind": fact["kind"],
                "label": fact["label"],
                "amount": item["amount"],
                "approx_quantity": quantity,
                "current_price": fact["price"],
                "price_currency": fact["currency"],
                "rationale": item["rationale"],
                "is_new": bool(fact.get("is_new")),
            }
        )
    return out
