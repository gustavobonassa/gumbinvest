"""Corporate actions: linking an asset to the one that replaced it.

The B3 export has a systematic blind spot. When a company is merged, renamed or
restructured, B3 **credits the new ticker and never debits the old one** — there
is no row anywhere linking the two. Replayed literally, that leaves:

* the predecessor holding a phantom position carrying the entire cost basis;
* the successor holding shares that arrived for free, so its average price is
  far below what was actually paid;
* sometimes an intermediate vehicle (holding units handed out mid-merger and
  redeemed for cash days later) whose zero-cost redemption books an invented
  realised gain.

Two real B3 event shapes that shaped this module:

* A **ticker rename**: B3 credits the new code with a position exactly
  matching the old one as an ``Atualização`` — and leaves the old code open.
* An **incorporation**: B3 credits the successor as ``Incorporação``, often
  alongside an intermediate holding vehicle (units credited with exactly the
  absorbed position and redeemed for cash days later). The absorbed ticker
  stays open throughout.

This module *proposes* the links from that evidence; it never applies them on
its own. The export cannot distinguish "the successor" from "an intermediate
vehicle credited on the same day", and picking wrong silently would produce
confident, wrong numbers. Applying is a decision recorded in
:class:`~app.db.models.AssetSuccession`.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Asset, AssetSuccession, Transaction
from app.domain.enums import OperationType, PositionEffect
from app.portfolio.engine import Succession

ZERO = Decimal(0)

#: A merged company simply stops trading, and the event can land months after
#: the last purchase — a paper bought mid-year may only be absorbed near its
#: end. So the search runs long, and precision comes from the movement types
#: below rather than from a narrow window.
WINDOW_DAYS = 400

#: Beyond this many days, only an unambiguous restructuring row still counts —
#: otherwise every routine ``Atualização`` in the following year becomes a
#: candidate.
NEAR_DAYS = 45

#: Effects that mean "quantity appeared without being bought" — the signature of
#: a corporate-action credit, and the only kind of row a successor can arrive on.
FREE_CREDIT_EFFECTS = {
    PositionEffect.QTY_IN_FREE.value,
    PositionEffect.QTY_SYNC.value,
    PositionEffect.QTY_RESTATE.value,
}

#: Movements that genuinely replace one line with another. Subscription rights
#: and receipts are deliberately absent: they are credited constantly and never
#: succeed anything.
RESTRUCTURING_TYPES = {
    OperationType.MERGER.value,
    OperationType.POSITION_UPDATE.value,
}


def load_successions(db: Session, portfolio_id: int) -> list[Succession]:
    """The engine-level view of what the user has declared."""
    rows = db.scalars(
        select(AssetSuccession)
        .where(AssetSuccession.portfolio_id == portfolio_id)
        .order_by(AssetSuccession.effective_date)
    ).all()
    return [
        Succession(
            from_asset_id=row.from_asset_id,
            to_asset_id=row.to_asset_id,
            effective_date=row.effective_date,
            cash_amount=Decimal(row.cash_amount or 0),
        )
        for row in rows
    ]


def _candidate_rows(db: Session, portfolio_id: int, asset_id: int, start, end) -> list[Transaction]:
    """Restructuring credits to *other* assets in the window around the event."""
    return list(
        db.scalars(
            select(Transaction)
            .where(
                Transaction.portfolio_id == portfolio_id,
                Transaction.asset_id != asset_id,
                Transaction.direction == "CREDIT",
                Transaction.effect.in_(list(FREE_CREDIT_EFFECTS)),
                Transaction.op_type.in_(list(RESTRUCTURING_TYPES)),
                Transaction.trade_date >= start,
                Transaction.trade_date <= end,
            )
            .order_by(Transaction.trade_date)
        ).all()
    )


def suggest_successions(db: Session, portfolio_id: int) -> list[dict]:
    """Propose a successor for every stranded position.

    A position is *stranded* when it is still open, has no market price of its
    own to justify staying open, and stopped moving — while some other asset
    received free quantity right afterwards. Candidates are ranked by how well
    the evidence fits, an exact quantity match being the strongest signal there
    is: B3 credits the successor with precisely the position being replaced.
    """
    from app.portfolio.service import NON_MARKET_KINDS, PortfolioService  # local import avoids a cycle

    service = PortfolioService(db, portfolio_id)
    assets = service.assets()
    declared = {
        row.from_asset_id
        for row in db.scalars(
            select(AssetSuccession).where(AssetSuccession.portfolio_id == portfolio_id)
        ).all()
    }

    positions = service.positions()
    suggestions: list[dict] = []
    for asset_position in service.asset_positions():
        position = asset_position.position
        asset = asset_position.asset
        # Only the positions the dashboard actually complains about: an open
        # holding that nothing prices. Families with no market by nature
        # (subscription rights, options) are never stranded mergers.
        if asset.id in declared or asset_position.has_market_price or position.last_trade is None:
            continue
        if asset.kind in NON_MARKET_KINDS:
            continue

        window_start = position.last_trade
        window_end = position.last_trade + timedelta(days=WINDOW_DAYS)
        candidates = []
        for row in _candidate_rows(db, portfolio_id, asset.id, window_start, window_end):
            target = assets.get(row.asset_id)
            if target is None:
                continue
            gap = (row.trade_date - window_start).days
            merger = row.op_type == OperationType.MERGER.value
            if gap > NEAR_DAYS and not merger:
                continue

            exact = row.quantity is not None and abs(Decimal(row.quantity) - position.quantity) < Decimal(
                "0.00000001"
            )
            target_position = positions.get(target.id)
            still_held = bool(target_position and target_position.is_open)
            candidates.append(
                {
                    "ticker": target.ticker,
                    "name": target.name,
                    "date": row.trade_date,
                    "movement": row.raw_movement,
                    "quantity": row.quantity,
                    "exact_quantity_match": exact,
                    "still_held": still_held,
                    # Cost has to land somewhere still held — a line that was
                    # itself closed was a pass-through, not the successor. That
                    # outranks even an exact quantity match, which a mid-merger
                    # holding vehicle also produces.
                    "score": (3 if still_held else 0) + (2 if exact else 0) + (1 if gap == 0 else 0),
                }
            )
        if not candidates:
            continue

        candidates.sort(key=lambda c: (-c["score"], c["date"]))
        suggestions.append(
            {
                "ticker": asset.ticker,
                "name": asset.name,
                "kind": asset.kind,
                "quantity": position.quantity,
                "cost_basis": position.cost_basis,
                "last_trade": position.last_trade,
                "candidates": candidates[:6],
            }
        )
    return suggestions
