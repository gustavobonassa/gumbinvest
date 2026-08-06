"""Database-facing portfolio analytics built on top of the pure engine."""
from __future__ import annotations

import hashlib
import threading
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta

from app.core.dates import local_today
from decimal import Decimal

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Asset, PortfolioSnapshot, PriceHistory, Quote, Transaction
from app.domain.enums import INCOME_TYPES, AssetKind, OperationType, PositionEffect
from app.portfolio.engine import (
    ZERO,
    Movement,
    Position,
    Succession,
    build_positions,
    build_timeline,
)

logger = get_logger(__name__)

#: Cross-request cache of the replayed ledger, keyed by portfolio id. The
#: replay is a pure function of transactions, successions and FX rates, and a
#: dashboard load fires half a dozen endpoints that each need it — without
#: this every request re-reads and re-replays the whole ledger. The
#: fingerprint hashes cheap aggregates over those tables, so an import, a
#: declared succession or an FX sync invalidates it on the next request.
#: In-process only: the boot-time reclassification passes rewrite rows in
#: place, but they run before the first request and a restart starts empty.
#: Consumers must treat cached movements/positions as read-only (they do — all
#: mutation happens inside the engine while building).
_REPLAY_CACHE: dict[int, tuple[str, dict[str, object]]] = {}
_REPLAY_LOCK = threading.Lock()


def clear_replay_cache() -> None:
    """Drop every cached replay (tests; import paths rely on the fingerprint)."""
    with _REPLAY_LOCK:
        _REPLAY_CACHE.clear()


INCOME_TYPE_VALUES = {t.value for t in INCOME_TYPES}
#: Bumped whenever the return arithmetic changes, so every stored chain is
#: recomputed instead of silently serving figures from the previous formula.
CHAIN_VERSION = 3
#: Instrument families excluded from the "missing quote" warning because no
#: public API marks them to market (fixed income is accrued instead — see
#: app.market.fixed_income). Subscription rights and receipts are here because
#: they are transient by nature: they convert into the underlying or expire, and
#: B3 leaves the converted line open at zero cost. Quoting them would be
#: meaningless even where a ticker exists.
NON_MARKET_KINDS = {
    AssetKind.FUTURE.value,
    AssetKind.OPTION.value,
    AssetKind.FIXED_INCOME.value,
    AssetKind.SUBSCRIPTION.value,
}

#: Asset classes the by-class charts plot as their own line, mirroring
#: ``KIND_ORDER`` in ``frontend/src/lib/colors.ts`` — the eight slots of the
#: validated chart palette. Everything else is aggregated into
#: ``AssetKind.OTHER`` *here*, before anything is computed, because a return
#: cannot be merged after the fact: percentages do not add up, so the bucket has
#: to exist while its flows and values are still being chained.
CHART_KINDS = frozenset(
    {
        AssetKind.STOCK.value,
        AssetKind.STOCK_INTL.value,
        AssetKind.FII.value,
        AssetKind.REIT.value,
        AssetKind.ETF.value,
        AssetKind.ETF_INTL.value,
        AssetKind.FIXED_INCOME.value,
        AssetKind.TREASURY.value,
    }
)


def chart_kind(kind: str | None) -> str:
    return kind if kind in CHART_KINDS else AssetKind.OTHER.value


#: Trailing windows the performance ranking can be measured over. "day" is not
#: here: a single session is read off the quote's own previous close rather than
#: from stored history, which is what makes it right on a Monday (and on any
#: weekend, when the last session is Friday's).
WINDOW_DAYS = {"1m": 30, "3m": 91, "6m": 182, "1y": 365}


def _flow_sign(effect: str) -> Decimal:
    """+1 when the movement brought cash in, -1 when it took cash out."""
    if effect in (
        PositionEffect.DISPOSE.value,
        PositionEffect.CASH_IN.value,
        PositionEffect.RETURN_OF_CAPITAL.value,
    ):
        return Decimal(1)
    if effect in (PositionEffect.ACQUIRE.value, PositionEffect.CASH_OUT.value):
        return Decimal(-1)
    return ZERO


def base_amount(column):
    """SQL expression converting a stored amount to the base currency.

    Uses the rate captured on the transaction, so an aggregate over years of
    dollar movements adds up the reais each one actually represented.
    """
    return column * func.coalesce(Transaction.fx_rate, 1)


def d(value: Decimal | float | int | None) -> Decimal:
    if value is None:
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def period_bucket(column, granularity: str):
    """Group a date column by month ("2024-03") or year ("2024").

    Uses ``substr`` on the ISO text form instead of ``to_char`` so the same SQL
    runs on PostgreSQL and on SQLite (used by the test suite).
    """
    return func.substr(cast(column, String), 1, 7 if granularity == "month" else 4)


#: Below this, an amount is float dust rather than money. Money is stored at six
#: decimals, so a position that closed out leaves residues like 2E-16 behind —
#: enough to be truthy, and enough to turn a percentage into 1.4e21 %.
MONEY_EPSILON = Decimal("0.01")
#: Neutral factor for chained returns — a period that changed nothing.
ONE = Decimal(1)
#: A single day's result may not exceed this share of the capital that could
#: have earned it. Above it, the money did not come from the market — see
#: :meth:`PortfolioService._period_return`.
IMPLAUSIBLE_DAILY_GAIN = Decimal("0.5")


def pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    """A percentage, or zero when there is nothing meaningful to divide by.

    The dust check is the point: ``if not denominator`` only catches an exact
    zero, and a closed position almost never has one. Dividing a real result by
    a residue of 2E-16 produces a number that is arithmetically correct, wildly
    wrong, and impossible to read as anything but a bug.
    """
    if abs(denominator) <= MONEY_EPSILON:
        return ZERO
    return (numerator / denominator) * Decimal(100)


def _split_across(amount: Decimal, mix: dict[str, Decimal] | None) -> list[tuple[str, Decimal]]:
    """Spread ``amount`` over ``mix`` in proportion to its weights.

    Withholding is reported as its own movement and names no payment type, so
    to appear on the "by type" axis it has to be attributed back onto the income
    it was taken out of: the same asset, the same period. Offshore income is
    entirely dividends, so in practice this lands the whole amount on
    ``DIVIDEND`` — the proportional split only matters for an asset that paid
    two kinds of income in one period.

    The remainder is given to the last bucket so the parts always add back up to
    ``amount`` exactly; a rounding crumb left behind would break the invariant
    that a breakdown sums to its total.
    """
    weights = {key: value for key, value in (mix or {}).items() if value > ZERO}
    if not weights:
        # Tax with no income to attribute it to — a refund arriving long after
        # the position was sold. Dividends are the only thing ever withheld on.
        return [(OperationType.DIVIDEND.value, amount)]
    if len(weights) == 1:
        return [(next(iter(weights)), amount)]

    total = sum(weights.values(), ZERO)
    parts: list[tuple[str, Decimal]] = []
    allocated = ZERO
    keys = sorted(weights)
    for key in keys[:-1]:
        portion = (amount * weights[key] / total).quantize(Decimal("0.000001"))
        parts.append((key, portion))
        allocated += portion
    parts.append((keys[-1], amount - allocated))
    return parts


def _subtract(gross: dict[str, Decimal], tax: dict[str, Decimal] | None) -> dict[str, Decimal]:
    """``gross`` minus ``tax``, key by key.

    Keys present only in ``tax`` are kept: a period whose only movement was a
    withholding refund is negative income for that bucket, and dropping it would
    stop the net breakdown adding up to the net total.
    """
    if not tax:
        return dict(gross)
    return {
        key: gross.get(key, ZERO) - tax.get(key, ZERO) for key in set(gross) | set(tax)
    }


