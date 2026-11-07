"""Monthly income tax on sales (apuração mensal de renda variável) and the DARF.

Brazil taxes gains on listed assets month by month, paid by the investor
through a DARF (code 6015) by the last business day of the following month.
This module turns the replayed ledger into that worksheet:

* **Operações comuns** (swing trade): ações, units, ETFs, BDRs, options,
  rights — 15 % on the month's net gain. Gains on *ações* are exempt in a month
  whose ações sales total R$ 20.000 or less; ETFs, BDRs and the rest never are.
* **FIIs** (and Fiagros): 20 %, no exemption.
* **Day trade**: 20 %. The B3 Movimentação export does not mark day trades —
  the custody movements of a same-day round trip net out — so the result is
  *entered by the user* from the broker's report; same-day buy+sell pairs that
  do show up are flagged for review.

Losses carry forward without expiry, each in its own pool: common operations
offset only common gains, FIIs only FIIs, day trade only day trade. A loss in
an exempt month still counts. The 0,005 % withheld at source on sales
(IRRF "dedo-duro") is estimated and deducted, carrying within the calendar
year; a DARF under R$ 10 is not issued — its tax rolls into the next month.

What this module deliberately leaves out, and says so on the page: assets
abroad (taxed yearly under Lei 14.754/2023, not monthly), criptoativos (their
own rules, and where they are held is not something the ledger knows), and
fixed income (taxed at source).

Like the IRPF page this is a worksheet to check, never a filing: realised
results come from the ledger's average cost, and the B3 export carries no
brokerage fees, so gains can read slightly high.

The arithmetic lives in :func:`compute`, which takes plain monthly figures —
every rule above is unit-tested without a database. :func:`monthly_taxes`
gathers those figures from the ledger.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from app.core.dates import local_today
from app.db.models import AppSetting, Asset, Transaction
from app.domain.enums import AssetKind, PositionEffect

ZERO = Decimal(0)
CENT = Decimal("0.01")

#: Monthly ações sales at or below this make the month's ações gains exempt
#: (Lei 11.033/2004, art. 3º, I).
EXEMPTION_LIMIT = Decimal(20_000)
RATE_COMMON = Decimal("0.15")
RATE_FII = Decimal("0.20")
RATE_DAY_TRADE = Decimal("0.20")
#: IRRF on swing-trade sales; brokers withhold only when it reaches R$ 1.
IRRF_RATE = Decimal("0.00005")
IRRF_MINIMUM = Decimal(1)
#: A DARF below this is not paid; the amount joins the next month's.
DARF_MINIMUM = Decimal(10)
DARF_CODE = "6015"

#: Classes whose gains the R$ 20.000 exemption covers.
EXEMPT_ELIGIBLE = {AssetKind.STOCK.value, AssetKind.UNIT.value}
#: Other operações comuns: same pool, same 15 %, never exempt.
COMMON_OTHER = {
    AssetKind.ETF.value,
    AssetKind.BDR.value,
    AssetKind.OPTION.value,
    AssetKind.FUTURE.value,
    AssetKind.SUBSCRIPTION.value,
}
FII_KINDS = {AssetKind.FII.value}
CRYPTO_KINDS = {AssetKind.CRYPTO.value, AssetKind.STABLECOIN.value}

DAY_TRADE_SETTING = "tax_day_trade"
#: Months the user marked as paid — the ledger cannot see a DARF being paid.
PAID_SETTING = "tax_paid"


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Due dates


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 — the algorithm's own name
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def bank_holidays(year: int) -> set[date]:
    """National holidays plus Carnival and Corpus Christi, when banks close."""
    easter = _easter(year)
    fixed = [(1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (12, 25)]
    days = {date(year, month, day) for month, day in fixed}
    if year >= 2024:
        days.add(date(year, 11, 20))  # Consciência Negra, national since Lei 14.759/2023
    days |= {
        easter - timedelta(days=48),  # Carnival Monday
        easter - timedelta(days=47),  # Carnival Tuesday
        easter - timedelta(days=2),  # Good Friday
        easter + timedelta(days=60),  # Corpus Christi
    }
    return days


def darf_due_date(period: str) -> date:
    """Last business day of the month after ``period`` (``YYYY-MM``)."""
    year, month = (int(part) for part in period.split("-"))
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    day = date(year, month, calendar.monthrange(year, month)[1])
    holidays = bank_holidays(year)
    while day.weekday() >= 5 or day in holidays:
        day -= timedelta(days=1)
    return day


# ---------------------------------------------------------------------------
# The arithmetic


@dataclass
class MonthFigures:
    """One month's raw figures, in the portfolio's currency."""

    period: str  # YYYY-MM
    sales_exempt_eligible: Decimal = ZERO  # ações + units sold
    sales_common_other: Decimal = ZERO  # ETFs, BDRs, ... sold
    sales_fii: Decimal = ZERO
    result_exempt_eligible: Decimal = ZERO  # realised on ações + units
    result_common_other: Decimal = ZERO
    result_fii: Decimal = ZERO
    day_trade_result: Decimal | None = None  # user-entered; None = not informed
    day_trade_irrf: Decimal = ZERO


@dataclass
class _Pools:
    common: Decimal = ZERO
    fii: Decimal = ZERO
    day_trade: Decimal = ZERO
    small_darf: Decimal = ZERO  # tax under the DARF minimum, rolling forward
    irrf_credit: Decimal = ZERO  # IRRF not yet used this calendar year
    year: int | None = None


def _apply_pool(result: Decimal, pool: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """(taxable base, loss used, pool after) for one regime's monthly result."""
    if result <= ZERO:
        return ZERO, ZERO, pool - result
    used = min(pool, result)
    return result - used, used, pool - used


