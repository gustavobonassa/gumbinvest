"""Ratios, from filings and prices. Pure Decimal, no I/O, no database.

Every screening figure is derived here rather than borrowed, so each one traces
back to an account code in a published filing. That is the point of the whole
pipeline: a P/L on the screener can be checked against Vale's own income
statement, not against a provider's undocumented derivation.

Two rules run through the module and both exist because a screener punishes
quiet errors much harder than visible gaps:

* **Every function returns ``None`` rather than raising or improvising.** A
  company with negative equity has no meaningful P/VP; the honest output is
  nothing, not a negative number that sorts as "cheap".
* **Absurd results are rejected.** A P/L of 40 000 is an artefact of a company
  that earned almost nothing, and leaving it in would put that company at the
  top of a "most expensive" sort and at the bottom of a "cheapest" one.
"""
from __future__ import annotations

from decimal import Decimal, DivisionByZero, InvalidOperation

HUNDRED = Decimal(100)
THOUSAND = Decimal(1000)

#: Bands outside which a computed ratio is an artefact rather than a fact.
#: Wide on purpose — these reject unit errors and near-zero denominators, not
#: unusual companies.
_PE_LIMIT = Decimal(10_000)
_PB_LIMIT = Decimal(1_000)
_PCT_LIMIT = Decimal(100_000)
_DE_LIMIT = Decimal(1_000)


def _safe(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    """``numerator / denominator``, or None when that is not a real answer."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    try:
        return numerator / denominator
    except (DivisionByZero, InvalidOperation):
        return None


def _bounded(value: Decimal | None, limit: Decimal) -> Decimal | None:
    """Drop a figure whose magnitude says it is an artefact."""
    if value is None:
        return None
    return value if abs(value) <= limit else None


# ---------------------------------------------------------------------------
# Share count scale — the one genuinely ambiguous input

#: A listed company's price-to-book sits in here. The two candidate share
#: scales differ by a factor of a thousand, so at most one of them can produce
#: a P/VP inside this band; that is what makes the choice a deduction rather
#: than a guess.
_PB_PLAUSIBLE = (Decimal("0.02"), Decimal(50))


def resolve_share_scale(
    shares: Decimal | None, equity: Decimal | None, price: Decimal | None
) -> tuple[Decimal | None, str | None]:
    """Decide whether a filed share count is in units or thousands.

    CVM's ``composicao_capital`` has no scale column and filers genuinely
    disagree — Banco do Brasil files 5 730 834 040 shares while Vale files
    4 539 007 for its 4,5 billion. Both satisfy the form. Getting it wrong
    misprices a company by a factor of a thousand, so it is deduced from the
    price rather than assumed: book value per share is only consistent with the
    market price at one of the two scales.

    Returns ``(shares, scale_label)``; ``(None, None)`` when the evidence does
    not single one out, which leaves market cap and P/L missing for that
    company rather than wrong.
    """
    if shares is None or shares <= 0:
        return None, None
    if equity is None or equity <= 0 or price is None or price <= 0:
        # Nothing to test against. The count is unusable for market cap, and
        # saying so beats publishing a number that may be 1000x off.
        return None, None

    low, high = _PB_PLAUSIBLE
    matches: list[tuple[Decimal, str]] = []
    for count, label in ((shares, "unidades"), (shares * THOUSAND, "milhares")):
        book_per_share = equity / count
        if book_per_share <= 0:
            continue
        price_to_book = price / book_per_share
        if low <= price_to_book <= high:
            matches.append((count, label))
    if len(matches) == 1:
        return matches[0]
    return None, None


# ---------------------------------------------------------------------------
# Valuation


def market_cap(price: Decimal | None, shares: Decimal | None) -> Decimal | None:
    if price is None or shares is None or price <= 0 or shares <= 0:
        return None
    return price * shares


def price_earnings(
    price: Decimal | None, net_income: Decimal | None, shares: Decimal | None
) -> Decimal | None:
    """P/L. None for a loss-making company — a negative P/L is not "cheap"."""
    if net_income is None or net_income <= 0:
        return None
    earnings_per_share = _safe(net_income, shares)
    if earnings_per_share is None or earnings_per_share <= 0:
        return None
    return _bounded(_safe(price, earnings_per_share), _PE_LIMIT)


def book_value_per_share(equity: Decimal | None, shares: Decimal | None) -> Decimal | None:
    if equity is None or equity <= 0:
        return None
    return _safe(equity, shares)


def price_book(price: Decimal | None, equity: Decimal | None, shares: Decimal | None) -> Decimal | None:
    """P/VP. None on negative equity, where the ratio has no meaning."""
    per_share = book_value_per_share(equity, shares)
    if per_share is None or per_share <= 0:
        return None
    return _bounded(_safe(price, per_share), _PB_LIMIT)


# ---------------------------------------------------------------------------
# Profitability and growth


def _percent(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    ratio = _safe(numerator, denominator)
    return None if ratio is None else _bounded(ratio * HUNDRED, _PCT_LIMIT)


def return_on_equity_pct(net_income: Decimal | None, equity: Decimal | None) -> Decimal | None:
    """ROE. Undefined on negative equity: the sign would invert the meaning."""
    if equity is None or equity <= 0:
        return None
    return _percent(net_income, equity)


def margin_pct(part: Decimal | None, revenue: Decimal | None) -> Decimal | None:
    """A margin over revenue. Undefined when there is no revenue to divide by."""
    if revenue is None or revenue <= 0:
        return None
    return _percent(part, revenue)


def growth_pct(current: Decimal | None, prior: Decimal | None) -> Decimal | None:
    """Year-on-year growth.

    The prior period must be positive: growth measured from a loss or from zero
    produces a number whose sign and magnitude are both meaningless (a swing
    from -1 to +100 is not "10 100 % growth" in any useful sense).
    """
    if current is None or prior is None or prior <= 0:
        return None
    return _bounded((current - prior) / prior * HUNDRED, _PCT_LIMIT)


def debt_to_equity(debt: Decimal | None, equity: Decimal | None) -> Decimal | None:
    if debt is None or equity is None or equity <= 0 or debt < 0:
        return None
    return _bounded(debt / equity, _DE_LIMIT)


# ---------------------------------------------------------------------------
# Income


def dividend_yield_pct(
    dividends_paid: Decimal | None, cap: Decimal | None
) -> Decimal | None:
    """Cash dividends over market value.

    ``dividends_paid`` is what actually left the company over the year, taken
    from the audited cash-flow statement — a firmer figure than a declared
    schedule, though by the same token a backward-looking one.
    """
    if dividends_paid is None or dividends_paid <= 0:
        return None
    return _bounded(_percent(dividends_paid, cap), Decimal(200))


def payout_pct(dividends_paid: Decimal | None, net_income: Decimal | None) -> Decimal | None:
    if dividends_paid is None or net_income is None or net_income <= 0:
        return None
    return _bounded(_percent(dividends_paid, net_income), Decimal(1_000))


# ---------------------------------------------------------------------------
# Storage helpers


def quantize_big(value: Decimal | None) -> Decimal | None:
    """Round to whole currency units, for the BIGMONEY columns.

    Caps and volumes run to twelve digits, and SQLite stores ``Numeric``
    through a float that quantizes past fifteen significant digits; keeping
    these whole leaves the precision entirely inside the exact range.
    """
    if value is None:
        return None
    return value.quantize(Decimal(1))


def quantize_ratio(value: Decimal | None) -> Decimal | None:
    """Six decimals, matching the RATIO column."""
    if value is None:
        return None
    return value.quantize(Decimal("0.000001"))


def quantize_money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(Decimal("0.000001"))