@dataclass(slots=True)
class AssetPosition:
    """A position enriched with asset metadata and market data.

    Everything here is in the **asset's own currency** — a US holding reports
    the dollars it was bought with, which is what the asset page shows. The
    ``*_base`` fields carry the same figures converted to the portfolio's
    currency, so that totals across a mixed portfolio can be added up.

    The two conversions are deliberately different: the cost basis uses the rate
    of each purchase's own trade date (``base_position``, replayed from
    converted movements), while the market value uses today's rate. That is the
    honest split — what it cost then, what it is worth now — and the difference
    between them is the currency gain, which would disappear if both used the
    same rate.
    """

    asset: Asset
    position: Position
    price: Decimal | None
    price_source: str | None
    previous_close: Decimal | None
    change_percent: Decimal | None
    quote_time: object | None = None
    #: The same position replayed from base-currency movements.
    base_position: Position | None = None
    #: Today's rate from the asset's currency to the portfolio's. ``None`` when
    #: the asset is foreign and no rate has been downloaded yet.
    fx_rate: Decimal | None = None
    base_currency: str = "BRL"

    @property
    def has_market_price(self) -> bool:
        return self.price is not None and self.price > ZERO

    @property
    def effective_price(self) -> Decimal:
        """Live price when known, otherwise the average cost (no fake P&L)."""
        if self.has_market_price:
            return d(self.price)
        return self.position.average_price

    @property
    def market_value(self) -> Decimal:
        return self.position.quantity * self.effective_price

    @property
    def unrealized(self) -> Decimal:
        return self.market_value - self.position.cost_basis

    @property
    def is_foreign(self) -> bool:
        return (self.asset.currency or self.base_currency).upper() != self.base_currency.upper()

    @property
    def is_convertible(self) -> bool:
        """Whether this position can be expressed in the base currency at all."""
        return not self.is_foreign or self.fx_rate is not None

    @property
    def rate(self) -> Decimal:
        """Rate to the base currency; zero when a foreign asset has none.

        Zero rather than one on purpose. Falling back to one would quietly add
        dollars into a total of reais — a number that looks right and is wrong
        by a factor of five. Zero leaves the position out of the total and it is
        listed in ``overview()["unconverted_positions"]`` so it is visible.
        """
        if not self.is_foreign:
            return Decimal(1)
        return d(self.fx_rate) if self.fx_rate is not None else ZERO

    @property
    def market_value_base(self) -> Decimal:
        """Market value in the portfolio's currency, at today's rate."""
        return self.market_value * self.rate

    @property
    def cost_basis_base(self) -> Decimal:
        """Cost in the portfolio's currency, at each purchase's own rate."""
        if self.base_position is not None:
            return self.base_position.cost_basis
        return self.position.cost_basis * self.rate

    @property
    def unrealized_base(self) -> Decimal:
        return self.market_value_base - self.cost_basis_base

    @property
    def realized_base(self) -> Decimal:
        if self.base_position is not None:
            return self.base_position.realized_pnl
        return self.position.realized_pnl * self.rate

    @property
    def income_base(self) -> Decimal:
        """Income in the portfolio's currency, **net of tax withheld at source**.

        What reached the account is what a result is made of: R$ 100 of JCP with
        R$ 15 withheld made the portfolio R$ 85, and the Proventos page has
        always said so. Reporting the gross figure here is what made the
        dashboard and that page disagree.
        """
        if self.base_position is not None:
            return self.base_position.income - self.base_position.income_tax
        return (self.position.income - self.position.income_tax) * self.rate

    @property
    def day_change(self) -> Decimal:
        if not self.has_market_price or self.previous_close in (None, ZERO):
            return ZERO
        return (d(self.price) - d(self.previous_close)) * self.position.quantity

    def to_dict(self, total_value: Decimal) -> dict:
        p = self.position
        income_net = p.income - p.income_tax
        total_return = self.unrealized + p.realized_pnl + income_net
        # What the return is a percentage *of*. ``total_return`` spans the whole
        # life of the position — realised results and income included — so the
        # base has to be what was put in over that life, not whatever cost is
        # left today. Dividing a lifetime result by a residual cost is what
        # turns a position that was sold down to dust into a five-figure
        # percentage: the numerator covers years, the denominator covers what
        # is left of the last few units.
        invested_reference = (
            p.total_bought_amount if p.total_bought_amount > MONEY_EPSILON else p.cost_basis
        )
        return {
            "asset_id": self.asset.id,
            "ticker": self.asset.ticker,
            "name": self.asset.name,
            "kind": self.asset.kind,
            "currency": self.asset.currency,
            "quantity": p.quantity,
            "average_price": p.average_price,
            "current_price": self.effective_price,
            "has_market_price": self.has_market_price,
            "price_source": self.price_source,
            "cost_basis": p.cost_basis,
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized,
            "unrealized_pct": pct(self.unrealized, p.cost_basis),
            "realized_pnl": p.realized_pnl,
            # Cash received for quantity that had no purchase behind it. Kept
            # apart from the realised result rather than folded into it: it is
            # money that arrived, but it is not a gain, and the difference is
            # the whole point.
            "uncosted_proceeds": p.uncosted_proceeds,
            "uncosted_quantity": p.uncosted_quantity,
            # Part of the quantity above that is locked in staking / Simple
            # Earn. Included in the position because it is owned; reported
            # separately because it is not spendable today.
            "staked_quantity": p.staked_quantity,
            # Net of withholding, like every other result figure. The gross
            # amount and the tax travel alongside it, and `income_by_type`
            # breaks down the *gross* — withholding names no payment type.
            "income": income_net,
            "income_gross": p.income,
            "income_withheld": p.income_tax,
            "income_by_type": {k: v for k, v in p.income_by_type.items()},
            "returned_capital": p.returned_capital,
            "total_return": total_return,
            "total_return_pct": pct(total_return, invested_reference),
            "day_change": self.day_change,
            # The same move in the portfolio's currency, so a day's winners can
            # be ranked across a mixed portfolio without comparing dollars with
            # reais. The percentage needs no conversion — it is the same number.
            "day_change_base": self.day_change * self.rate,
            "day_change_pct": self.change_percent,
            # Allocation is a share of the portfolio, so it can only be computed
            # from values that were all converted to the same currency.
            "allocation_pct": pct(self.market_value_base, total_value),
            "transactions": p.transactions,
            "first_trade": p.first_trade,
            "last_trade": p.last_trade,
            "is_open": p.is_open,
            "warnings": p.warnings,
            "notes": p.notes,
            "is_foreign": self.is_foreign,
            "is_convertible": self.is_convertible,
            "fx_rate": self.fx_rate,
            "market_value_base": self.market_value_base,
            "cost_basis_base": self.cost_basis_base,
            "unrealized_pnl_base": self.unrealized_base,
            "realized_pnl_base": self.realized_base,
            "income_base": self.income_base,
            "total_return_base": self.unrealized_base + self.realized_base + self.income_base,
        }