def _rounded(figures: MonthFigures) -> MonthFigures:
    """The month's figures to the cent, as they are written on a DARF.

    Average costs carry repeating decimals (R$ 4.190,476190…); rounding each
    month's totals once, before any rule applies, keeps every derived figure —
    bases, losses carried, tax — in whole cents.
    """
    return MonthFigures(
        period=figures.period,
        sales_exempt_eligible=_money(figures.sales_exempt_eligible),
        sales_common_other=_money(figures.sales_common_other),
        sales_fii=_money(figures.sales_fii),
        result_exempt_eligible=_money(figures.result_exempt_eligible),
        result_common_other=_money(figures.result_common_other),
        result_fii=_money(figures.result_fii),
        day_trade_result=(
            _money(figures.day_trade_result) if figures.day_trade_result is not None else None
        ),
        day_trade_irrf=_money(figures.day_trade_irrf),
    )


def compute(
    months: list[MonthFigures], today: date | None = None, paid: set[str] | None = None
) -> list[dict]:
    """The worksheet, oldest month first. Pure: figures in, figures out."""
    today = today or local_today()
    paid = paid or set()
    pools = _Pools()
    out: list[dict] = []
    for figures in sorted((_rounded(item) for item in months), key=lambda item: item.period):
        year = int(figures.period[:4])
        if pools.year != year:
            # IRRF compensates only within the calendar year; what is left goes
            # to the annual return. Losses and small DARFs carry on.
            pools.irrf_credit = ZERO
            pools.year = year

        # --- operações comuns -------------------------------------------
        exempt = figures.sales_exempt_eligible <= EXEMPTION_LIMIT
        exempt_gain = (
            figures.result_exempt_eligible
            if exempt and figures.result_exempt_eligible > ZERO
            else ZERO
        )
        common_result = figures.result_exempt_eligible - exempt_gain + figures.result_common_other
        common_base, common_used, pools.common = _apply_pool(common_result, pools.common)
        tax_common = common_base * RATE_COMMON

        # --- FIIs -----------------------------------------------------------
        fii_base, fii_used, pools.fii = _apply_pool(figures.result_fii, pools.fii)
        tax_fii = fii_base * RATE_FII

        # --- day trade (user-entered) ---------------------------------------
        dt_result = figures.day_trade_result if figures.day_trade_result is not None else ZERO
        dt_base, dt_used, pools.day_trade = _apply_pool(dt_result, pools.day_trade)
        tax_dt = dt_base * RATE_DAY_TRADE

        # --- IRRF -------------------------------------------------------------
        total_sales = (
            figures.sales_exempt_eligible + figures.sales_common_other + figures.sales_fii
        )
        irrf_estimated = total_sales * IRRF_RATE
        if irrf_estimated < IRRF_MINIMUM:
            irrf_estimated = ZERO
        irrf_estimated = _money(irrf_estimated)
        pools.irrf_credit += irrf_estimated + figures.day_trade_irrf

        gross_tax = _money(tax_common + tax_fii + tax_dt)
        irrf_used = min(pools.irrf_credit, gross_tax)
        pools.irrf_credit -= irrf_used
        net_tax = gross_tax - irrf_used

        # --- DARF ---------------------------------------------------------------
        carried_in = pools.small_darf
        due_total = net_tax + carried_in
        if due_total < DARF_MINIMUM:
            darf = ZERO
            pools.small_darf = due_total
        else:
            darf = due_total
            pools.small_darf = ZERO

        due_date = darf_due_date(figures.period)
        in_progress = figures.period == f"{today.year}-{today.month:02d}"
        if in_progress:
            status = "em_andamento"
        elif darf == ZERO:
            status = "acumulado" if due_total > ZERO else "sem_imposto"
        elif figures.period in paid:
            status = "pago"
        elif due_date < today:
            status = "vencido"
        else:
            status = "a_pagar"

        out.append(
            {
                "period": figures.period,
                "status": status,
                "sales": {
                    "acoes": figures.sales_exempt_eligible,
                    "outras_comuns": figures.sales_common_other,
                    "fii": figures.sales_fii,
                },
                "results": {
                    "acoes": figures.result_exempt_eligible,
                    "outras_comuns": figures.result_common_other,
                    "fii": figures.result_fii,
                    "day_trade": figures.day_trade_result,
                },
                "exempt": exempt,
                "exempt_gain": exempt_gain,
                "common": {"base": common_base, "loss_used": common_used,
                           "tax": _money(tax_common)},
                "fii": {"base": fii_base, "loss_used": fii_used, "tax": _money(tax_fii)},
                "day_trade": {"informed": figures.day_trade_result is not None,
                              "base": dt_base, "loss_used": dt_used, "tax": _money(tax_dt),
                              "irrf": figures.day_trade_irrf},
                "irrf_estimated": irrf_estimated,
                "irrf_used": irrf_used,
                "gross_tax": gross_tax,
                "carried_in": carried_in,
                "darf": _money(darf),
                "due_date": due_date,
                "losses_after": {"common": pools.common, "fii": pools.fii,
                                 "day_trade": pools.day_trade},
            }
        )
    return out


