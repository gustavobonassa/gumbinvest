"""Hand-entered movements: they must behave exactly like imported ones."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dates import local_today
from app.db.models import Asset, Transaction
from app.importer.service import reclassify_transactions
from app.portfolio.service import PortfolioService
from app.services import manual_entries
from app.services.portfolio_registry import get_default_portfolio


@pytest.fixture
def portfolio(db: Session):
    return get_default_portfolio(db)


def test_every_operation_survives_the_startup_reclassification(db: Session, portfolio):
    """The rule the whole design hangs on.

    ``reclassify_transactions`` re-derives ``op_type`` and ``effect`` from the
    raw movement label on every start. A manual entry that invented a label the
    classifier does not know would be silently rewritten to UNKNOWN/NONE — the
    movement would vanish from the position and nothing would say so.
    """
    for index, operation in enumerate(manual_entries.OPERATIONS):
        manual_entries.create(
            db,
            portfolio.id,
            operation=operation.code,
            ticker=f"TST{index}",
            when=date(2026, 1, 5),
            quantity=Decimal(10),
            unit_price=Decimal(20),
            amount=Decimal(200),
        )

    before = {
        row.id: (row.op_type, row.effect)
        for row in db.scalars(select(Transaction)).all()
    }
    reclassify_transactions(db)
    after = {
        row.id: (row.op_type, row.effect)
        for row in db.scalars(select(Transaction)).all()
    }
    assert after == before
    # And none of them landed on the "we could not read this" fallback.
    assert not [pair for pair in after.values() if pair[0] == "UNKNOWN" or pair[1] == "NONE"]


def test_the_catalogue_promises_what_the_entry_delivers(db: Session, portfolio):
    """The form tells the user what will happen; it must not be lying."""
    promised = {item["code"]: (item["op_type"], item["effect"]) for item in manual_entries.catalogue()}
    for operation in manual_entries.OPERATIONS:
        movement = manual_entries.create(
            db,
            portfolio.id,
            operation=operation.code,
            ticker="PROMESSA",
            when=date(2026, 1, 5),
            quantity=Decimal(5),
            unit_price=Decimal(10),
            amount=Decimal(50),
        )
        assert (movement.op_type, movement.effect) == promised[operation.code], operation.code


def test_a_manual_purchase_reaches_the_position(db: Session, portfolio):
    manual_entries.create(
        db, portfolio.id, operation="BUY", ticker="PETR4", when=date(2026, 1, 5),
        quantity=Decimal(100), unit_price=Decimal("30.00"), name="PETROLEO BRASILEIRO",
    )
    manual_entries.create(
        db, portfolio.id, operation="DIVIDEND", ticker="PETR4", when=date(2026, 2, 5),
        amount=Decimal("150.00"),
    )
    asset = db.scalar(select(Asset).where(Asset.ticker == "PETR4"))
    position = PortfolioService(db, portfolio.id).positions()[asset.id]

    assert position.quantity == Decimal(100)
    assert position.cost_basis == Decimal("3000.00")
    assert position.income == Decimal("150.00")
    # A ticker nobody imported is still created, and classified.
    assert asset.name == "PETROLEO BRASILEIRO"
    assert asset.kind == "STOCK"


def test_an_explicit_total_wins_over_the_multiplication(db: Session, portfolio):
    """A broker note rounds its own way; its figure is the one that reconciles."""
    movement = manual_entries.create(
        db, portfolio.id, operation="BUY", ticker="MGLU3", when=date(2026, 1, 5),
        quantity=Decimal(3), unit_price=Decimal("10.333"), amount=Decimal("31.00"),
    )
    assert movement.gross_amount == Decimal("31.00")


def test_only_hand_entered_rows_can_be_deleted(db: Session, portfolio):
    mine = manual_entries.create(
        db, portfolio.id, operation="BUY", ticker="VALE3", when=date(2026, 1, 5),
        quantity=Decimal(10), unit_price=Decimal(60),
    )
    assert manual_entries.is_manual(mine)
    manual_entries.delete(db, portfolio.id, mine.id)
    assert db.get(Transaction, mine.id) is None

    imported = Transaction(
        portfolio_id=portfolio.id,
        asset_id=db.scalar(select(Asset.id).where(Asset.ticker == "VALE3")),
        trade_date=date(2026, 1, 6),
        direction="CREDIT", op_type="BUY", effect="ACQUIRE",
        quantity=Decimal(1), unit_price=Decimal(1), gross_amount=Decimal(1),
        net_amount=Decimal(1), raw_movement="Compra", raw_product="VALE3",
        dedup_key="sha256:deadbeef", occurrence=0,
    )
    db.add(imported)
    db.commit()
    assert not manual_entries.is_manual(imported)
    with pytest.raises(manual_entries.ManualEntryError, match="importado"):
        manual_entries.delete(db, portfolio.id, imported.id)
    assert db.get(Transaction, imported.id) is not None


def test_the_obvious_mistakes_are_refused(db: Session, portfolio):
    with pytest.raises(manual_entries.ManualEntryError, match="desconhecida"):
        manual_entries.create(db, portfolio.id, operation="NOPE", ticker="X", when=date(2026, 1, 5))
    with pytest.raises(manual_entries.ManualEntryError, match="futuro"):
        manual_entries.create(
            db, portfolio.id, operation="BUY", ticker="X", when=local_today() + timedelta(days=1),
            quantity=Decimal(1), unit_price=Decimal(1),
        )
    with pytest.raises(manual_entries.ManualEntryError, match="quantidade"):
        manual_entries.create(db, portfolio.id, operation="BUY", ticker="X", when=date(2026, 1, 5))
    with pytest.raises(manual_entries.ManualEntryError, match="valor"):
        manual_entries.create(db, portfolio.id, operation="DIVIDEND", ticker="X", when=date(2026, 1, 5))