class PortfolioService:
    """All portfolio-level reads. One instance per request."""

    def __init__(self, db: Session, portfolio_id: int) -> None:
        self.db = db
        self.portfolio_id = portfolio_id
        self._movements: list[Movement] | None = None
        self._base_movements: list[Movement] | None = None
        self._positions: dict[int, Position] | None = None
        self._base_positions: dict[int, Position] | None = None
        self._assets: dict[int, Asset] | None = None
        self._quotes: dict[int, Quote] | None = None
        self._accrued: dict[int, Decimal] | None = None
        #: Accrued value per (asset, day) for papers no market quotes — filled
        #: on demand while walking a chart's sampled days.
        self._accrued_on: dict[tuple[int, date], Decimal | None] = {}
        self._accrual_rows: dict[int, list] = {}
        self._successions: list[Succession] | None = None
        self._base_currency: str | None = None
        self._fx: dict[str, object] = {}
        self._prices: dict[int, list[tuple[date, Decimal]]] | None = None
        self._replay_fp: str | None = None

    # -- currency ----------------------------------------------------------
    @property
    def base_currency(self) -> str:
        """The currency the portfolio reports in (BRL for this app)."""
        if self._base_currency is None:
            from app.db.models import Portfolio

            portfolio = self.db.get(Portfolio, self.portfolio_id)
            self._base_currency = (portfolio.base_currency if portfolio else "BRL") or "BRL"
        return self._base_currency

    def fx_table(self, currency: str):
        """Cached PTAX series for converting ``currency`` into the base."""
        key = currency.upper()
        if key not in self._fx:
            from app.market.fx import load_table

            self._fx[key] = load_table(self.db, key, self.base_currency)
        return self._fx[key]

    def spot_rate(self, currency: str) -> Decimal | None:
        """Today's rate — what a foreign position is worth in reais *now*."""
        if currency.upper() == self.base_currency.upper():
            return Decimal(1)
        table = self.fx_table(currency)
        return table.latest if not table.is_empty else None

    # -- data loading ------------------------------------------------------
    def _replay_fingerprint(self) -> str:
        """Cheap aggregates over everything the replay reads (see _REPLAY_CACHE)."""
        if self._replay_fp is None:
            from app.db.models import AssetSuccession, FxRate

            transactions = self.db.execute(
                select(func.count(Transaction.id), func.max(Transaction.id)).where(
                    Transaction.portfolio_id == self.portfolio_id
                )
            ).one()
            successions = self.db.execute(
                select(func.count(AssetSuccession.id), func.max(AssetSuccession.id)).where(
                    AssetSuccession.portfolio_id == self.portfolio_id
                )
            ).one()
            fx = self.db.execute(select(func.count(FxRate.id), func.max(FxRate.date))).one()
            raw = f"replay-1|{self.base_currency}|{transactions}|{successions}|{fx}"
            self._replay_fp = hashlib.sha256(raw.encode()).hexdigest()[:32]
        return self._replay_fp

    def _shared(self, key: str, builder):
        """Cross-request memo for pure replay products (movements, positions)."""
        fp = self._replay_fingerprint()
        with _REPLAY_LOCK:
            cached_fp, values = _REPLAY_CACHE.get(self.portfolio_id, (None, {}))
            if cached_fp == fp and key in values:
                return values[key]
        value = builder()
        with _REPLAY_LOCK:
            cached_fp, values = _REPLAY_CACHE.get(self.portfolio_id, (None, {}))
            if cached_fp != fp:
                values = {}
                _REPLAY_CACHE[self.portfolio_id] = (fp, values)
            values[key] = value
        return value

    def movements(self) -> list[Movement]:
        """Movements in the currency they happened in."""
        if self._movements is None:
            self._movements = self._shared("movements", self._load_movements)
        return self._movements

    def _load_movements(self) -> list[Movement]:
        rows = self.db.execute(
                select(
                    Transaction.id,
                    Transaction.asset_id,
                    Transaction.trade_date,
                    Transaction.op_type,
                    Transaction.effect,
                    Transaction.quantity,
                    Transaction.unit_price,
                    Transaction.gross_amount,
                    Transaction.fees,
                    Transaction.taxes,
                    Transaction.broker_id,
                    Transaction.currency,
                    Transaction.fx_rate,
                ).where(Transaction.portfolio_id == self.portfolio_id)
            ).all()
        return [
                Movement(
                    id=r.id,
                    asset_id=r.asset_id,
                    trade_date=r.trade_date,
                    op_type=r.op_type,
                    effect=r.effect,
                    quantity=d(r.quantity),
                    unit_price=d(r.unit_price),
                    gross_amount=d(r.gross_amount),
                    fees=d(r.fees),
                    taxes=d(r.taxes),
                    broker_id=r.broker_id,
                    currency=r.currency or self.base_currency,
                    fx_rate=d(r.fx_rate) if r.fx_rate is not None else None,
                )
                for r in rows
            ]

    def base_movements(self) -> list[Movement]:
        """The same movements, restated in the portfolio's base currency.

        Each one is converted at the rate that applied on **its own trade
        date**, not today's: a share bought in 2021 cost the dollars paid times
        the rate that day, and that is the number a cost basis in reais has to
        be built from. Quantities are untouched, so replaying these produces the
        same positions with a base-currency cost.
        """
        if self._base_movements is None:
            self._base_movements = self._shared("base_movements", self._convert_movements)
        return self._base_movements

    def _convert_movements(self) -> list[Movement]:
        base = self.base_currency.upper()
        converted: list[Movement] = []
        for movement in self.movements():
            if (movement.currency or base).upper() == base:
                converted.append(movement)
                continue
            rate = movement.fx_rate or self._rate_for(movement)
            if rate is None:
                # No rate available: leave the movement out of the converted
                # replay rather than silently valuing dollars as reais.
                continue
            converted.append(
                replace(
                    movement,
                    unit_price=movement.unit_price * rate,
                    gross_amount=movement.gross_amount * rate,
                    fees=movement.fees * rate,
                    taxes=movement.taxes * rate,
                    currency=base,
                    fx_rate=rate,
                )
            )
        return converted

    def _rate_for(self, movement: Movement) -> Decimal | None:
        table = self.fx_table(movement.currency or self.base_currency)
        return None if table.is_empty else table.rate_on(movement.trade_date)

    def successions(self) -> list[Succession]:
        """Declared corporate actions — B3 never links an asset to its successor."""
        if self._successions is None:
            from app.portfolio.corporate_actions import load_successions  # avoids a cycle

            self._successions = self._shared(
                "successions", lambda: load_successions(self.db, self.portfolio_id)
            )
        return self._successions

    def positions(self) -> dict[int, Position]:
        """Positions in each asset's own currency."""
        if self._positions is None:
            self._positions = self._shared(
                "positions", lambda: build_positions(self.movements(), self.successions())
            )
        return self._positions

    def base_positions(self) -> dict[int, Position]:
        """The same replay, run over base-currency movements.

        Running the engine twice rather than teaching it about currencies keeps
        it a pure function of its movements — which is what makes it testable —
        and costs one extra pass over a few thousand rows.
        """
        if self._base_positions is None:
            self._base_positions = self._shared(
                "base_positions",
                lambda: build_positions(self.base_movements(), self.successions()),
            )
        return self._base_positions

    def assets(self) -> dict[int, Asset]:
        if self._assets is None:
            self._assets = {a.id: a for a in self.db.scalars(select(Asset)).all()}
        return self._assets

    def quotes(self) -> dict[int, Quote]:
        if self._quotes is None:
            self._quotes = {q.asset_id: q for q in self.db.scalars(select(Quote)).all()}
        return self._quotes

    def accrued_prices(self) -> dict[int, Decimal]:
        """Unit prices for fixed income, accrued from the index (CDI/IPCA/pre).

        No public API quotes a private CDB, but the paper tracks a published
        index — so its value is computed rather than fetched.
        """
        if self._accrued is None:
            from app.market.fixed_income import accrued_prices  # local import avoids a cycle

            held = {aid: p.quantity for aid, p in self.positions().items() if p.is_open}
            try:
                self._accrued = accrued_prices(self.db, self.portfolio_id, held)
            except Exception:  # noqa: BLE001 — pricing must never break the portfolio
                logger.exception("fixed-income accrual failed; falling back to cost")
                self._accrued = {}
        return self._accrued

    def accrued_value_on(self, asset_id: int, day: date) -> Decimal | None:
        """What a fixed income position was worth at the end of ``day``.

        A CDB has no close to look up: its value is the principal compounded by
        the index from each application's own date. Charts used to fall back to
        cost for these, which drew the papers as a flat line and made the last
        point of the curve disagree with the headline net worth by exactly the
        interest earned. ``None`` for anything that is not an accruing paper.
        """
        key = (asset_id, day)
        if key in self._accrued_on:
            return self._accrued_on[key]

        from app.market.fixed_income import ACCRUED_KINDS, get_terms, movements, value_any

        asset = self.assets().get(asset_id)
        value: Decimal | None = None
        if asset is not None and asset.kind in ACCRUED_KINDS:
            try:
                terms = get_terms(self.db, asset)
                if terms is not None:
                    # The paper's movements are the same on every day of the
                    # walk; reading them once per asset is what keeps a chart
                    # from issuing one query per day per paper.
                    if asset_id not in self._accrual_rows:
                        self._accrual_rows[asset_id] = movements(self.db, self.portfolio_id, asset_id)
                    accrual = value_any(
                        self.db,
                        asset,
                        terms,
                        self.portfolio_id,
                        through=day,
                        rows=self._accrual_rows[asset_id],
                    )
                    value = accrual.value if accrual is not None else None
            except Exception:  # noqa: BLE001 — pricing must never break a chart
                logger.exception("fixed-income accrual failed for %s on %s", asset_id, day)
                value = None
        self._accrued_on[key] = value
        return value

    # -- positions ---------------------------------------------------------
    def asset_positions(self, include_closed: bool = False) -> list[AssetPosition]:
        assets = self.assets()
        quotes = self.quotes()
        accrued = self.accrued_prices()
        base_positions = self.base_positions()
        result: list[AssetPosition] = []
        for asset_id, position in self.positions().items():
            asset = assets.get(asset_id)
            if asset is None:
                continue
            if not include_closed and not position.is_open:
                continue
            price: Decimal | None = None
            source: str | None = None
            previous: Decimal | None = None
            change: Decimal | None = None
            quote_time = None
            if asset.price_manual and asset.manual_price is not None:
                price, source = d(asset.manual_price), "manual"
            elif asset_id in accrued:
                price, source = d(accrued[asset_id]), "cdi"
            else:
                quote = quotes.get(asset_id)
                if quote is not None and quote.price is not None:
                    price = d(quote.price)
                    source = quote.source
                    previous = d(quote.previous_close) if quote.previous_close is not None else None
                    change = d(quote.change_percent) if quote.change_percent is not None else None
                    quote_time = quote.fetched_at
            result.append(
                AssetPosition(
                    asset=asset,
                    position=position,
                    price=price,
                    price_source=source,
                    previous_close=previous,
                    change_percent=change,
                    quote_time=quote_time,
                    base_position=base_positions.get(asset_id),
                    fx_rate=self.spot_rate(asset.currency or self.base_currency),
                    base_currency=self.base_currency,
                )
            )
        # Sorted by base-currency value: comparing a dollar figure with a real
        # one would put a small US holding above a large Brazilian one.
        result.sort(key=lambda ap: ap.market_value_base, reverse=True)
        return result

    # -- aggregates --------------------------------------------------------
    def overview(self) -> dict:
        """Portfolio totals, always in the portfolio's base currency.

        Net worth has to be one number, so every figure here is converted:
        dollars and reais cannot be added. The per-asset views keep the native
        currency — see :class:`AssetPosition`.
        """
        open_positions = self.asset_positions()
        base_positions = self.base_positions().values()

        market_value = sum((ap.market_value_base for ap in open_positions), ZERO)
        cost_basis = sum((ap.cost_basis_base for ap in open_positions), ZERO)
        unrealized = market_value - cost_basis
        realized = sum((p.realized_pnl for p in base_positions), ZERO)
        # Net of tax withheld at source, so "Proventos recebidos" on the
        # dashboard and the Proventos page quote the same number — and so the
        # headline result does not count money that never arrived.
        income = sum((p.income - p.income_tax for p in base_positions), ZERO)
        returned_capital = sum((p.returned_capital for p in base_positions), ZERO)
        uncosted_proceeds = sum((p.uncosted_proceeds for p in base_positions), ZERO)
        day_change = sum((ap.day_change * ap.rate for ap in open_positions), ZERO)
        priced = [ap for ap in open_positions if ap.has_market_price]
        unpriced = [
            ap
            for ap in open_positions
            if not ap.has_market_price and ap.asset.kind not in NON_MARKET_KINDS
        ]

        # Cash flows are summed in base currency, which is why they are computed
        # from the converted replay rather than with SUM() in SQL: the stored
        # amounts are in whatever currency the movement happened in.
        cash_flow = sum((m.gross_amount * _flow_sign(m.effect) for m in self.base_movements()), ZERO)
        contributions = sum(
            (
                m.net_cost if m.effect == PositionEffect.ACQUIRE.value else -m.net_proceeds
                for m in self.base_movements()
                if m.effect in (PositionEffect.ACQUIRE.value, PositionEffect.DISPOSE.value)
            ),
            ZERO,
        )
        total_profit = unrealized + realized + income
        return {
            "base_currency": self.base_currency,
            "fx_rates": {
                currency: str(rate)
                for currency, rate in self.foreign_rates().items()
            },
            "foreign_value": sum(
                (ap.market_value_base for ap in open_positions if ap.is_foreign), ZERO
            ),
            "market_value": market_value,
            "cost_basis": cost_basis,
            "invested": cost_basis,
            "net_contributed": d(contributions),
            "unrealized_pnl": unrealized,
            "unrealized_pct": pct(unrealized, cost_basis),
            "realized_pnl": realized,
            "income_total": income,
            "returned_capital": returned_capital,
            # Sales of quantity that arrived with no purchase behind it. Real
            # cash, deliberately outside every profit figure above: with no
            # cost to subtract there is no result to report, and counting the
            # proceeds as one is what turns a wallet transfer into a
            # six-figure return.
            "uncosted_proceeds": uncosted_proceeds,
            "uncosted_positions": [
                ap.asset.ticker for ap in open_positions if ap.position.uncosted_quantity > ZERO
            ],
            "total_profit": total_profit,
            "total_profit_pct": pct(total_profit, cost_basis if cost_basis else d(contributions)),
            "day_change": day_change,
            "day_change_pct": pct(day_change, market_value - day_change),
            "cash_balance": d(cash_flow),
            "positions_count": len(open_positions),
            "assets_tracked": len(self.positions()),
            "priced_positions": len(priced),
            "unpriced_positions": [ap.asset.ticker for ap in unpriced],
            # Foreign holdings with no exchange rate yet: left out of every
            # figure above rather than added in at the wrong scale.
            "unconverted_positions": [
                ap.asset.ticker for ap in open_positions if not ap.is_convertible
            ],
            "last_quote_at": max(
                (ap.quote_time for ap in priced if ap.quote_time is not None), default=None
            ),
        }

    def foreign_rates(self) -> dict[str, Decimal]:
        """Today's rate for every non-base currency held."""
        currencies = {
            (asset.currency or self.base_currency).upper()
            for asset in self.assets().values()
        }
        rates: dict[str, Decimal] = {}
        for currency in sorted(currencies):
            if currency == self.base_currency.upper():
                continue
            rate = self.spot_rate(currency)
            if rate is not None:
                rates[currency] = rate
        return rates

    def allocation(self, group_by: str = "asset") -> list[dict]:
        """Allocation always in base currency — the slices must share a unit."""
        positions = self.asset_positions()
        total = sum((ap.market_value_base for ap in positions), ZERO)
        buckets: dict[str, Decimal] = defaultdict(Decimal)
        labels: dict[str, str] = {}

        if group_by == "currency":
            for ap in positions:
                currency = (ap.asset.currency or self.base_currency).upper()
                buckets[currency] += ap.market_value_base
                labels[currency] = currency
        elif group_by == "kind":
            for ap in positions:
                buckets[ap.asset.kind] += ap.market_value_base
                labels[ap.asset.kind] = ap.asset.kind
        elif group_by == "broker":
            rows = self.db.execute(
                select(Transaction.asset_id, Transaction.broker_id, func.count())
                .where(Transaction.portfolio_id == self.portfolio_id)
                .group_by(Transaction.asset_id, Transaction.broker_id)
            ).all()
            from app.db.models import Broker  # local import avoids a cycle at module load

            brokers = {b.id: b.canonical_name for b in self.db.scalars(select(Broker)).all()}
            weight: dict[int, dict[int | None, int]] = defaultdict(lambda: defaultdict(int))
            for asset_id, broker_id, count in rows:
                weight[asset_id][broker_id] += count
            for ap in positions:
                distribution = weight.get(ap.asset.id) or {None: 1}
                total_count = sum(distribution.values()) or 1
                for broker_id, count in distribution.items():
                    name = brokers.get(broker_id, "Desconhecida")
                    buckets[name] += ap.market_value_base * Decimal(count) / Decimal(total_count)
                    labels[name] = name
        else:
            for ap in positions:
                buckets[ap.asset.ticker] += ap.market_value_base
                labels[ap.asset.ticker] = ap.asset.name

        return sorted(
            (
                {
                    "key": key,
                    "label": labels.get(key, key),
                    "value": value,
                    "percent": pct(value, total),
                }
                for key, value in buckets.items()
                if value != ZERO
            ),
            key=lambda item: item["value"],
            reverse=True,
        )

    # -- time series -------------------------------------------------------
    def history(self, start: date | None = None, granularity: str = "auto") -> list[dict]:
        """Portfolio value over time.

        Uses stored daily closes when available and falls back to cost basis
        for assets without price history, so the curve always exists even with
        the market data provider disabled.

        Everything is in the portfolio's base currency: the replay runs over
        converted movements, and a foreign asset's daily close is converted at
        *that day's* rate, so the curve shows the currency swing as it happened
        rather than restating history at today's rate.
        """
        timeline = build_timeline(self.base_movements(), self.successions())
        if not timeline:
            return []

        first_day = timeline[0].day
        last_day = max(timeline[-1].day, local_today())
        start = start or first_day
        prices = self._price_matrix()
        currencies = {
            asset_id: (asset.currency or self.base_currency).upper()
            for asset_id, asset in self.assets().items()
        }

        step = self._resolve_step(start, last_day, granularity)
        points: list[dict] = []
        index = 0
        current = timeline[0]
        day = first_day
        latest_prices = {aid: d(q.price) for aid, q in self.quotes().items() if q.price is not None}

        while day <= last_day:
            while index < len(timeline) and timeline[index].day <= day:
                current = timeline[index]
                index += 1
            if day >= start and (day == last_day or self._is_step_day(day, step)):
                value = ZERO
                for asset_id, qty in current.quantities.items():
                    price = self._price_at(prices, latest_prices, asset_id, day)
                    if price is not None:
                        value += qty * price * self._rate_on(currencies.get(asset_id), day)
                        continue
                    accrued = self.accrued_value_on(asset_id, day)
                    if accrued is not None:
                        value += accrued
                    else:
                        # No quote and nothing to accrue: the cost is already
                        # in base currency.
                        value += current.costs.get(asset_id, ZERO)
                points.append(
                    {
                        "date": day,
                        "market_value": value,
                        "cost_basis": current.cost_basis,
                        "invested": current.invested_flow,
                        "dividends": current.dividends,
                        "realized": current.realized,
                        "profit": value - current.cost_basis,
                    }
                )
            day += timedelta(days=1)
        return points

    def _rate_on(self, currency: str | None, day: date) -> Decimal:
        """Rate from ``currency`` to the base on ``day`` (1 when already base)."""
        if not currency or currency == self.base_currency.upper():
            return Decimal(1)
        table = self.fx_table(currency)
        if table.is_empty:
            return Decimal(1)
        return table.rate_on(day) or Decimal(1)

    def _resolve_step(self, start: date, end: date, granularity: str) -> str:
        if granularity in {"day", "week", "month"}:
            return granularity
        span = (end - start).days
        if span <= 120:
            return "day"
        if span <= 800:
            return "week"
        return "month"

    @staticmethod
    def _is_step_day(day: date, step: str) -> bool:
        if step == "day":
            return True
        if step == "week":
            return day.weekday() == 4  # Fridays
        return (day + timedelta(days=1)).day == 1  # month end

    def _price_matrix(self) -> dict[int, list[tuple[date, Decimal]]]:
        # Only assets this portfolio ever transacted — the table also holds
        # benchmarks and other portfolios' assets, and callers never look
        # those up here.
        if self._prices is None:
            held = list(self.positions().keys())
            rows = self.db.execute(
                select(PriceHistory.asset_id, PriceHistory.date, PriceHistory.close)
                .where(PriceHistory.asset_id.in_(held))
                .order_by(PriceHistory.asset_id, PriceHistory.date)
            ).all()
            matrix: dict[int, list[tuple[date, Decimal]]] = defaultdict(list)
            for asset_id, day, close in rows:
                matrix[asset_id].append((day, d(close)))
            self._prices = matrix
        return self._prices

    @staticmethod
    def _price_at(
        matrix: dict[int, list[tuple[date, Decimal]]],
        latest: dict[int, Decimal],
        asset_id: int,
        day: date,
    ) -> Decimal | None:
        series = matrix.get(asset_id)
        if not series:
            return None
        # Series are short (a few thousand points); a reverse scan is fine and
        # keeps the code obvious. Callers walk days forward, so this is warm.
        lo, hi = 0, len(series) - 1
        best: Decimal | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if series[mid][0] <= day:
                best = series[mid][1]
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def income_series(self, granularity: str = "month") -> list[dict]:
        """Dividends/JCP/yields grouped by month or year."""
        bucket = period_bucket(Transaction.trade_date, granularity)
        rows = self.db.execute(
            select(
                bucket.label("period"),
                Transaction.op_type,
                func.sum(base_amount(Transaction.net_amount)).label("amount"),
            )
            .where(
                Transaction.portfolio_id == self.portfolio_id,
                Transaction.op_type.in_(list(INCOME_TYPE_VALUES)),
            )
            .group_by("period", Transaction.op_type)
            .order_by("period")
        ).all()
        grouped: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for period, op_type, amount in rows:
            grouped[period][op_type] += d(amount)
        series = []
        running = ZERO
        for period in sorted(grouped):
            values = grouped[period]
            total = sum(values.values(), ZERO)
            running += total
            series.append(
                {
                    "period": period,
                    "total": total,
                    "cumulative": running,
                    **{k: v for k, v in values.items()},
                }
            )
        return series

    # -- dividends ---------------------------------------------------------
    def _income_rows(self) -> list[tuple[date, int, str, Decimal]]:
        """Every income payment: (day, asset_id, op_type, amount).

        Amounts are converted to the base currency at the rate of the day they
        were paid. A dividend page adds payments up across assets, so it can
        only work in one currency — leaving a US$ 50 dividend unconverted next
        to a R$ 50 one understates it fivefold and quietly flatters every
        Brazilian payer in the ranking.
        """
        rows = self.db.execute(
            select(
                Transaction.trade_date,
                Transaction.asset_id,
                Transaction.op_type,
                base_amount(Transaction.net_amount),
            )
            .where(
                Transaction.portfolio_id == self.portfolio_id,
                Transaction.op_type.in_(list(INCOME_TYPE_VALUES)),
            )
            .order_by(Transaction.trade_date)
        ).all()
        return [(day, asset_id, op_type, d(amount)) for day, asset_id, op_type, amount in rows]

    def _income_cost_rows(self) -> list[tuple[date, int, str, Decimal]]:
        """Tax withheld and fees charged: (day, asset_id, op_type, amount).

        Amounts are positive when money was taken and negative when it came
        back — a withholding refund ("NRA ADJ", sometimes years later) is a
        genuine reduction of tax paid, not extra income.
        """
        rows = self.db.execute(
            select(
                Transaction.trade_date,
                Transaction.asset_id,
                Transaction.op_type,
                base_amount(Transaction.net_amount),
            ).where(
                Transaction.portfolio_id == self.portfolio_id,
                Transaction.op_type.in_(
                    [OperationType.TAX.value, OperationType.FEE.value]
                ),
            )
        ).all()
        # `net_amount` is negative when cash left, so the sign is flipped to
        # read as "amount withheld".
        return [(day, asset_id, op_type, -d(amount)) for day, asset_id, op_type, amount in rows]

    @staticmethod
    def _period_key(day: date, granularity: str) -> str:
        if granularity == "year":
            return f"{day.year}"
        if granularity == "quarter":
            return f"{day.year}-Q{(day.month - 1) // 3 + 1}"
        return f"{day.year}-{day.month:02d}"

    def dividends(self, granularity: str = "month") -> dict:
        """Everything the income page shows, from one pass over the payments.

        Grouping happens in Python rather than in SQL: quarters have no portable
        SQL expression, the volume is a few thousand rows, and one pass feeds the
        period series, the per-class split and the per-asset ranking at once.
        """
        rows = self._income_rows()
        assets = self.assets()
        # Base-currency positions: the income above is converted, so the cost it
        # is divided by has to be too, or a US holding's yield on cost comes out
        # five times too high.
        positions = self.base_positions()

        # Two independent axes, both carried per period so the chart can switch
        # between them without another round trip. They answer different
        # questions — "what kind of payment was it" and "what paid it" — and
        # merging them into one breakdown makes neither readable.
        types_by_period: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        kinds_by_period: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        by_kind: dict[str, Decimal] = defaultdict(Decimal)
        by_type: dict[str, Decimal] = defaultdict(Decimal)
        by_asset: dict[int, dict] = {}
        by_month: dict[str, Decimal] = defaultdict(Decimal)
        # Which types an asset was paid in, per period and overall: withholding
        # names no payment type of its own, so it is attributed back onto the
        # income it was taken from.
        mix_by_asset_period: dict[tuple[int, str], dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(Decimal)
        )
        mix_by_asset: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

        today = local_today()
        last_12m_start = date(today.year - 1, today.month, 1)
        totals = {"all_time": ZERO, "this_year": ZERO, "last_12m": ZERO}

        for day, asset_id, op_type, amount in rows:
            asset = assets.get(asset_id)
            kind = asset.kind if asset else AssetKind.OTHER.value
            period = self._period_key(day, granularity)
            types_by_period[period][op_type] += amount
            kinds_by_period[period][kind] += amount
            by_month[self._period_key(day, "month")] += amount
            by_kind[kind] += amount
            by_type[op_type] += amount
            mix_by_asset_period[(asset_id, period)][op_type] += amount
            mix_by_asset[asset_id][op_type] += amount

            entry = by_asset.get(asset_id)
            if entry is None:
                entry = by_asset[asset_id] = {
                    "ticker": asset.ticker if asset else str(asset_id),
                    "name": asset.name if asset else "",
                    "kind": kind,
                    "total": ZERO,
                    "payments": 0,
                    "first": day,
                    "last": day,
                }
            entry["total"] += amount
            entry["payments"] += 1
            entry["last"] = day

            totals["all_time"] += amount
            if day.year == today.year:
                totals["this_year"] += amount
            if day >= last_12m_start:
                totals["last_12m"] += amount

        # Withholding, attributed onto the same axes as the income it came out
        # of, so every breakdown has an exact net counterpart.
        tax_by_period: dict[str, Decimal] = defaultdict(Decimal)
        tax_by_asset: dict[int, Decimal] = defaultdict(Decimal)
        tax_by_month: dict[str, Decimal] = defaultdict(Decimal)
        tax_by_kind: dict[str, Decimal] = defaultdict(Decimal)
        tax_by_type: dict[str, Decimal] = defaultdict(Decimal)
        tax_types_by_period: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        tax_kinds_by_period: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        tax_totals = {"all_time": ZERO, "this_year": ZERO, "last_12m": ZERO}
        # Trading commissions are tracked but never netted off income: a
        # brokerage charge is a cost of buying, not a deduction from a dividend.
        costs = ZERO

        for day, asset_id, op_type, amount in self._income_cost_rows():
            if op_type == OperationType.FEE.value:
                costs += amount
                continue
            period = self._period_key(day, granularity)
            asset = assets.get(asset_id)
            kind = asset.kind if asset else AssetKind.OTHER.value

            tax_by_period[period] += amount
            tax_by_asset[asset_id] += amount
            tax_by_month[self._period_key(day, "month")] += amount
            tax_by_kind[kind] += amount
            tax_kinds_by_period[period][kind] += amount
            for income_type, portion in _split_across(
                amount, mix_by_asset_period.get((asset_id, period)) or mix_by_asset.get(asset_id)
            ):
                tax_by_type[income_type] += portion
                tax_types_by_period[period][income_type] += portion

            tax_totals["all_time"] += amount
            if day.year == today.year:
                tax_totals["this_year"] += amount
            if day >= last_12m_start:
                tax_totals["last_12m"] += amount

        net_by_month = {
            month: by_month.get(month, ZERO) - tax_by_month.get(month, ZERO)
            for month in set(by_month) | set(tax_by_month)
        }

        series = []
        running = ZERO
        # Periods that had only a tax movement still belong on the chart: a
        # month of pure withholding refunds is real income.
        for period in sorted(set(types_by_period) | set(tax_by_period)):
            types = types_by_period[period]
            total = sum(types.values(), ZERO)
            tax = tax_by_period.get(period, ZERO)
            running += total
            series.append(
                {
                    "period": period,
                    "total": total,
                    "tax": tax,
                    "net": total - tax,
                    "cumulative": running,
                    # Nested rather than spread across the row: the two axes are
                    # separate namespaces, and nesting keeps them from ever
                    # colliding as new kinds and operations are added.
                    "types": dict(types),
                    "kinds": dict(kinds_by_period[period]),
                    # The same two axes with withholding taken off. Both are
                    # sent so the page can show what reached the account
                    # without losing the gross figure the tables report.
                    "types_net": _subtract(types, tax_types_by_period.get(period)),
                    "kinds_net": _subtract(
                        kinds_by_period[period], tax_kinds_by_period.get(period)
                    ),
                }
            )

        # Yield on cost answers the question the raw total cannot: what the
        # capital still invested is paying. It is per-asset because a sold
        # position has no cost left to divide by.
        ranked = []
        for asset_id, entry in by_asset.items():
            position = positions.get(asset_id)
            cost = position.cost_basis if position else ZERO
            tax = tax_by_asset.get(asset_id, ZERO)
            net = entry["total"] - tax
            ranked.append(
                {
                    **entry,
                    "tax": tax,
                    "net": net,
                    "cost_basis": cost,
                    "yield_on_cost": pct(entry["total"], cost) if cost else None,
                    "yield_on_cost_net": pct(net, cost) if cost else None,
                    "share": pct(net, totals["all_time"] - tax_totals["all_time"]),
                }
            )
        ranked.sort(key=lambda item: item["net"], reverse=True)

        months_with_income = [value for value in net_by_month.values() if value]
        held_cost = sum((p.cost_basis for p in positions.values() if p.is_open), ZERO)

        return {
            "granularity": granularity,
            "series": series,
            "by_kind": sorted(
                (
                    {
                        "kind": kind,
                        "total": total,
                        "tax": tax_by_kind.get(kind, ZERO),
                        "net": total - tax_by_kind.get(kind, ZERO),
                        "share": pct(
                            total - tax_by_kind.get(kind, ZERO),
                            totals["all_time"] - tax_totals["all_time"],
                        ),
                    }
                    for kind, total in by_kind.items()
                ),
                key=lambda item: item["net"],
                reverse=True,
            ),
            "by_type": sorted(
                (
                    {
                        "op_type": op_type,
                        "total": total,
                        "tax": tax_by_type.get(op_type, ZERO),
                        "net": total - tax_by_type.get(op_type, ZERO),
                        "share": pct(
                            total - tax_by_type.get(op_type, ZERO),
                            totals["all_time"] - tax_totals["all_time"],
                        ),
                    }
                    for op_type, total in by_type.items()
                ),
                key=lambda item: item["net"],
                reverse=True,
            ),
            "by_asset": ranked,
            "totals": {
                # Gross: what was declared. Every breakdown above sums to it.
                **totals,
                # Withheld: tax taken at source, net of any later refund.
                "tax": tax_totals["all_time"],
                "tax_this_year": tax_totals["this_year"],
                "tax_last_12m": tax_totals["last_12m"],
                # Net: what actually reached the account.
                "net": totals["all_time"] - tax_totals["all_time"],
                "net_this_year": totals["this_year"] - tax_totals["this_year"],
                "net_last_12m": totals["last_12m"] - tax_totals["last_12m"],
                # Trading commissions, reported for transparency and pointedly
                # *not* deducted above: they are a cost of buying, not of being
                # paid a dividend.
                "trading_costs": costs,
                "payments": len(rows),
                "assets": len(by_asset),
                "best_month": max(net_by_month.items(), key=lambda kv: kv[1], default=(None, ZERO))[0],
                "best_month_amount": max(net_by_month.values(), default=ZERO),
                "average_month": (
                    sum(months_with_income, ZERO) / len(months_with_income) if months_with_income else ZERO
                ),
                "monthly_average_12m": totals["last_12m"] / 12 if totals["last_12m"] else ZERO,
                "net_monthly_average_12m": (
                    (totals["last_12m"] - tax_totals["last_12m"]) / 12 if totals["last_12m"] else ZERO
                ),
                "yield_on_cost": pct(totals["last_12m"], held_cost) if held_cost else ZERO,
                "yield_on_cost_net": (
                    pct(totals["last_12m"] - tax_totals["last_12m"], held_cost) if held_cost else ZERO
                ),
                "cost_basis": held_cost,
            },
        }

    def income_breakdown(self, period: str, granularity: str = "month") -> dict:
        """Who paid what in one period, grouped by asset class.

        The period series answers "how much"; this answers "from where", which
        is the question a single tall bar immediately raises. Figures are net of
        withholding, per asset, so the rows add up to the bar that was clicked.
        """
        assets = self.assets()
        net_by_asset: dict[int, Decimal] = defaultdict(Decimal)
        payments: dict[int, int] = defaultdict(int)

        for day, asset_id, _op_type, amount in self._income_rows():
            if self._period_key(day, granularity) != period:
                continue
            net_by_asset[asset_id] += amount
            payments[asset_id] += 1

        for day, asset_id, op_type, amount in self._income_cost_rows():
            # Commissions are a cost of trading, not a deduction from income.
            if op_type == OperationType.FEE.value:
                continue
            if self._period_key(day, granularity) != period:
                continue
            net_by_asset[asset_id] -= amount

        grouped: dict[str, list[dict]] = defaultdict(list)
        for asset_id, total in net_by_asset.items():
            asset = assets.get(asset_id)
            kind = asset.kind if asset else AssetKind.OTHER.value
            grouped[kind].append(
                {
                    "ticker": asset.ticker if asset else str(asset_id),
                    "name": asset.name if asset else "",
                    "total": total,
                    "payments": payments.get(asset_id, 0),
                }
            )

        groups = []
        for kind, items in grouped.items():
            items.sort(key=lambda item: item["total"], reverse=True)
            groups.append({"kind": kind, "total": sum((i["total"] for i in items), ZERO), "assets": items})
        groups.sort(key=lambda group: group["total"], reverse=True)

        return {
            "period": period,
            "granularity": granularity,
            "total": sum((group["total"] for group in groups), ZERO),
            "groups": groups,
        }

    def income_calendar(self, limit: int = 40) -> list[dict]:
        """The most recent payments, newest first — the 'what just landed' list.

        Each payment carries the tax withheld from it. Statements report the two
        as separate movements on the same day for the same asset, so they are
        matched back together here — a list of gross amounts next to a page of
        net figures would look like a different set of payments.
        """
        assets = self.assets()
        withheld: dict[tuple[date, int], Decimal] = defaultdict(Decimal)
        for day, asset_id, op_type, amount in self._income_cost_rows():
            if op_type == OperationType.TAX.value:
                withheld[(day, asset_id)] += amount

        rows = self.db.execute(
            select(
                Transaction.trade_date,
                Transaction.asset_id,
                Transaction.op_type,
                base_amount(Transaction.net_amount),
                Transaction.quantity,
                Transaction.unit_price,
            )
            .where(
                Transaction.portfolio_id == self.portfolio_id,
                Transaction.op_type.in_(list(INCOME_TYPE_VALUES)),
            )
            .order_by(Transaction.trade_date.desc(), Transaction.id.desc())
            .limit(limit)
        ).all()

        # One day's withholding covers that day's payments for the asset, so it
        # is consumed as it is applied rather than counted against each one.
        remaining = dict(withheld)
        calendar: list[dict] = []
        for day, asset_id, op_type, amount, quantity, unit_price in rows:
            tax = remaining.pop((day, asset_id), ZERO)
            calendar.append(
                {
                    "date": day,
                    "ticker": assets[asset_id].ticker if asset_id in assets else str(asset_id),
                    "name": assets[asset_id].name if asset_id in assets else "",
                    "kind": assets[asset_id].kind if asset_id in assets else AssetKind.OTHER.value,
                    "op_type": op_type,
                    "amount": d(amount),
                    "tax": tax,
                    "net": d(amount) - tax,
                    "quantity": quantity,
                    "unit_price": unit_price,
                }
            )
        return calendar

    def contributions_series(self, granularity: str = "month") -> list[dict]:
        """Net capital deployed per period (buys minus sells)."""
        bucket = period_bucket(Transaction.trade_date, granularity)
        rows = self.db.execute(
            select(
                bucket.label("period"),
                func.sum(base_amount(Transaction.gross_amount))
                .filter(Transaction.effect == PositionEffect.ACQUIRE.value)
                .label("bought"),
                func.sum(base_amount(Transaction.gross_amount))
                .filter(Transaction.effect == PositionEffect.DISPOSE.value)
                .label("sold"),
            )
            .where(Transaction.portfolio_id == self.portfolio_id)
            .group_by("period")
            .order_by("period")
        ).all()
        series = []
        running = ZERO
        for period, bought, sold in rows:
            net = d(bought) - d(sold)
            running += net
            series.append(
                {
                    "period": period,
                    "bought": d(bought),
                    "sold": d(sold),
                    "net": net,
                    "cumulative": running,
                }
            )
        return series

    def monthly_returns(self) -> list[dict]:
        """Monthly return: value change adjusted for the period's cash flows."""
        history = self.history(granularity="month")
        contributions = {c["period"]: c["net"] for c in self.contributions_series("month")}
        income = {i["period"]: i["total"] for i in self.income_series("month")}
        series: list[dict] = []
        previous_value = ZERO
        for point in history:
            period = point["date"].strftime("%Y-%m")
            flow = d(contributions.get(period))
            dividends = d(income.get(period))
            value = point["market_value"]
            base = previous_value + flow
            profit = value - previous_value - flow + dividends
            series.append(
                {
                    "period": period,
                    "market_value": value,
                    "flow": flow,
                    "income": dividends,
                    "profit": profit,
                    "return_pct": pct(profit, base),
                }
            )
            previous_value = value
        return series

    # -- profit ------------------------------------------------------------
    def _effective_prices(self) -> dict[int, Decimal]:
        """Today's unit price per asset, exactly as every other screen values it.

        Stored closes are only as fresh as the last backfill, and they never
        cover a manually priced holding or a CDB accrued from the CDI. Marking
        the *last* point of a curve with the same price the headline uses is
        what stops the chart ending several thousand reais below the number in
        the header.
        """
        return {ap.asset.id: ap.effective_price for ap in self.asset_positions()}

    def _market_context(self) -> tuple[dict, dict, dict, dict]:
        """Everything needed to value a past day: prices, quotes, currencies, classes."""
        prices = self._price_matrix()
        latest = {aid: d(q.price) for aid, q in self.quotes().items() if q.price is not None}
        currencies = {
            asset_id: (asset.currency or self.base_currency).upper()
            for asset_id, asset in self.assets().items()
        }
        kinds = {asset_id: asset.kind for asset_id, asset in self.assets().items()}
        return prices, latest, currencies, kinds

    class _PriceCursor:
        """Each asset's last known close, walked forward one day at a time.

        The return chain has to value every position on every day, and a binary
        search per asset per day is wasted work when the caller only ever moves
        forward: a cursor per asset turns the whole walk into one pass over the
        price matrix.
        """

        __slots__ = ("_matrix", "_index", "_last")

        def __init__(self, matrix: dict[int, list[tuple[date, Decimal]]]) -> None:
            self._matrix = matrix
            self._index: dict[int, int] = defaultdict(int)
            self._last: dict[int, Decimal] = {}

        def price(self, asset_id: int, day: date) -> Decimal | None:
            series = self._matrix.get(asset_id)
            if not series:
                return None
            index = self._index[asset_id]
            while index < len(series) and series[index][0] <= day:
                self._last[asset_id] = series[index][1]
                index += 1
            self._index[asset_id] = index
            return self._last.get(asset_id)

    def _flows_by_day(self) -> dict[date, list[tuple[int, Decimal]]]:
        """Capital in (+) and out (-) per asset, per day, in the base currency.

        The denominator of a money-weighted return: money the investor put in is
        not a gain, and money taken out is not a loss.
        """
        flows: dict[date, list[tuple[int, Decimal]]] = defaultdict(list)
        for movement in self.base_movements():
            if movement.effect == PositionEffect.ACQUIRE.value:
                flows[movement.trade_date].append((movement.asset_id, movement.net_cost))
            elif movement.effect == PositionEffect.DISPOSE.value:
                flows[movement.trade_date].append((movement.asset_id, -movement.net_proceeds))
        return flows

    @staticmethod
    def dietz(
        gain: Decimal,
        opening: Decimal,
        flow: Decimal,
        flow_time: Decimal,
        start: date,
        end: date,
    ) -> Decimal | None:
        """Modified Dietz: a return that knows how much money was at work.

        The denominator is the opening balance plus each contribution weighted
        by the fraction of the window it was actually invested — so R$ 400 mil
        that arrived last month counts for a month, not for six years. That is
        the difference between "how well was this run" and "what did my money
        do", and it is the second question this page is asked.

        ``flow`` and ``flow_time`` are the window's net capital and the same
        flows multiplied by their date, both obtained by subtracting two stored
        running sums (see :class:`app.db.models.PortfolioSnapshot`), which is
        what makes this O(1) per plotted point instead of a pass over years of
        movements.

        ``None`` when there was no capital at work to earn on: dividing a result
        by nothing produces a number, and it is never the right one.
        """
        span = end.toordinal() - start.toordinal()
        if span <= 0:
            return ZERO
        weighted = (Decimal(end.toordinal()) * flow - flow_time) / Decimal(span)
        base = opening + weighted
        if base <= MONEY_EPSILON:
            return None
        return gain / base * Decimal(100)

    @staticmethod
    def _period_return(gain: Decimal, base: Decimal) -> Decimal:
        """One day's time-weighted return, as a factor around 1.

        ``gain`` is what yesterday's holding earned today — its price move plus
        anything it paid — and ``base`` is what that holding was worth. Nothing
        that arrived or left today appears in either, which is what makes the
        figure a *return* rather than a record of money moving:

        * a purchase cannot show up as an instant loss, however its trade price
          compares with that evening's close;
        * a sale cannot inflate the day by shrinking the denominator;
        * a split, a bonificação or shares credited by a merger change the
          quantity on both sides of a smooth price series and are simply not
          performance — counting them is what turns a 10-for-1 grupamento into
          a 90 % crash.

        A base of nothing means there was nothing to earn on, and a result the
        base could not plausibly have produced is a data artifact rather than a
        market move: both answer "no return", and the new value just becomes
        tomorrow's opening balance.
        """
        if base <= MONEY_EPSILON:
            return ONE
        if abs(gain) > base * IMPLAUSIBLE_DAILY_GAIN:
            return ONE
        return ONE + gain / base

    def ledger_fingerprint(self) -> str:
        """What the stored chain was built from.

        Cheap aggregates over everything the replay reads. A snapshot whose
        fingerprint no longer matches is recomputed rather than served — an
        import, a declared succession or a fresh price backfill all change the
        answer, and a fast wrong chart is worse than a slow right one.
        ``CHAIN_VERSION`` is in the mix so a change to the arithmetic itself
        invalidates every stored row without anyone having to remember.
        """
        from app.db.models import AssetSuccession

        transactions = self.db.execute(
            select(func.count(Transaction.id), func.max(Transaction.id)).where(
                Transaction.portfolio_id == self.portfolio_id
            )
        ).one()
        successions = self.db.execute(
            select(func.count(AssetSuccession.id), func.max(AssetSuccession.id)).where(
                AssetSuccession.portfolio_id == self.portfolio_id
            )
        ).one()
        history = self.db.execute(
            select(func.count(PriceHistory.id), func.max(PriceHistory.date))
        ).one()
        assets = self.db.execute(select(func.count(Asset.id), func.max(Asset.id))).one()
        raw = f"{CHAIN_VERSION}|{transactions}|{successions}|{history}|{assets}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def daily_chain(self) -> list[dict]:
        """The portfolio, valued and its return chained, once per day.

        This is the expensive pass — every open position priced on every day
        since the first movement — and everything else on the rentabilidade
        page is a slice of it. :meth:`profit_history` reads it back from
        ``portfolio_snapshots`` rather than running it per request.

        The return is time-weighted and chained **daily**, and each day's
        numerator is only what the *previous* day's holding earned: its price
        move plus anything it paid. See :meth:`_period_return` for why nothing
        that arrived or left that day may appear in it.
        """
        timeline = build_timeline(self.base_movements(), self.successions())
        if not timeline:
            return []

        first_day = timeline[0].day
        last_day = max(timeline[-1].day, local_today())
        prices, latest, currencies, raw_kinds = self._market_context()
        # Bucketed up front: see CHART_KINDS. The by-class series has to be
        # formed before any return is chained.
        kinds = {asset_id: chart_kind(kind) for asset_id, kind in raw_kinds.items()}
        effective = self._effective_prices()
        cursor = self._PriceCursor(prices)
        flows_by_day = self._flows_by_day()

        rows: list[dict] = []
        index = 0
        current = timeline[0]
        day = first_day

        factor = ONE
        factor_by_kind: dict[str, Decimal] = defaultdict(lambda: ONE)
        previous_quantity: dict[int, Decimal] = {}
        previous_price: dict[int, Decimal] = {}
        previous_income: dict[int, Decimal] = {}
        # Running sums for the money-weighted return; see `dietz`.
        flow_total = ZERO
        flow_time_total = ZERO
        flow_by_kind: dict[str, Decimal] = defaultdict(Decimal)
        flow_time_by_kind: dict[str, Decimal] = defaultdict(Decimal)

        while day <= last_day:
            while index < len(timeline) and timeline[index].day <= day:
                current = timeline[index]
                index += 1

            stamp = Decimal(day.toordinal())
            for asset_id, amount in flows_by_day.get(day, ()):
                kind = kinds.get(asset_id, AssetKind.OTHER.value)
                flow_total += amount
                flow_time_total += amount * stamp
                flow_by_kind[kind] += amount
                flow_time_by_kind[kind] += amount * stamp

            result_by_kind: dict[str, Decimal] = defaultdict(Decimal)
            value_by_kind: dict[str, Decimal] = defaultdict(Decimal)
            unrealized = ZERO
            total_value = ZERO
            quantity_now: dict[int, Decimal] = {}
            price_now: dict[int, Decimal] = {}
            # The day's return, built asset by asset. Numerator: what
            # yesterday's holding earned — its price move plus anything it
            # paid. Denominator: what that holding was worth.
            gain = ZERO
            base = ZERO
            gain_by_kind: dict[str, Decimal] = defaultdict(Decimal)
            base_by_kind: dict[str, Decimal] = defaultdict(Decimal)

            for asset_id in current.quantities.keys() | previous_quantity.keys():
                kind = kinds.get(asset_id, AssetKind.OTHER.value)
                quantity = current.quantities.get(asset_id, ZERO)
                cost = current.costs.get(asset_id, ZERO)
                close = cursor.price(asset_id, day)
                rate = self._rate_on(currencies.get(asset_id), day)
                unit = None if close is None else close * rate
                if unit is None and quantity > ZERO:
                    # A paper with no close accrues instead. Expressed as a unit
                    # price so the day's interest also reaches the return chain
                    # below — valuing it at cost would report a CDB as earning
                    # nothing every day of its life.
                    accrued = self.accrued_value_on(asset_id, day)
                    if accrued is not None:
                        unit = accrued / quantity

                if quantity > ZERO:
                    # The last row is "now", and now is priced live — see
                    # _effective_prices. Anything else ends the curve on a
                    # stale close and makes the chart contradict the header.
                    # It marks the *value* only: an asset with no close on file
                    # falls back to its average price there, and letting that
                    # into the return would print the gap as a final-day move.
                    mark = effective.get(asset_id) if day == last_day else None
                    value = (
                        (cost if unit is None else quantity * unit)
                        if mark is None
                        else quantity * mark * rate
                    )
                    unrealized += value - cost
                    total_value += value
                    value_by_kind[kind] += value
                    result_by_kind[kind] += value - cost
                    quantity_now[asset_id] = quantity
                    if unit is not None:
                        price_now[asset_id] = unit

                held = previous_quantity.get(asset_id, ZERO)
                before = previous_price.get(asset_id)
                if held <= ZERO or before is None or unit is None:
                    continue
                paid = current.income_by_asset.get(asset_id, ZERO) - previous_income.get(asset_id, ZERO)
                moved = held * (unit - before) + paid
                gain += moved
                base += held * before
                gain_by_kind[kind] += moved
                base_by_kind[kind] += held * before

            if rows:
                factor *= self._period_return(gain, base)
                for kind in base_by_kind:
                    factor_by_kind[kind] *= self._period_return(gain_by_kind[kind], base_by_kind[kind])
            # A class held today owns a line from today on, even before it has
            # a day behind it: a key that appears only on its second point
            # draws as a gap in the series.
            for kind in value_by_kind:
                factor_by_kind[kind]  # noqa: B018 — seeds the defaultdict at 1

            previous_quantity = quantity_now
            previous_price = price_now
            previous_income = dict(current.income_by_asset)

            for asset_id, amount in current.realized_by_asset.items():
                result_by_kind[kinds.get(asset_id, AssetKind.OTHER.value)] += amount
            for asset_id, amount in current.income_by_asset.items():
                result_by_kind[kinds.get(asset_id, AssetKind.OTHER.value)] += amount

            rows.append(
                {
                    "date": day,
                    "market_value": total_value,
                    "cost_basis": current.cost_basis,
                    "invested": current.invested_flow,
                    "unrealized": unrealized,
                    "realized": current.realized,
                    "income": current.dividends,
                    "profit": unrealized + current.realized + current.dividends,
                    "priced_value": base,
                    "factor": factor,
                    "flow": flow_total,
                    "flow_time": flow_time_total,
                    "kinds": {
                        kind: {
                            "factor": factor_by_kind[kind],
                            "result": result_by_kind.get(kind, ZERO),
                            "value": value_by_kind.get(kind, ZERO),
                            "flow": flow_by_kind[kind],
                            "flow_time": flow_time_by_kind[kind],
                        }
                        for kind in set(factor_by_kind) | set(result_by_kind) | set(flow_by_kind)
                    },
                }
            )
            day += timedelta(days=1)
        return rows

    def _stored_chain(self) -> list[dict] | None:
        """The chain as last materialised, or ``None`` if it cannot be trusted.

        Trust means two things: it was built from today's ledger, and it
        reaches today. Anything else and the caller recomputes.
        """
        fingerprint = self.ledger_fingerprint()
        rows = self.db.execute(
            select(PortfolioSnapshot)
            .where(
                PortfolioSnapshot.portfolio_id == self.portfolio_id,
                PortfolioSnapshot.fingerprint == fingerprint,
            )
            .order_by(PortfolioSnapshot.date)
        ).scalars().all()
        if not rows or rows[-1].date < local_today():
            return None
        return [
            {
                "date": row.date,
                "market_value": d(row.market_value),
                "cost_basis": d(row.cost_basis),
                "invested": d(row.net_invested),
                "unrealized": d(row.unrealized),
                "realized": d(row.realized_cumulative),
                "income": d(row.dividends_cumulative),
                "profit": d(row.unrealized) + d(row.realized_cumulative) + d(row.dividends_cumulative),
                "priced_value": d(row.priced_value),
                "factor": d(row.return_factor),
                "flow": d(row.flow),
                "flow_time": d(row.flow_time),
                "kinds": {
                    kind: {key: d(value) for key, value in state.items()}
                    for kind, state in (row.kind_state or {}).items()
                },
            }
            for row in rows
        ]

    def chain(self) -> list[dict]:
        """The daily chain, materialising it first if the stored one is stale."""
        stored = self._stored_chain()
        if stored is not None:
            return stored
        rows = self.daily_chain()
        if rows:
            try:
                self._store_chain(rows)
            except Exception:  # noqa: BLE001 — a cache write must never fail a read
                self.db.rollback()
                logger.exception("could not materialise the daily return chain")
        return rows

    def _store_chain(self, rows: list[dict]) -> None:
        fingerprint = self.ledger_fingerprint()
        self.db.query(PortfolioSnapshot).filter(
            PortfolioSnapshot.portfolio_id == self.portfolio_id
        ).delete()
        for row in rows:
            self.db.add(
                PortfolioSnapshot(
                    portfolio_id=self.portfolio_id,
                    date=row["date"],
                    market_value=row["market_value"],
                    cost_basis=row["cost_basis"],
                    net_invested=row["invested"],
                    cash_flow=ZERO,
                    dividends_cumulative=row["income"],
                    realized_cumulative=row["realized"],
                    unrealized=row["unrealized"],
                    return_factor=row["factor"],
                    priced_value=row["priced_value"],
                    flow=row["flow"],
                    flow_time=row["flow_time"],
                    kind_state={
                        kind: {key: str(value) for key, value in state.items()}
                        for kind, state in row["kinds"].items()
                    },
                    fingerprint=fingerprint,
                )
            )
        self.db.commit()

    def profit_history(
        self,
        start: date | None = None,
        granularity: str = "auto",
        group_by: str = "total",
    ) -> list[dict]:
        """The rentabilidade series: result in money and return in percent.

        Two readings of the same daily chain, because they answer different
        questions:

        * **Result** in money — the unrealised gap between market value and
          cost, *plus* every result already realised and every provento
          received. Plotting only the unrealised part would show a portfolio
          that sold its winners as having made nothing.
        * **Return** as a percentage — time-weighted, so it is comparable with
          an index. A raw value-over-value ratio is not: it would credit every
          deposit as performance.

        Both are rebased to the **first plotted point**, which is also what the
        benchmarks are rebased to, so every line on the chart starts from the
        same place and answers "over this window".
        """
        rows = self.chain()
        if not rows:
            return []

        first_day = rows[0]["date"]
        last_day = rows[-1]["date"]
        start = start or first_day
        step = self._resolve_step(start, last_day, granularity)
        split = group_by == "kind"

        window = [
            row
            for row in rows
            if row["date"] >= start
            and (row["date"] == last_day or self._is_step_day(row["date"], step))
        ]
        if not window:
            return []

        # Rebasing is arithmetic on two stored rows: this is what the chain buys.
        opening = window[0]
        base_factor = opening["factor"] or ONE
        base_kind = {kind: state["factor"] or ONE for kind, state in opening["kinds"].items()}
        first_day_of_window = opening["date"]

        points = []
        for row in window:
            gain = row["profit"] - opening["profit"]
            money = self.dietz(
                gain,
                opening["market_value"],
                row["flow"] - opening["flow"],
                row["flow_time"] - opening["flow_time"],
                first_day_of_window,
                row["date"],
            )
            kinds_pct: dict[str, Decimal] = {}
            if split:
                for kind, state in row["kinds"].items():
                    before = opening["kinds"].get(kind, {})
                    value = self.dietz(
                        state["result"] - before.get("result", ZERO),
                        before.get("value", ZERO),
                        state["flow"] - before.get("flow", ZERO),
                        state["flow_time"] - before.get("flow_time", ZERO),
                        first_day_of_window,
                        row["date"],
                    )
                    if value is not None:
                        kinds_pct[kind] = value
            points.append(
                {
                    "date": row["date"],
                    "profit": row["profit"],
                    "unrealized": row["unrealized"],
                    "realized": row["realized"],
                    "income": row["income"],
                    "cost_basis": row["cost_basis"],
                    "market_value": row["market_value"],
                    # The headline: what the money made, weighting each
                    # contribution by how long it was actually invested.
                    "return_pct": money if money is not None else ZERO,
                    # The same history run size-blind — every day weighted
                    # equally, whatever was at stake. This is the figure an
                    # index is a fair comparison for, and it is what the page
                    # quotes underneath the chart.
                    "twr_pct": (row["factor"] / base_factor - ONE) * Decimal(100),
                    # What share of the portfolio the time-weighted figure
                    # speaks for. A CDB has no daily close on file, so it is
                    # outside it; saying so beats quietly diluting it to zero.
                    "priced_share": (
                        pct(row["priced_value"], row["market_value"]) if row["market_value"] else ZERO
                    ),
                    "kinds": (
                        {kind: state["result"] for kind, state in row["kinds"].items()} if split else {}
                    ),
                    "kinds_pct": kinds_pct,
                    "kinds_twr_pct": (
                        {
                            kind: (state["factor"] / (base_kind.get(kind) or ONE) - ONE) * Decimal(100)
                            for kind, state in row["kinds"].items()
                        }
                        if split
                        else {}
                    ),
                    # Filled in below: the benchmarks need the whole list of
                    # days at once to rebase to the first of them.
                    "benchmarks": {},
                }
            )

        self._attach_benchmarks(points)
        return points

    def _attach_benchmarks(self, points: list[dict]) -> None:
        """Add each benchmark's return over the same window to every point."""
        if not points:
            return
        from app.market.benchmarks import BENCHMARKS, series  # local: avoids a cycle

        days = [point["date"] for point in points]
        for code in BENCHMARKS:
            try:
                values = series(self.db, code, days)
            except Exception:  # noqa: BLE001 — a benchmark must never break the chart
                continue
            for point in points:
                if point["date"] in values:
                    point["benchmarks"][code] = values[point["date"]]

    def _asset_state_on(
        self,
        timeline: list,
        day: date,
        prices: dict,
        latest: dict,
        currencies: dict,
    ) -> dict[int, tuple[Decimal, Decimal, bool]]:
        """Per asset at the end of ``day``: (market value, result, was priced).

        The third element is the honesty flag. An asset held on that day with no
        stored close is valued at cost, which makes its result look like zero —
        fine as a fallback for a chart, useless as the starting point of a
        window measurement, so the caller drops it instead of crediting the
        whole lifetime gain to the window.
        """
        point = None
        for candidate in timeline:
            if candidate.day > day:
                break
            point = candidate
        state: dict[int, tuple[Decimal, Decimal, bool]] = {}
        if point is None:
            return state

        for asset_id, qty in point.quantities.items():
            cost = point.costs.get(asset_id, ZERO)
            price = self._price_at(prices, latest, asset_id, day)
            if price is not None:
                value = qty * price * self._rate_on(currencies.get(asset_id), day)
                state[asset_id] = (value, value - cost, True)
                continue
            # An accrued paper is priced — computed rather than fetched, but
            # every bit as real as a close, so it is not dropped from a window.
            accrued = self.accrued_value_on(asset_id, day)
            if accrued is not None:
                state[asset_id] = (accrued, accrued - cost, True)
            else:
                state[asset_id] = (cost, ZERO, False)
        for source in (point.realized_by_asset, point.income_by_asset):
            for asset_id, amount in source.items():
                value, profit, priced = state.get(asset_id, (ZERO, ZERO, True))
                state[asset_id] = (value, profit + amount, priced)
        return state

    def performance(self, window: str = "total", limit: int = 5) -> dict:
        """Best and worst assets over a window, ranked in the base currency.

        Every row carries ``window_change`` and ``window_pct`` whatever the
        window, so the caller reads one pair of fields instead of branching on
        which period it asked for.
        """
        positions = self.asset_positions(include_closed=True)
        portfolio_value = sum((ap.market_value_base for ap in positions), ZERO)
        rows = {ap.asset.id: ap.to_dict(portfolio_value) for ap in positions}
        # Quantity that arrived free — a fund merger, a restructuring — has no
        # capital behind it, so its return has no denominator. Tracked here
        # because ``to_dict`` reports that case as 0%, which reads as "went
        # nowhere" beside a five-figure gain.
        invested = {
            ap.asset.id: max(ap.position.total_bought_amount, ap.position.cost_basis)
            for ap in positions
        }
        as_of: object | None = None
        ranked: list[dict]

        if window == "day":
            # Read off the quote's own previous close, so the ranking is always
            # the last *session* — on a Monday morning, and all weekend, that is
            # Friday's, which is exactly what a "melhores do dia" list should
            # show when the market has not opened since.
            ranked = [
                {**row, "window_change": row["day_change_base"], "window_pct": row["day_change_pct"]}
                for row in rows.values()
                if row["has_market_price"] and abs(row["day_change_base"]) > MONEY_EPSILON
            ]
            as_of = max((ap.quote_time for ap in positions if ap.quote_time), default=None)
        elif window in WINDOW_DAYS:
            end = local_today()
            begin = end - timedelta(days=WINDOW_DAYS[window])
            timeline = build_timeline(self.base_movements(), self.successions())
            prices, latest, currencies, _ = self._market_context()
            before = self._asset_state_on(timeline, begin, prices, latest, currencies)

            # Capital put in (or taken out) during the window. A position that
            # doubled because it was bought into, not because it went up, must
            # not report that as a return — so the denominator is what was
            # actually at risk: the value at the start plus what went in after.
            flows: dict[int, Decimal] = defaultdict(Decimal)
            for movement in self.base_movements():
                if not begin < movement.trade_date <= end:
                    continue
                if movement.effect == PositionEffect.ACQUIRE.value:
                    flows[movement.asset_id] += movement.net_cost
                elif movement.effect == PositionEffect.DISPOSE.value:
                    flows[movement.asset_id] -= movement.net_proceeds

            ranked = []
            for asset_id, row in rows.items():
                start_value, start_profit, priced = before.get(asset_id, (ZERO, ZERO, True))
                if not priced:
                    continue  # unknown starting point — see _asset_state_on
                change = d(row["total_return_base"]) - start_profit
                if abs(change) <= MONEY_EPSILON:
                    continue
                base = start_value + max(flows.get(asset_id, ZERO), ZERO)
                ranked.append(
                    {
                        **row,
                        "window_change": change,
                        # No capital behind the move — quantity that arrived
                        # free, from a merger or a restructuring — so there is
                        # no percentage to state. ``None`` says that; a zero
                        # would claim the position went nowhere.
                        "window_pct": pct(change, base) if abs(base) > MONEY_EPSILON else None,
                    }
                )
            as_of = end
        else:
            ranked = [
                {
                    **row,
                    "window_change": row["total_return_base"],
                    "window_pct": (
                        row["total_return_pct"]
                        if invested.get(asset_id, ZERO) > MONEY_EPSILON
                        else None
                    ),
                }
                for asset_id, row in rows.items()
            ]
            window = "total"

        ranked.sort(key=lambda item: item["window_change"], reverse=True)
        return {
            "window": window,
            "as_of": as_of,
            "best": ranked[:limit],
            "worst": (
                list(reversed(ranked[-limit:])) if len(ranked) > limit else list(reversed(ranked))
            ),
        }

    # -- reports -----------------------------------------------------------
    def performers(self, limit: int = 5) -> dict[str, list[dict]]:
        """Best and worst assets, ranked in the portfolio's own currency.

        Ranking on the native figure would sort dollars against reais and put a
        modest US gain below a smaller Brazilian one.
        """
        positions = self.asset_positions(include_closed=True)
        total = sum((ap.market_value_base for ap in positions), ZERO)
        enriched = [ap.to_dict(total) for ap in positions]
        ranked = sorted(enriched, key=lambda a: a["total_return_base"], reverse=True)
        return {
            "best": ranked[:limit],
            "worst": list(reversed(ranked[-limit:])) if len(ranked) > limit else list(reversed(ranked)),
        }

    def annual_report(self) -> list[dict]:
        rows = self.db.execute(
            select(
                period_bucket(Transaction.trade_date, "year").label("year"),
                Transaction.op_type,
                func.sum(base_amount(Transaction.gross_amount)),
                func.count(),
            )
            .where(Transaction.portfolio_id == self.portfolio_id)
            .group_by("year", Transaction.op_type)
            .order_by("year")
        ).all()
        grouped: dict[str, dict] = defaultdict(
            lambda: {"bought": ZERO, "sold": ZERO, "income": ZERO, "transactions": 0}
        )
        for year, op_type, amount, count in rows:
            entry = grouped[year]
            entry["transactions"] += count
            if op_type == OperationType.BUY.value:
                entry["bought"] += d(amount)
            elif op_type == OperationType.SELL.value:
                entry["sold"] += d(amount)
            elif op_type in INCOME_TYPE_VALUES:
                entry["income"] += d(amount)
        history = {p["date"].strftime("%Y"): p for p in self.history(granularity="month")}
        return [
            {
                "year": year,
                **{k: v for k, v in values.items()},
                "market_value": history.get(year, {}).get("market_value", ZERO),
            }
            for year, values in sorted(grouped.items())
        ]

    # -- snapshots ---------------------------------------------------------
    def rebuild_snapshots(self) -> int:
        """Materialise the daily walk into ``portfolio_snapshots``.

        The same pass that values the portfolio also chains its return, so the
        nightly job leaves the rentabilidade page with nothing left to compute.
        """
        rows = self.daily_chain()
        if not rows:
            return 0
        self._store_chain(rows)
        return len(rows)