# ---------------------------------------------------------------------------
# From the ledger


def _bucket(asset: Asset, base_currency: str) -> str | None:
    """Which regime a sale answers to, or why it is not on this page."""
    if asset.is_cash_account:
        return None
    if asset.kind in CRYPTO_KINDS:
        return "cripto"
    if (asset.currency or base_currency).upper() != base_currency.upper():
        return "exterior"
    if asset.kind in EXEMPT_ELIGIBLE:
        return "acoes"
    if asset.kind in COMMON_OTHER:
        return "outras_comuns"
    if asset.kind in FII_KINDS:
        return "fii"
    return None  # renda fixa, Tesouro, cash: taxed at source


def _day_trade_key(portfolio_id: int) -> str:
    return f"{DAY_TRADE_SETTING}:{portfolio_id}"


def paid_periods(db, portfolio_id: int) -> set[str]:
    row = db.get(AppSetting, f"{PAID_SETTING}:{portfolio_id}")
    raw = (row.value or {}).get("value") if row is not None else None
    return {str(period) for period in (raw or [])}


def set_paid(db, portfolio_id: int, period: str, paid: bool) -> None:
    current = paid_periods(db, portfolio_id)
    if paid:
        current.add(period)
    else:
        current.discard(period)
    db.merge(AppSetting(key=f"{PAID_SETTING}:{portfolio_id}", value={"value": sorted(current)}))


