"""Accrual and valuation for fixed income (CDB, LCI, LCA, RDB, debentures).

No public API quotes a private CDB, but its value is *computable*: the paper
tracks a published index, and Banco Central publishes the index. This module
accrues each purchase from its own settlement date to today.

Conventions (CETIP/B3, 252 business days)
-----------------------------------------
Banco Central's series 12 already publishes the **daily** DI rate, so for a
paper paying ``p`` % of CDI the accumulated factor over the period is::

    factor = Π ( 1 + TDI_k × p/100 )        TDI_k = value_k / 100

An annual spread ("CDI + 2 %") compounds on top over business days::

    factor = Π ( 1 + TDI_k ) × (1 + spread) ** (business_days / 252)

A prefixed paper ignores the index entirely::

    factor = (1 + rate) ** (business_days / 252)

IPCA-linked papers use the monthly index plus the spread; the pro-rata rules
inside a month are approximated (see :func:`_ipca_factor`), which is documented
in the UI because it is a simplification, not an exact NTN-B calculation.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date

from app.core.dates import local_today
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Asset, FixedIncomeTerms, Transaction
from app.domain.enums import AssetKind, PositionEffect
from app.market.indices import load_series

logger = get_logger(__name__)

ZERO = Decimal(0)
ONE = Decimal(1)
BUSINESS_DAYS_PER_YEAR = Decimal(252)
#: Banco Central publishes the CDI one business day late, so a short lag is
#: normal. Only a gap wider than this means the series really is behind.
STALE_TOLERANCE_DAYS = 5

#: Instrument families valued by accrual rather than by a market quote.
ACCRUED_KINDS = {AssetKind.FIXED_INCOME.value}


@dataclass(slots=True)
class Accrual:
    """Result of valuing one fixed income position."""

    principal: Decimal
    value: Decimal
    interest: Decimal
    factor: Decimal
    business_days: int
    index_code: str
    through: date
    stale: bool = False

    @property
    def yield_percent(self) -> Decimal:
        if not self.principal:
            return ZERO
        return (self.interest / self.principal) * Decimal(100)


def _is_stale(last_known: date | None, wanted: date) -> bool:
    """True when the index series is materially behind the valuation date."""
    if last_known is None:
        return True
    return (wanted - last_known).days > STALE_TOLERANCE_DAYS


def _add_month(day: date) -> tuple[int, int]:
    """(year, month) of the month following ``day``."""
    return (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)


def _pow(base: Decimal, exponent: Decimal) -> Decimal:
    """``base ** exponent`` for Decimals (via float, precision is ample here)."""
    if exponent == 0:
        return ONE
    return Decimal(str(float(base) ** float(exponent)))


def _factor_table(db: Session, code: str, percent: Decimal) -> tuple[list[date], list[Decimal]]:
    """Cumulative growth of an index at a given percentage, day by day.

    ``cum[i]`` is the product of every daily factor up to ``days[i]``, so the
    growth over any window is one division instead of a walk. Valuing a paper
    on a single date does not care; valuing it on all 2 200 days of a chart is
    the difference between ten seconds and one.
    """
    cache = db.info.setdefault("fi_factor_tables", {})
    key = (code.upper(), str(percent))
    if key not in cache:
        ratio = percent / Decimal(100)
        days: list[date] = []
        cum: list[Decimal] = []
        running = ONE
        for day, value in load_series(db, code):
            running *= ONE + (value / Decimal(100)) * ratio
            days.append(day)
            cum.append(running)
        cache[key] = (days, cum)
    return cache[key]


def _daily_factor(
    db: Session, code: str, start: date, end: date, percent: Decimal
) -> tuple[Decimal, int]:
    """Compound a daily-rate index over ``(start, end]``.

    The settlement date itself does not yield — a paper bought on D earns the DI
    of every business day after D, up to and including the valuation date.
    """
    days, cum = _factor_table(db, code, percent)
    lo = bisect_right(days, start)
    hi = bisect_right(days, end)
    if hi <= lo:
        return ONE, 0
    return cum[hi - 1] / (cum[lo - 1] if lo > 0 else ONE), hi - lo


def _business_days(series: list[tuple[date, Decimal]], start: date, end: date) -> int:
    """Business days between two dates, taken from the index calendar itself."""
    days = [day for day, _ in series]
    return max(bisect_right(days, end) - bisect_right(days, start), 0)


def _ipca_factor(series: list[tuple[date, Decimal]], start: date, end: date) -> Decimal:
    """Compound monthly IPCA over whole months in the period (approximation)."""
    days = [day for day, _ in series]
    lo = bisect_left(days, date(start.year, start.month, 1))
    hi = bisect_right(days, date(end.year, end.month, 1))
    factor = ONE
    for _, value in series[lo:hi]:
        factor *= ONE + value / Decimal(100)
    return factor


def accrual_factor(
    db: Session,
    terms: FixedIncomeTerms,
    start: date,
    end: date | None = None,
) -> tuple[Decimal, int, bool]:
    """Growth factor for one paper between two dates.

    Returns ``(factor, business_days, stale)`` — ``stale`` means the index series
    does not reach ``end``, so the value is accrued only up to what is known.
    """
    end = end or local_today()
    if terms.maturity_date and terms.maturity_date < end:
        end = terms.maturity_date  # stop accruing once the paper matured
    if end <= start:
        return ONE, 0, False

    index_code = (terms.index_code or "CDI").upper()
    spread = Decimal(terms.spread_annual or 0) / Decimal(100)

    if index_code == "PRE":
        # A prefixed paper still needs a business-day count; CDI supplies the
        # calendar without contributing to the rate.
        calendar = load_series(db, "CDI")
        days = _business_days(calendar, start, end)
        rate = Decimal(terms.fixed_rate_annual or 0) / Decimal(100)
        last = calendar[-1][0] if calendar else None
        return (
            _pow(ONE + rate, Decimal(days) / BUSINESS_DAYS_PER_YEAR),
            days,
            _is_stale(last, end),
        )

    if index_code == "IPCA":
        monthly = load_series(db, "IPCA")
        calendar = load_series(db, "CDI")
        days = _business_days(calendar, start, end)
        factor = _ipca_factor(monthly, start, end)
        if spread:
            factor *= _pow(ONE + spread, Decimal(days) / BUSINESS_DAYS_PER_YEAR)
        last = monthly[-1][0] if monthly else None
        # IPCA is monthly and published mid-month, so allow a full month of lag.
        return factor, days, bool(last is None or (end.year, end.month) > _add_month(last))

    series = load_series(db, index_code)
    if not series:
        return ONE, 0, True
    percent = Decimal(terms.percent_of_index if terms.percent_of_index is not None else 100)
    factor, days = _daily_factor(db, index_code, start, end, percent)
    if spread:
        factor *= _pow(ONE + spread, Decimal(days) / BUSINESS_DAYS_PER_YEAR)
    return factor, days, _is_stale(series[-1][0], end)


def default_terms(asset: Asset) -> FixedIncomeTerms:
    """100 % of CDI — the sane default for a Brazilian CDB."""
    return FixedIncomeTerms(
        asset_id=asset.id,
        index_code="CDI",
        percent_of_index=Decimal(100),
        spread_annual=ZERO,
        fixed_rate_annual=ZERO,
    )


def get_terms(db: Session, asset: Asset, create: bool = False) -> FixedIncomeTerms | None:
    terms = db.get(FixedIncomeTerms, asset.id)
    if terms is None and create:
        terms = default_terms(asset)
        db.add(terms)
        db.flush()
    return terms


def ensure_terms_for_fixed_income(db: Session) -> int:
    """Give every fixed income asset a default 100 % CDI setting."""
    assets = db.scalars(select(Asset).where(Asset.kind.in_(ACCRUED_KINDS))).all()
    created = 0
    for asset in assets:
        if db.get(FixedIncomeTerms, asset.id) is None:
            db.add(default_terms(asset))
            created += 1
    if created:
        db.commit()
        logger.info("created default CDI terms for %s fixed income assets", created)
    return created


def movements(db: Session, portfolio_id: int, asset_id: int) -> list[tuple]:
    """Applications and redemptions of one paper, oldest first.

    Public because valuing a paper across every day of a chart re-reads the
    same handful of rows thousands of times: the caller loads them once and
    hands them to :func:`value_any`. Deliberately *not* cached here — a session
    that records a movement and then values the position must see it.
    """
    return db.execute(
        select(Transaction.trade_date, Transaction.gross_amount, Transaction.effect)
        .where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.asset_id == asset_id,
            Transaction.effect.in_([PositionEffect.ACQUIRE.value, PositionEffect.DISPOSE.value]),
        )
        .order_by(Transaction.trade_date)
    ).all()


def value_position(
    db: Session,
    asset: Asset,
    terms: FixedIncomeTerms,
    portfolio_id: int,
    through: date | None = None,
    rows: list[tuple] | None = None,
) -> Accrual | None:
    """Accrue every open purchase of a paper from its own settlement date.

    Purchases are accrued individually, so a position built in tranches is
    valued correctly instead of using a single average date.
    """
    through = through or local_today()
    rows = movements(db, portfolio_id, asset.id) if rows is None else rows
    if not rows:
        return None

    principal = ZERO
    value = ZERO
    total_days = 0
    stale = False
    redeemed = ZERO

    for trade_date, amount, effect in rows:
        # Valuing the paper *as of* a past day: what had not happened yet then
        # cannot count now, or a chart would show a position already redeemed.
        if trade_date > through:
            continue
        amount = Decimal(amount or 0)
        if effect == PositionEffect.DISPOSE.value:
            redeemed += amount
            continue
        factor, days, is_stale = accrual_factor(db, terms, trade_date, through)
        principal += amount
        value += amount * factor
        total_days = max(total_days, days)
        stale = stale or is_stale

    if principal <= ZERO:
        return None
    if redeemed > ZERO:
        # Partially or fully redeemed: scale the accrued value by what is left.
        remaining = max(principal - redeemed, ZERO)
        if remaining <= ZERO:
            return None
        value *= remaining / principal
        principal = remaining

    return Accrual(
        principal=principal,
        value=value,
        interest=value - principal,
        factor=(value / principal) if principal else ONE,
        business_days=total_days,
        index_code=(terms.index_code or "CDI").upper(),
        through=through,
        stale=stale,
    )


def value_account(
    db: Session,
    asset: Asset,
    terms: FixedIncomeTerms,
    portfolio_id: int,
    through: date | None = None,
    rows: list[tuple] | None = None,
) -> Accrual | None:
    """Accrue a hand-kept bank balance: every movement earns from its own date.

    A conta corrente is not a paper, and the difference shows on a withdrawal.
    Redeeming part of a CDB takes a *share* of the position — principal and the
    interest it earned — which is why :func:`value_position` scales the accrued
    value down. Taking R$ 1.000 out of a bank account takes exactly R$ 1.000:
    the interest the balance had already earned stays behind and keeps earning.

    Both cases fall out of treating the movements as a signed series, each
    compounded from its own date::

        value(t) = Σ deposits  a · F(d → t)  −  Σ withdrawals  a · F(w → t)

    So R$ 100 mil left for a year and then topped up with another R$ 100 mil is
    worth "200 mil plus the first year's interest", and R$ 1.000 taken out of a
    balance that had grown to R$ 110 mil leaves R$ 109 mil compounding — which
    is exactly what the bank statement would say.
    """
    through = through or local_today()
    rows = movements(db, portfolio_id, asset.id) if rows is None else rows
    if not rows:
        return None

    principal = ZERO
    value = ZERO
    total_days = 0
    stale = False

    for trade_date, amount, effect in rows:
        if trade_date > through:
            continue
        signed = Decimal(amount or 0)
        if effect == PositionEffect.DISPOSE.value:
            signed = -signed
        factor, days, is_stale = accrual_factor(db, terms, trade_date, through)
        principal += signed
        value += signed * factor
        total_days = max(total_days, days)
        stale = stale or is_stale

    if value <= ZERO:
        return None
    return Accrual(
        principal=principal,
        value=value,
        interest=value - principal,
        factor=(value / principal) if principal > ZERO else ONE,
        business_days=total_days,
        index_code=(terms.index_code or "CDI").upper(),
        through=through,
        stale=stale,
    )


def value_any(
    db: Session,
    asset: Asset,
    terms: FixedIncomeTerms,
    portfolio_id: int,
    through: date | None = None,
    rows: list[tuple] | None = None,
) -> Accrual | None:
    """Accrue a position by whichever rule its kind calls for."""
    accrue = value_account if asset.is_cash_account else value_position
    return accrue(db, asset, terms, portfolio_id, through, rows)


def implied_percent_of_index(
    db: Session, asset: Asset, terms: FixedIncomeTerms, portfolio_id: int
) -> dict | None:
    """Solve for the % of the index that reproduces what the paper actually paid.

    A redeemed paper is a closed experiment: principal in, principal + interest
    out. Searching for the percentage whose accrual matches those cash flows
    recovers the contracted rate, which the B3 export never states — and which is
    usually well above 100 % of CDI.
    """
    rows = db.execute(
        select(Transaction.trade_date, Transaction.gross_amount, Transaction.effect, Transaction.op_type)
        .where(Transaction.portfolio_id == portfolio_id, Transaction.asset_id == asset.id)
        .order_by(Transaction.trade_date)
    ).all()

    invested = ZERO
    proceeds = ZERO
    first_day: date | None = None
    last_day: date | None = None
    for trade_date, amount, effect, op_type in rows:
        amount = Decimal(amount or 0)
        if effect == PositionEffect.ACQUIRE.value:
            invested += amount
            first_day = trade_date if first_day is None else min(first_day, trade_date)
        elif effect == PositionEffect.DISPOSE.value or op_type == "INTEREST":
            proceeds += amount
            last_day = trade_date if last_day is None else max(last_day, trade_date)

    if invested <= ZERO or proceeds <= ZERO or first_day is None or last_day is None:
        return None
    target = proceeds / invested
    if target <= ONE:
        return None

    # The factor grows monotonically with the percentage, so bisect on it.
    low, high = Decimal(0), Decimal(500)
    probe = FixedIncomeTerms(
        asset_id=asset.id,
        index_code=terms.index_code or "CDI",
        percent_of_index=Decimal(100),
        spread_annual=ZERO,
        fixed_rate_annual=ZERO,
    )
    best = None
    for _ in range(40):
        mid = (low + high) / 2
        probe.percent_of_index = mid
        factor, _, _ = accrual_factor(db, probe, first_day, last_day)
        best = mid
        if factor < target:
            low = mid
        else:
            high = mid
    if best is None:
        return None
    return {
        "percent_of_index": best.quantize(Decimal("0.1")),
        "index_code": (terms.index_code or "CDI").upper(),
        "invested": invested,
        "proceeds": proceeds,
        "total_return_percent": (target - ONE) * Decimal(100),
        "start": first_day,
        "end": last_day,
    }


def accrued_prices(db: Session, portfolio_id: int, quantities: dict[int, Decimal]) -> dict[int, Decimal]:
    """Unit price per asset id for the fixed income positions still held.

    Returns a *price* (value / quantity) so the rest of the app keeps working in
    "quantity x price" terms — a CDB bought as 30.000 units of R$ 1,00 simply
    shows a unit price above par once it has accrued.
    """
    assets = db.scalars(select(Asset).where(Asset.kind.in_(ACCRUED_KINDS))).all()
    prices: dict[int, Decimal] = {}
    for asset in assets:
        quantity = quantities.get(asset.id, ZERO)
        if quantity <= ZERO:
            continue
        terms = db.get(FixedIncomeTerms, asset.id)
        if terms is None:
            continue
        accrual = value_any(db, asset, terms, portfolio_id)
        if accrual is None or accrual.value <= ZERO:
            continue
        prices[asset.id] = accrual.value / quantity
    return prices
