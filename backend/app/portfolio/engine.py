"""Portfolio calculation engine.

Design
------
The engine is a **pure replay** over the transaction history: feed it an ordered
list of movements and it returns the state of every position. It never touches
the database or the network, which makes it trivial to unit test and cheap to
re-run after every import.

Cost basis follows the Brazilian *preço médio* (weighted average) convention:

* a purchase adds quantity and cost -> the average price moves;
* a sale removes quantity and cost **proportionally** -> the average price is
  unchanged, and the realised result is ``(price - average) * quantity``;
* free quantity (splits, bonuses, subscription receipts) adds quantity with no
  cost -> the average price dilutes, total cost is preserved. This is what
  makes splits and reverse splits work without needing an explicit ratio;
* a return of capital (amortisation) reduces the cost basis instead of counting
  as income, and only the excess over the remaining basis is realised.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation

from app.domain.enums import INCOME_TYPES, OperationType, PositionEffect

ZERO = Decimal(0)
#: Quantities below this are treated as fully closed (float dust from B3).
QTY_EPSILON = Decimal("0.00000001")
#: Above ``QTY_EPSILON`` but still not worth telling a human about. A residue of
#: 2E-8 coins is arithmetically present and completely uninteresting, and a
#: warning that reports it in scientific notation reads as a malfunction.
DUST_QUANTITY = Decimal("0.000001")
#: The same idea for money, at the resolution amounts are stored in.
MONEY_DUST = Decimal("0.01")
#: How far apart the two legs of a custody transfer may be dated and still be
#: recognised as the same move. Brokers book the departure and the arrival on
#: their own settlement dates, usually a day or two apart.
TRANSFER_PAIRING_DAYS = 7


@dataclass(slots=True)
class Movement:
    """Engine-level view of a transaction (decoupled from the ORM)."""

    asset_id: int
    trade_date: date
    op_type: str
    effect: str
    quantity: Decimal
    unit_price: Decimal
    gross_amount: Decimal
    fees: Decimal = ZERO
    taxes: Decimal = ZERO
    id: int | None = None
    #: Custody where the movement happened (kept for reporting/traceability).
    broker_id: int | None = None
    #: Currency the amounts above are expressed in.
    currency: str = "BRL"
    #: Rate to the portfolio's base currency on the trade date, when known.
    fx_rate: Decimal | None = None

    @property
    def net_cost(self) -> Decimal:
        """Cash actually paid on an acquisition (amount + costs)."""
        return self.gross_amount + self.fees + self.taxes

    @property
    def net_proceeds(self) -> Decimal:
        """Cash actually received on a disposal (amount - costs)."""
        return self.gross_amount - self.fees - self.taxes


@dataclass(slots=True)
class Position:
    """Running state of a single asset."""

    asset_id: int
    quantity: Decimal = ZERO
    cost_basis: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    #: Income as *declared* — the gross figure ``income_by_type`` breaks down.
    income: Decimal = ZERO
    income_by_type: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    #: Tax withheld at source on that income, net of refunds returned later.
    #: Kept apart from ``income`` so the gross figure survives, and apart from
    #: ``fees`` because brokerage is a cost of *trading* while this is a
    #: deduction from the income itself — netting the two would make a purchase
    #: commission look like it came out of a dividend. Every figure that
    #: reports a *result* subtracts it; see ``PortfolioService.overview``.
    income_tax: Decimal = ZERO
    returned_capital: Decimal = ZERO
    fees: Decimal = ZERO
    taxes: Decimal = ZERO
    total_bought_qty: Decimal = ZERO
    total_bought_amount: Decimal = ZERO
    total_sold_qty: Decimal = ZERO
    total_sold_amount: Decimal = ZERO
    #: Cost that left with a fraction, held until B3 auctions and pays for it.
    pending_fraction_cost: Decimal = ZERO
    #: Cost and quantity sitting in an exchange's staking / Simple Earn product.
    #: The coins are still owned but are no longer in the reported balance, so
    #: their cost waits here rather than being written off — see
    #: :attr:`PositionEffect.QTY_OUT_PARKED`.
    parked_cost: Decimal = ZERO
    parked_quantity: Decimal = ZERO
    #: What was still in staking / Simple Earn when the replay ended, folded
    #: back into ``quantity`` and ``cost_basis`` above. Kept separately only so
    #: the UI can say *where* the coins are — they are owned either way, and a
    #: portfolio that hides them reports a position of zero for a balance the
    #: exchange is paying interest on.
    staked_quantity: Decimal = ZERO
    staked_cost: Decimal = ZERO
    #: Quantity that arrived from outside with no purchase anywhere in the
    #: history — coins deposited from a wallet the export cannot see. It is
    #: held at zero cost because inventing one would be worse, but a disposal
    #: then realises its whole proceeds, so the amount is tracked and reported.
    uncosted_quantity: Decimal = ZERO
    #: Cash received for that quantity. Deliberately kept out of
    #: ``realized_pnl``: a result is proceeds *minus cost*, and when the cost is
    #: unknown the subtraction cannot be done. Booking the whole proceeds as a
    #: gain does not make the number knowable, it only makes it wrong — and
    #: loudly so, since dividing it by a cost basis of nothing produces a return
    #: of several hundred thousand percent.
    uncosted_proceeds: Decimal = ZERO
    first_trade: date | None = None
    last_trade: date | None = None
    transactions: int = 0
    #: Genuine data problems worth a human looking at them.
    warnings: list[str] = field(default_factory=list)
    #: Interpretation decisions taken automatically (ambiguous B3 rows).
    notes: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.quantity > QTY_EPSILON

    @property
    def average_price(self) -> Decimal:
        if self.quantity <= QTY_EPSILON:
            return ZERO
        try:
            return self.cost_basis / self.quantity
        except (InvalidOperation, ZeroDivisionError):
            return ZERO

    def snapshot(self) -> tuple[Decimal, Decimal]:
        return self.quantity, self.cost_basis


def _income_bucket(op_type: str) -> str:
    return op_type if op_type in {t.value for t in INCOME_TYPES} else OperationType.DIVIDEND.value


def neutralize_paired_transfers(movements: list[Movement]) -> list[Movement]:
    """Neutralise custody transfers that are internal to the portfolio.

    Moving shares between two of your own brokers shows up as a credit in one
    institution and a debit in the other. At portfolio level that is a no-op —
    applying both would strip the cost basis (quantity out at cost, back in for
    free). Rows that pair up on asset and quantity are rewritten to
    :attr:`PositionEffect.LEDGER_ONLY` so they stay visible in the audit trail
    without moving anything. Unpaired rows keep their original effect, because
    those are genuine inbound/outbound custody moves.

    The two sides need not share a date. When Nomad moved from DriveWealth to
    Apex the shares left on 2025-11-07 and arrived on 2025-11-08, each broker
    dating the move by its own books, so pairing allows a few days' drift and
    matches each credit to its nearest unclaimed debit.
    """
    outs: dict[tuple[int, Decimal], list[tuple[date, int]]] = defaultdict(list)
    ins: dict[tuple[int, Decimal], list[tuple[date, int]]] = defaultdict(list)
    for index, mv in enumerate(movements):
        key = (mv.asset_id, mv.quantity)
        if mv.op_type == OperationType.TRANSFER_OUT.value:
            outs[key].append((mv.trade_date, index))
        elif mv.op_type == OperationType.TRANSFER_IN.value:
            ins[key].append((mv.trade_date, index))

    paired: set[int] = set()
    for key, inbound in ins.items():
        outbound = sorted(outs.get(key, []))
        claimed: set[int] = set()
        for in_date, in_index in sorted(inbound):
            best: tuple[int, int, int] | None = None
            for slot, (out_date, out_index) in enumerate(outbound):
                if slot in claimed:
                    continue
                distance = abs((out_date - in_date).days)
                if distance > TRANSFER_PAIRING_DAYS:
                    continue
                if best is None or distance < best[0]:
                    best = (distance, slot, out_index)
            if best is not None:
                claimed.add(best[1])
                paired.add(in_index)
                paired.add(best[2])
    if not paired:
        return movements
    return [
        replace(mv, effect=PositionEffect.LEDGER_ONLY.value) if index in paired else mv
        for index, mv in enumerate(movements)
    ]


def resolve_share_restructures(movements: list[Movement]) -> list[Movement]:
    """Detect B3's *grupamento + desdobramento* share restructurings.

    A plain ``Desdobro`` credits the **extra** shares — a delta (verified: a
    53-share position plus a 477 credit becomes the 565 B3 itself reports on the
    next distribution). But when B3 emits a ``Grupamento`` **and** a ``Desdobro``
    for the same asset on the same day, it is describing a share restructuring:
    the credits state the **resulting** position and the old shares are consumed
    without an explicit debit.

    Applying those rows as deltas inflates the position enormously — a holding
    that was restructured 1:25 and later sold in full would otherwise linger
    with thousands of phantom shares.

    The first credit of the day is rewritten to
    :attr:`PositionEffect.QTY_RESTATE` carrying the day's total; the remaining
    credits become no-ops so the total is applied exactly once.
    """
    grouped: dict[tuple[int, date], list[int]] = defaultdict(list)
    has_reverse: set[tuple[int, date]] = set()
    has_forward: set[tuple[int, date]] = set()

    for index, mv in enumerate(movements):
        if mv.effect != PositionEffect.QTY_IN_FREE.value:
            continue
        key = (mv.asset_id, mv.trade_date)
        if mv.op_type == OperationType.REVERSE_SPLIT.value:
            has_reverse.add(key)
            grouped[key].append(index)
        elif mv.op_type == OperationType.SPLIT.value:
            has_forward.add(key)
            grouped[key].append(index)

    restructures = has_reverse & has_forward
    if not restructures:
        return movements

    result = list(movements)
    for key in restructures:
        indexes = sorted(grouped[key])
        total = sum((movements[i].quantity or ZERO for i in indexes), ZERO)
        result[indexes[0]] = replace(
            movements[indexes[0]], effect=PositionEffect.QTY_RESTATE.value, quantity=total
        )
        for index in indexes[1:]:
            result[index] = replace(movements[index], effect=PositionEffect.NONE.value)
    return result


@dataclass(frozen=True, slots=True)
class Succession:
    """One asset became another: merger, ticker change, delisting.

    B3 credits the successor and never debits the predecessor, so without this
    link the old ticker keeps the whole cost basis as a phantom position while
    the new one holds free shares. ``to_asset_id`` of ``None`` marks an artifact
    (an intermediate holding vehicle) whose movements are dropped entirely.
    """

    from_asset_id: int
    to_asset_id: int | None
    effective_date: date
    #: Cash received in the event; reduces the cost carried over.
    cash_amount: Decimal = ZERO


def drop_voided_assets(movements: list[Movement], successions: Sequence[Succession]) -> list[Movement]:
    """Neutralise every movement of an asset declared an artifact.

    An intermediate vehicle (B3 hands out holding units mid-merger, then
    redeems them) exists only inside the event. Left alone, its zero-cost
    redemption books an invented realised gain.
    """
    voided = {s.from_asset_id for s in successions if s.to_asset_id is None}
    if not voided:
        return movements
    return [
        replace(mv, effect=PositionEffect.NONE.value) if mv.asset_id in voided else mv
        for mv in movements
    ]


def apply_succession(positions: dict[int, Position], succession: Succession) -> None:
    """Close the predecessor and carry its cost basis into the successor.

    Only cost moves: the successor's *quantity* was already credited by B3's own
    corporate-action row, which arrived for free. Cash received in the event is
    treated as returned capital (it reduces the basis carried over) rather than
    as a gain — the usual retail treatment of a cash-plus-shares merger. Cash
    beyond the basis has nothing left to reduce and is realised.
    """
    source = positions.get(succession.from_asset_id)
    if source is None or succession.to_asset_id is None:
        return

    carried = source.cost_basis - succession.cash_amount
    source.returned_capital += succession.cash_amount
    if carried < ZERO:
        source.realized_pnl += -carried
        carried = ZERO

    source.quantity = ZERO
    source.cost_basis = ZERO
    source.notes.append(f"sucedido em {succession.effective_date.isoformat()}")

    target = positions.setdefault(succession.to_asset_id, Position(asset_id=succession.to_asset_id))
    target.cost_basis += carried
    target.notes.append(
        f"recebeu {carried:.2f} de custo do ativo sucedido em {succession.effective_date.isoformat()}"
    )


def _successions_by_day(successions: Sequence[Succession]) -> dict[date, list[Succession]]:
    by_day: dict[date, list[Succession]] = defaultdict(list)
    for succession in successions:
        if succession.to_asset_id is not None:
            by_day[succession.effective_date].append(succession)
    return by_day


def _prepare(movements: list[Movement], successions: Sequence[Succession]) -> list[Movement]:
    """The rewrites every replay applies before the first movement is booked."""
    return sorted(
        resolve_share_restructures(
            drop_voided_assets(neutralize_paired_transfers(movements), successions)
        ),
        key=lambda m: (m.trade_date, _effect_rank(m.effect), m.id or 0),
    )


#: Total "Atualização" quantity per (asset, day) — see :func:`sync_totals`.
SyncTotals = dict[tuple[int, date], Decimal]


def sync_totals(movements: list[Movement]) -> SyncTotals:
    """Sum the ``Atualização`` quantities reported for each asset and day.

    B3 states them per broker, so a position split across two custodians
    produces two rows that must be judged together.
    """
    totals: SyncTotals = defaultdict(Decimal)
    for mv in movements:
        if mv.effect == PositionEffect.QTY_SYNC.value:
            totals[(mv.asset_id, mv.trade_date)] += mv.quantity or ZERO
    return totals


def _release_uncosted(position: Position, qty: Decimal) -> None:
    """Shrink the uncosted share when quantity leaves other than by a sale.

    Uncosted units are not a separate holding — they are a *proportion* of the
    position — so anything that removes quantity has to take its share of them
    with it, or the proportion drifts and a later sale attributes proceeds to
    units that are no longer there.
    """
    if position.uncosted_quantity <= ZERO or position.quantity <= ZERO:
        return
    share = min(qty / position.quantity, Decimal(1))
    position.uncosted_quantity -= position.uncosted_quantity * share


def _restore_staked(position: Position) -> None:
    """Put whatever is still in Earn back into the position it belongs to.

    Coins in Simple Earn or staking leave the *reported* balance — which is why
    applying the movement is what reconciles with the exchange — but they have
    not gone anywhere. Leaving them out closes the position on paper: a balance
    of 1.453 USDT earning interest showed up as no position at all, because
    every unit of it had been subscribed and never redeemed.

    Coins *withdrawn* are the opposite case and stay out: once they leave the
    exchange, nothing here knows what became of them. Their cost waits in
    ``parked_cost`` in case they come back.
    """
    if position.parked_quantity > DUST_QUANTITY:
        position.notes.append(
            f"{position.parked_quantity:.8f} unidades foram sacadas para fora da corretora e "
            f"não fazem parte da carteira; o custo correspondente ({position.parked_cost:.2f}) "
            "fica reservado caso voltem"
        )
    if position.staked_quantity <= QTY_EPSILON:
        return
    position.quantity += position.staked_quantity
    position.cost_basis += position.staked_cost
    position.notes.append(
        f"{position.staked_quantity:.8f} unidades estão aplicadas em staking/Simple Earn "
        f"(custo {position.staked_cost:.2f}); continuam na carteira, mas fora do saldo "
        "livre informado pela corretora"
    )


def apply_movement(position: Position, mv: Movement, totals: SyncTotals | None = None) -> None:
    """Apply one movement to a position (in place).

    ``totals`` is only needed to resolve :attr:`PositionEffect.QTY_SYNC`.
    """
    position.transactions += 1
    position.fees += mv.fees
    position.taxes += mv.taxes
    if mv.trade_date is not None:
        if position.first_trade is None or mv.trade_date < position.first_trade:
            position.first_trade = mv.trade_date
        if position.last_trade is None or mv.trade_date > position.last_trade:
            position.last_trade = mv.trade_date

    effect = mv.effect
    qty = mv.quantity or ZERO

    if effect == PositionEffect.ACQUIRE.value:
        position.quantity += qty
        position.cost_basis += mv.net_cost
        position.total_bought_qty += qty
        position.total_bought_amount += mv.net_cost

    elif effect == PositionEffect.DISPOSE.value:
        proceeds = mv.net_proceeds
        sold = qty if qty > ZERO else ZERO
        if position.quantity <= QTY_EPSILON:
            # Selling something the history never bought (missing older export).
            # Treat the whole proceeds as realised and flag it for review.
            position.realized_pnl += proceeds
            position.warnings.append(
                f"{mv.trade_date}: disposal of {sold} units with no recorded position; "
                "proceeds booked entirely as realised result"
            )
            position.quantity = ZERO
            position.cost_basis = ZERO
        else:
            if sold > position.quantity:
                position.warnings.append(
                    f"{mv.trade_date}: disposal of {sold} units exceeds the held {position.quantity}; "
                    "capped to the available position"
                )
                sold = position.quantity
            # A position can hold two kinds of quantity at once: units that were
            # bought, and units that arrived from outside with no purchase
            # behind them. A sale draws on both in proportion, and only the
            # first kind can produce a result — for the second there is nothing
            # to subtract, so its share of the proceeds is set aside instead of
            # being counted as a gain it may not be.
            uncosted_sold = ZERO
            if position.uncosted_quantity > ZERO and position.quantity > ZERO:
                uncosted_sold = sold * (position.uncosted_quantity / position.quantity)
            costed_sold = sold - uncosted_sold
            costed_held = position.quantity - position.uncosted_quantity
            cost_removed = (
                position.cost_basis * (costed_sold / costed_held) if costed_held > ZERO else ZERO
            )
            costed_proceeds = proceeds * (costed_sold / sold) if sold > ZERO else proceeds

            position.quantity -= sold
            position.uncosted_quantity -= uncosted_sold
            position.cost_basis -= cost_removed
            position.realized_pnl += costed_proceeds - cost_removed
            position.uncosted_proceeds += proceeds - costed_proceeds
            if position.quantity <= QTY_EPSILON:
                # Close out float dust so the asset reads as fully sold.
                position.quantity = ZERO
                position.cost_basis = ZERO
                position.uncosted_quantity = ZERO
        position.total_sold_qty += sold
        position.total_sold_amount += proceeds

    elif effect == PositionEffect.QTY_IN_FREE.value:
        position.quantity += qty

    elif effect == PositionEffect.QTY_SYNC.value:
        # "Atualização": either shares being credited, or B3 restating the
        # position it already knows about (which happens whenever custody
        # migrates between brokers). Decide by comparing the day's total for
        # the asset against what is currently held: an exact match is a
        # restatement — anything else is a real credit.
        held = position.quantity
        day_total = totals.get((mv.asset_id, mv.trade_date), qty) if totals is not None else qty
        if abs(day_total - held) <= QTY_EPSILON:
            position.notes.append(
                f"{mv.trade_date}: 'Atualização' of {qty} (day total {day_total}) matches the "
                f"{held} already held; treated as a position restatement, not applied"
            )
        else:
            position.quantity += qty
            position.notes.append(
                f"{mv.trade_date}: 'Atualização' of {qty} (day total {day_total}) differs from "
                f"the {held} held; applied as a free quantity credit"
            )

    elif effect == PositionEffect.QTY_RESTATE.value:
        # Share restructuring: the quantity replaces the position, the cost
        # basis rides along untouched, so the average price rescales by the
        # restructuring ratio.
        previous = position.quantity
        position.quantity = qty
        position.notes.append(
            f"{mv.trade_date}: grupamento + desdobramento: position restated from "
            f"{previous} to {qty}; cost basis preserved and average price rescaled"
        )

    elif effect == PositionEffect.REALIZE.value:
        # Fraction auction: the shares already left via "Fração em Ativos", so
        # only the proceeds land here, netted against the cost that left with
        # them.
        proceeds = mv.net_proceeds
        position.realized_pnl += proceeds - position.pending_fraction_cost
        position.total_sold_amount += proceeds
        position.pending_fraction_cost = ZERO

    elif effect == PositionEffect.QTY_OUT_PARKED.value:
        # Into staking / Simple Earn, or out to a wallet: the exchange stops
        # reporting the coins in the balance, but they are still owned. Their
        # cost is set aside rather than removed, so the round trip is
        # cost-neutral.
        removed = min(qty, position.quantity) if position.quantity > ZERO else ZERO
        if removed > ZERO:
            moved = position.cost_basis * (removed / position.quantity)
            position.cost_basis -= moved
            position.parked_cost += moved
            position.parked_quantity += removed
        _release_uncosted(position, qty)
        position.quantity -= qty
        if abs(position.quantity) <= QTY_EPSILON:
            position.quantity = ZERO
            position.uncosted_quantity = ZERO

    elif effect == PositionEffect.QTY_OUT_STAKED.value:
        # Into Simple Earn / staking. Same bookkeeping as a withdrawal, into a
        # different bucket — because these coins never left the exchange and
        # are folded back into the position at the end of the replay.
        removed = min(qty, position.quantity) if position.quantity > ZERO else ZERO
        if removed > ZERO:
            moved = position.cost_basis * (removed / position.quantity)
            position.cost_basis -= moved
            position.staked_cost += moved
            position.staked_quantity += removed
        _release_uncosted(position, qty)
        position.quantity -= qty
        if abs(position.quantity) <= QTY_EPSILON:
            position.quantity = ZERO
            position.uncosted_quantity = ZERO

    elif effect == PositionEffect.QTY_IN_STAKED.value:
        position.quantity += qty
        reclaimed = min(qty, position.staked_quantity) if position.staked_quantity > ZERO else ZERO
        if reclaimed > ZERO:
            returned = position.staked_cost * (reclaimed / position.staked_quantity)
            position.cost_basis += returned
            position.staked_cost -= returned
            position.staked_quantity -= reclaimed
        # More can come back than went in: rewards compound inside the product.
        # That excess is free quantity, not an untraceable external deposit.

    elif effect == PositionEffect.QTY_IN_PARKED.value:
        # Back out again, carrying its share of what was set aside. More can
        # come back than went in, because rewards compound inside the product —
        # the excess arrived free and is treated that way.
        position.quantity += qty
        reclaimed = min(qty, position.parked_quantity) if position.parked_quantity > ZERO else ZERO
        if reclaimed > ZERO:
            returned = position.parked_cost * (reclaimed / position.parked_quantity)
            position.cost_basis += returned
            position.parked_cost -= returned
            position.parked_quantity -= reclaimed
        # Whatever came back beyond what left has no purchase behind it: either
        # a reward compounded inside the product, or coins bought somewhere this
        # history cannot see. Both are genuinely uncosted; the difference is
        # only that the second one matters, so it is counted rather than
        # silently valued at zero.
        position.uncosted_quantity += qty - reclaimed

    elif effect == PositionEffect.LEDGER_ONLY.value:
        # Internal custody transfer: the matching credit/debit cancel out, so
        # the portfolio position and its cost basis are deliberately untouched.
        pass

    elif effect == PositionEffect.QTY_OUT_FREE.value:
        removed = min(qty, position.quantity) if position.quantity > ZERO else ZERO
        if position.quantity > ZERO and removed > ZERO:
            cost_removed = position.cost_basis * (removed / position.quantity)
            position.cost_basis -= cost_removed
            if mv.op_type == OperationType.FRACTION.value:
                # Held until B3 auctions the fraction and pays for it, so the
                # auction can be booked net of what it actually cost.
                position.pending_fraction_cost += cost_removed
        _release_uncosted(position, qty)
        position.quantity -= qty
        if position.quantity < ZERO:
            position.warnings.append(
                f"{mv.trade_date}: removal of {qty} units left a negative position "
                f"({position.quantity}); the export may be missing earlier movements"
            )
        if abs(position.quantity) <= QTY_EPSILON:
            position.quantity = ZERO
            position.cost_basis = ZERO
            position.uncosted_quantity = ZERO

    elif effect == PositionEffect.QTY_EXPIRE.value:
        # A right that was never exercised is worth nothing the day after the
        # deadline. B3 records the event but leaves the quantity empty, so an
        # unquantified row expires the whole remaining right — which is what
        # "não exercido" means once the window has closed. Left as a plain
        # debit of zero it removes nothing, and the expired rights pile up as a
        # position that cannot be sold, priced or converted.
        expired = qty if qty > ZERO else position.quantity
        expired = min(expired, position.quantity) if position.quantity > ZERO else ZERO
        if expired > ZERO:
            cost_lost = position.cost_basis * (expired / position.quantity)
            position.cost_basis -= cost_lost
            # Whatever it cost is gone: an expired right pays nothing back.
            position.realized_pnl -= cost_lost
            _release_uncosted(position, expired)
            position.quantity -= expired
            position.notes.append(
                f"{mv.trade_date}: {expired:f} direito(s) expiraram sem exercício"
            )
        if abs(position.quantity) <= QTY_EPSILON:
            position.quantity = ZERO
            position.cost_basis = ZERO
            position.uncosted_quantity = ZERO

    elif effect == PositionEffect.CASH_IN.value:
        if mv.op_type == OperationType.TAX.value:
            # Withholding released when a distribution is reclassified (US
            # brokers do this months later, as "NRA ADJ"). It gives tax back —
            # counting it as income would inflate dividends received.
            position.income_tax -= mv.gross_amount
        elif mv.op_type == OperationType.FEE.value:
            position.fees -= mv.gross_amount
        else:
            position.income += mv.gross_amount
            position.income_by_type[_income_bucket(mv.op_type)] += mv.gross_amount

    elif effect == PositionEffect.CASH_OUT.value:
        if mv.op_type == OperationType.TAX.value:
            position.income_tax += mv.gross_amount
        elif mv.op_type == OperationType.FEE.value:
            position.fees += mv.gross_amount
        else:
            # Reversal of a previously credited income (e.g. transferred yields).
            position.income -= mv.gross_amount
            position.income_by_type[_income_bucket(mv.op_type)] -= mv.gross_amount

    elif effect == PositionEffect.RETURN_OF_CAPITAL.value:
        amount = mv.gross_amount
        position.returned_capital += amount
        if amount <= position.cost_basis:
            position.cost_basis -= amount
        else:
            position.realized_pnl += amount - position.cost_basis
            position.cost_basis = ZERO

    # PositionEffect.NONE -> audit trail only.


def build_positions(
    movements: list[Movement], successions: Sequence[Succession] = ()
) -> dict[int, Position]:
    """Replay every movement in chronological order.

    Successions are applied at the end of their effective day, so the cost they
    carry over is the balance left after that day's own movements.
    """
    ordered = _prepare(movements, successions)
    positions: dict[int, Position] = {}
    totals = sync_totals(ordered)
    pending = _successions_by_day(successions)

    index = 0
    for day in sorted({m.trade_date for m in ordered} | set(pending)):
        while index < len(ordered) and ordered[index].trade_date == day:
            mv = ordered[index]
            index += 1
            position = positions.get(mv.asset_id)
            if position is None:
                position = Position(asset_id=mv.asset_id)
                positions[mv.asset_id] = position
            apply_movement(position, mv, totals)
        for succession in pending.get(day, ()):
            apply_succession(positions, succession)

    # Quantity that went into a staking product and never came back out is
    # still owned, but the export stopped reporting it — so it sits outside
    # every total. Said out loud rather than left implicit: the alternative is
    # capital quietly missing from the portfolio with nothing to explain it.
    for position in positions.values():
        _restore_staked(position)
        # A warning, not a note: it says a headline number is wrong. Proceeds
        # from quantity that never had a purchase are booked in full as a gain,
        # and there is no way to know what they actually cost.
        if position.uncosted_quantity > DUST_QUANTITY:
            position.warnings.append(
                f"{position.uncosted_quantity:.8f} unidades entraram por depósito externo sem "
                "compra correspondente no histórico; o custo é desconhecido, então elas "
                "aparecem com custo zero e o lucro não pode ser apurado sobre elas"
            )
        if position.uncosted_proceeds > MONEY_DUST:
            position.warnings.append(
                f"{position.uncosted_proceeds:.2f} recebidos na venda de unidades sem custo "
                "conhecido; fora do resultado realizado, porque um resultado é receita "
                "menos custo e aqui não há custo a subtrair"
            )
    return positions


def _effect_rank(effect: str) -> int:
    """Within one day, credits are applied before debits.

    B3 exports have no intraday ordering, so a same-day buy+sell must be
    applied buy-first — otherwise the sale would hit an empty position.
    """
    order = {
        # A restructuring replaces the position, so it must land before the
        # day's other movements (notably the fraction removal that follows it).
        PositionEffect.QTY_RESTATE.value: -1,
        PositionEffect.ACQUIRE.value: 0,
        PositionEffect.QTY_IN_FREE.value: 1,
        # Coins coming back from staking land with the day's other credits, and
        # must precede any debit that spends them.
        PositionEffect.QTY_IN_PARKED.value: 1,
        PositionEffect.QTY_IN_STAKED.value: 1,
        # Applied after the day's credits so a restatement is compared against
        # the position that already includes them.
        PositionEffect.LEDGER_ONLY.value: 2,
        PositionEffect.QTY_SYNC.value: 3,
        PositionEffect.CASH_IN.value: 4,
        PositionEffect.RETURN_OF_CAPITAL.value: 5,
        PositionEffect.DISPOSE.value: 6,
        PositionEffect.QTY_OUT_FREE.value: 7,
        PositionEffect.QTY_OUT_PARKED.value: 7,
        PositionEffect.QTY_OUT_STAKED.value: 7,
        # After the day's other debits: an expiry with no stated quantity
        # sweeps whatever is left, so it has to see the final balance.
        PositionEffect.QTY_EXPIRE.value: 8,
        PositionEffect.REALIZE.value: 8,
        PositionEffect.CASH_OUT.value: 8,
        PositionEffect.NONE.value: 9,
    }
    return order.get(effect, 10)


@dataclass(slots=True)
class TimelinePoint:
    """Portfolio state at the end of a given day (market value excluded)."""

    day: date
    cost_basis: Decimal
    quantities: dict[int, Decimal]
    costs: dict[int, Decimal]
    invested_flow: Decimal  # cumulative net cash put into assets
    dividends: Decimal  # cumulative income
    realized: Decimal  # cumulative realised result
    #: The last two figures split per asset, zeros omitted. Carried so a caller
    #: can group the running result by something it knows about the asset —
    #: class, broker, currency — which the engine deliberately does not. Closed
    #: positions stay in here: their realised result and the income they paid
    #: are part of the portfolio's history for good.
    realized_by_asset: dict[int, Decimal] = field(default_factory=dict)
    income_by_asset: dict[int, Decimal] = field(default_factory=dict)


def build_timeline(
    movements: list[Movement], successions: Sequence[Succession] = ()
) -> list[TimelinePoint]:
    """Cumulative state after each day that has at least one movement."""
    ordered = _prepare(movements, successions)
    positions: dict[int, Position] = {}
    totals = sync_totals(ordered)
    pending = _successions_by_day(successions)
    invested_flow = ZERO
    timeline: list[TimelinePoint] = []

    def emit(day: date) -> None:
        # Staked quantity counts as held. Coins moved into an exchange's Earn
        # product leave ``Position.quantity`` but are still owned, and a
        # timeline that drops them draws the move as the portfolio losing that
        # value and getting it back days later — a round trip through staking
        # would otherwise print as a crash followed by a rally.
        held = {
            aid: (p.quantity + p.staked_quantity, p.cost_basis + p.staked_cost)
            for aid, p in positions.items()
            if p.quantity + p.staked_quantity > QTY_EPSILON
        }
        quantities = {aid: quantity for aid, (quantity, _cost) in held.items()}
        costs = {aid: cost for aid, (_quantity, cost) in held.items()}
        timeline.append(
            TimelinePoint(
                day=day,
                cost_basis=sum(costs.values(), ZERO),
                quantities=quantities,
                costs=costs,
                invested_flow=invested_flow,
                # Net of withholding: what actually reached the account is what
                # the result curves are built from.
                dividends=sum((p.income - p.income_tax for p in positions.values()), ZERO),
                realized=sum((p.realized_pnl for p in positions.values()), ZERO),
                realized_by_asset={
                    aid: p.realized_pnl for aid, p in positions.items() if p.realized_pnl
                },
                income_by_asset={
                    aid: p.income - p.income_tax
                    for aid, p in positions.items()
                    if p.income or p.income_tax
                },
            )
        )

    index = 0
    for day in sorted({m.trade_date for m in ordered} | set(pending)):
        while index < len(ordered) and ordered[index].trade_date == day:
            mv = ordered[index]
            index += 1
            position = positions.setdefault(mv.asset_id, Position(asset_id=mv.asset_id))
            if mv.effect == PositionEffect.ACQUIRE.value:
                invested_flow += mv.net_cost
            elif mv.effect == PositionEffect.DISPOSE.value:
                invested_flow -= mv.net_proceeds
            apply_movement(position, mv, totals)
        for succession in pending.get(day, ()):
            # Cash received in the event leaves the portfolio's asset side.
            invested_flow -= succession.cash_amount
            apply_succession(positions, succession)
        emit(day)
    return timeline