def day_trade_entries(db, portfolio_id: int) -> dict[str, dict[str, Decimal]]:
    row = db.get(AppSetting, _day_trade_key(portfolio_id))
    raw = (row.value or {}).get("value") if row is not None else None
    entries: dict[str, dict[str, Decimal]] = {}
    for period, entry in (raw or {}).items():
        try:
            entries[period] = {
                "result": Decimal(str(entry["result"])),
                "irrf": Decimal(str(entry.get("irrf") or 0)),
            }
        except (KeyError, ArithmeticError, ValueError, TypeError):
            continue
    return entries


def save_day_trade(db, portfolio_id: int, period: str, result: Decimal | None,
                   irrf: Decimal = ZERO) -> None:
    """Store (or, with ``result=None``, remove) one month's day-trade figures.

    Kept as strings: money never round-trips through float.
    """
    key = _day_trade_key(portfolio_id)
    row = db.get(AppSetting, key)
    current = dict(((row.value or {}).get("value") or {}) if row is not None else {})
    if result is None:
        current.pop(period, None)
    else:
        current[period] = {"result": str(_money(result)), "irrf": str(_money(irrf))}
    db.merge(AppSetting(key=key, value={"value": current}))


@dataclass
class _Ledger:
    months: dict[str, MonthFigures] = field(default_factory=dict)
    excluded_sales: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    sales_without_costs: int = 0
    same_day_round_trips: list[dict] = field(default_factory=list)


def _month(ledger: _Ledger, period: str) -> MonthFigures:
    if period not in ledger.months:
        ledger.months[period] = MonthFigures(period=period)
    return ledger.months[period]


def _gather(service) -> _Ledger:
    assets = service.assets()
    base = service.base_currency
    ledger = _Ledger()

    # Sales per month and regime, from the disposals themselves.
    rows = service.db.execute(
        select(
            Transaction.trade_date,
            Transaction.asset_id,
            Transaction.effect,
            Transaction.quantity,
            Transaction.gross_amount,
            Transaction.fx_rate,
            Transaction.fees,
            Transaction.taxes,
        )
        .where(
            Transaction.portfolio_id == service.portfolio_id,
            Transaction.effect.in_(
                [PositionEffect.DISPOSE.value, PositionEffect.ACQUIRE.value]
            ),
        )
        .order_by(Transaction.trade_date)
    ).all()
    bought_on: dict[tuple[date, int], Decimal] = defaultdict(Decimal)
    sold_on: dict[tuple[date, int], Decimal] = defaultdict(Decimal)
    for day, asset_id, effect, quantity, amount, rate, fees, taxes in rows:
        asset = assets.get(asset_id)
        if asset is None:
            continue
        bucket = _bucket(asset, base)
        if bucket is None:
            continue
        if effect == PositionEffect.ACQUIRE.value:
            bought_on[(day, asset_id)] += Decimal(quantity or 0)
            continue
        sold_on[(day, asset_id)] += Decimal(quantity or 0)
        value = Decimal(amount or 0) * Decimal(rate or 1)
        period = f"{day.year}-{day.month:02d}"
        if bucket in ("cripto", "exterior"):
            excluded = ledger.excluded_sales.setdefault(period, defaultdict(Decimal))
            excluded[bucket] += value
            continue
        figures = _month(ledger, period)
        if bucket == "acoes":
            figures.sales_exempt_eligible += value
        elif bucket == "outras_comuns":
            figures.sales_common_other += value
        else:
            figures.sales_fii += value
        if not Decimal(fees or 0) and not Decimal(taxes or 0):
            ledger.sales_without_costs += 1

    # Same-day buy and sell of one asset: possibly a day trade, which the
    # export cannot confirm. Flagged, never reclassified.
    for (day, asset_id), sold in sorted(sold_on.items()):
        bought = bought_on.get((day, asset_id))
        if bought and sold:
            ledger.same_day_round_trips.append(
                {"date": day, "ticker": assets[asset_id].ticker,
                 "bought": bought, "sold": sold}
            )

    # Realised results per month and regime: the change in each asset's
    # cumulative realised result between month ends.
    last_point_of_month: dict[str, object] = {}
    for point in service.timeline():
        last_point_of_month[f"{point.day.year}-{point.day.month:02d}"] = point
    previous: dict[int, Decimal] = {}
    for period in sorted(last_point_of_month):
        current = last_point_of_month[period].realized_by_asset
        for asset_id in set(current) | set(previous):
            moved = current.get(asset_id, ZERO) - previous.get(asset_id, ZERO)
            if not moved:
                continue
            asset = assets.get(asset_id)
            bucket = _bucket(asset, base) if asset is not None else None
            if bucket not in ("acoes", "outras_comuns", "fii"):
                continue
            figures = _month(ledger, period)
            if bucket == "acoes":
                figures.result_exempt_eligible += moved
            elif bucket == "outras_comuns":
                figures.result_common_other += moved
            else:
                figures.result_fii += moved
        previous = dict(current)
    return ledger


def monthly_taxes(service, today: date | None = None) -> dict:
    """Everything the Impostos page shows."""
    today = today or local_today()
    ledger = _gather(service)
    entries = day_trade_entries(service.db, service.portfolio_id)
    for period, entry in entries.items():
        figures = _month(ledger, period)
        figures.day_trade_result = entry["result"]
        figures.day_trade_irrf = entry["irrf"]
    # The month in progress always shows, so its running sales are visible
    # against the exemption before the month closes.
    _month(ledger, f"{today.year}-{today.month:02d}")

    months = compute(
        list(ledger.months.values()), today, paid_periods(service.db, service.portfolio_id)
    )
    warnings: list[dict] = []
    if ledger.same_day_round_trips:
        warnings.append({
            "kind": "possible_day_trade",
            "message": (
                "Há dias em que o mesmo ativo foi comprado e vendido. Se foram day trades, "
                "o resultado deles deve ser informado à parte (20 %) e não entra no preço "
                "médio — confira na nota de corretagem."
            ),
            "items": [_jsonable_trip(trip) for trip in ledger.same_day_round_trips[-20:]],
        })
    if ledger.sales_without_costs:
        warnings.append({
            "kind": "missing_costs",
            "message": (
                f"{ledger.sales_without_costs} venda(s) sem corretagem/emolumentos no extrato. "
                "O extrato da B3 não traz esses custos, que reduzem o lucro: o imposto aqui "
                "pode estar levemente acima do devido."
            ),
        })
    excluded = [
        {"period": period, **{key: value for key, value in sorted(buckets.items())}}
        for period, buckets in sorted(ledger.excluded_sales.items(), reverse=True)
    ]
    return {
        "months": list(reversed(months)),
        "warnings": warnings,
        "excluded_sales": excluded,
        "rules": {
            "exemption_limit": EXEMPTION_LIMIT,
            "rate_common": RATE_COMMON,
            "rate_fii": RATE_FII,
            "rate_day_trade": RATE_DAY_TRADE,
            "irrf_rate": IRRF_RATE,
            "darf_minimum": DARF_MINIMUM,
            "darf_code": DARF_CODE,
        },
    }


def _jsonable_trip(trip: dict) -> dict:
    return {**trip, "date": trip["date"].isoformat()}
