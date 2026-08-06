"""Movements the user types in, for everything no export reaches.

An importer covers the brokers that publish a file. Everything else — a trade on
a platform with no export, a dividend that never made it into the CSV, a
correction — has to be enterable by hand, or the portfolio is only as complete
as the files that happen to exist.

Two rules keep hand-entered rows from corrupting the ledger:

* **They are written in the importer's own vocabulary.** Each operation carries
  a raw movement label that :mod:`app.importer.classifier` already knows, and
  the ``op_type``/``effect`` are *derived* by calling that classifier rather
  than being spelled out here. Anything else would drift the moment a rule
  changes — and the reclassification that runs on every start would silently
  rewrite the entry into something the user never asked for.
* **They are marked, and only they can be deleted.** An imported row is
  reproducible from its file and belongs to the audit trail; deleting one would
  either come back on the next upload or quietly change history. A manual row
  has no file behind it, so removing it is the only way to fix a typo.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dates import local_today
from app.db.models import Asset, Broker, Transaction
from app.domain.enums import Direction
from app.importer.classifier import classify
from app.importer.parser import classify_asset_kind
# Which effects move cash, and in which direction, is decided in exactly one
# place. Restating it here is how a manual purchase ended up booking a net
# amount of zero while an imported one booked a negative.
from app.importer.service import _net_amount as net_amount_for

ZERO = Decimal(0)

#: Marks a movement as hand-entered. Also used by the bank-balance entries in
#: :mod:`app.portfolio.accounts`, which are manual movements by another name.
MANUAL_PREFIX = "manual:"


class ManualEntryError(ValueError):
    """Something the user can fix — reported as a 400, not a crash."""


@dataclass(frozen=True, slots=True)
class Operation:
    """One entry in the "what happened" dropdown.

    ``movement`` is the label the ledger stores, and it must be one the
    classifier resolves — that is what makes a manual row survive the start-up
    reclassification untouched. ``needs`` tells the form which fields to ask
    for; it is served to the UI so the two never disagree.
    """

    code: str
    label: str
    movement: str
    direction: Direction
    #: "trade" = quantity × price, "quantity" = units only, "amount" = cash only.
    needs: str
    hint: str = ""


OPERATIONS: tuple[Operation, ...] = (
    Operation("BUY", "Compra", "Compra", Direction.CREDIT, "trade"),
    Operation("SELL", "Venda", "Venda", Direction.DEBIT, "trade"),
    Operation("DIVIDEND", "Dividendo", "Dividendo", Direction.CREDIT, "amount"),
    Operation("JCP", "Juros sobre capital próprio", "Juros Sobre Capital Próprio", Direction.CREDIT, "amount"),
    Operation("YIELD", "Rendimento", "Rendimento", Direction.CREDIT, "amount"),
    Operation("INTEREST", "Juros", "Juros", Direction.CREDIT, "amount"),
    Operation(
        "AMORTIZATION", "Amortização", "Amortização", Direction.CREDIT, "amount",
        "Devolução de capital: reduz o preço médio em vez de contar como renda.",
    ),
    Operation(
        "TAX", "Imposto retido", "IRRF", Direction.DEBIT, "amount",
        "Descontado dos proventos em todo resultado da carteira.",
    ),
    Operation(
        "FEE", "Taxa / corretagem", "Taxa", Direction.DEBIT, "amount",
        "Custo de negociar: reportado, nunca abatido dos proventos.",
    ),
    Operation(
        "SPLIT", "Desdobramento", "Desdobro", Direction.CREDIT, "quantity",
        "Quantidade nova sem custo — o preço médio se dilui sozinho.",
    ),
    Operation("REVERSE_SPLIT", "Grupamento", "Grupamento", Direction.DEBIT, "quantity"),
    Operation("BONUS", "Bonificação", "Bonificação em Ativos", Direction.CREDIT, "quantity"),
    Operation("TRANSFER_IN", "Transferência recebida", "Transferência", Direction.CREDIT, "quantity"),
    Operation("TRANSFER_OUT", "Transferência enviada", "Transferência", Direction.DEBIT, "quantity"),
    Operation(
        "REDEMPTION", "Resgate / vencimento", "Resgate", Direction.DEBIT, "trade",
        "Encerra a posição realizando o resultado contra o preço médio.",
    ),
)

_BY_CODE = {operation.code: operation for operation in OPERATIONS}


def catalogue() -> list[dict]:
    """The operations a manual entry may use, for the form to build itself.

    Each one is resolved through the classifier, so what the form promises and
    what the engine will do are the same thing by construction. The nominal
    amount matters: a disposal with no cash attached is downgraded to a free
    exit, which is right for a maturity row the export left blank and wrong as
    a description of a sale the user is about to type an amount into.
    """
    return [
        {
            "code": operation.code,
            "label": operation.label,
            "needs": operation.needs,
            "hint": operation.hint or None,
            **{
                "op_type": resolved.op_type.value,
                "effect": resolved.effect.value,
            },
        }
        for operation in OPERATIONS
        for resolved in (classify(operation.movement, operation.direction, Decimal(1)),)
    ]


def is_manual(movement: Transaction) -> bool:
    return bool(movement.dedup_key and movement.dedup_key.startswith(MANUAL_PREFIX))


def _asset_for(db: Session, ticker: str, name: str, kind: str | None, currency: str) -> Asset:
    ticker = ticker.strip().upper()
    if not ticker:
        raise ManualEntryError("informe o ticker do ativo")
    asset = db.scalar(select(Asset).where(Asset.ticker == ticker))
    if asset is not None:
        if name and not asset.name:
            asset.name = name[:255]
        return asset
    return _create_asset(db, ticker, name, kind, currency)


def _create_asset(db: Session, ticker: str, name: str, kind: str | None, currency: str) -> Asset:
    """A ticker nobody imported yet is still a real asset — create it."""
    from app.market.service import resolve_market_symbol

    asset = Asset(
        ticker=ticker,
        name=(name or ticker)[:255],
        kind=kind or classify_asset_kind(ticker, name or ticker).value,
        currency=(currency or "BRL").upper(),
    )
    db.add(asset)
    db.flush()
    asset.market_symbol = resolve_market_symbol(asset)
    return asset


def _broker_for(db: Session, name: str | None) -> Broker | None:
    canonical = (name or "").strip()
    if not canonical:
        return None
    broker = db.scalar(select(Broker).where(Broker.canonical_name == canonical))
    if broker is None:
        broker = Broker(canonical_name=canonical, raw_names=[])
        db.add(broker)
        db.flush()
    return broker


def create(
    db: Session,
    portfolio_id: int,
    *,
    operation: str,
    ticker: str,
    when: date,
    quantity: Decimal = ZERO,
    unit_price: Decimal = ZERO,
    amount: Decimal | None = None,
    fees: Decimal = ZERO,
    taxes: Decimal = ZERO,
    name: str = "",
    kind: str | None = None,
    currency: str = "BRL",
    broker: str | None = None,
    notes: str | None = None,
) -> Transaction:
    """Write one hand-entered movement into the ledger."""
    op = _BY_CODE.get(operation)
    if op is None:
        raise ManualEntryError(f"operação desconhecida: {operation}")
    if when > local_today():
        raise ManualEntryError("a data não pode estar no futuro")

    quantity = quantity or ZERO
    unit_price = unit_price or ZERO
    fees = fees or ZERO
    taxes = taxes or ZERO

    if op.needs in {"trade", "quantity"} and quantity <= ZERO:
        raise ManualEntryError("informe a quantidade")
    if op.needs == "amount" and (amount is None or amount <= ZERO):
        raise ManualEntryError("informe o valor")

    if op.needs == "amount":
        gross = Decimal(amount or 0)
    elif amount is not None and amount > ZERO:
        # An explicit total wins over the multiplication: a broker note rounds
        # its own way, and the figure on the note is the one that reconciles.
        gross = amount
        unit_price = unit_price or (gross / quantity if quantity else ZERO)
    else:
        gross = quantity * unit_price

    classification = classify(op.movement, op.direction, gross)
    effect = classification.effect.value
    net = net_amount_for(classification.effect, gross)

    asset = _asset_for(db, ticker, name, kind, currency)
    broker_row = _broker_for(db, broker)

    movement = Transaction(
        portfolio_id=portfolio_id,
        asset_id=asset.id,
        broker_id=broker_row.id if broker_row else None,
        trade_date=when,
        direction=op.direction.value,
        op_type=classification.op_type.value,
        effect=effect,
        quantity=quantity,
        unit_price=unit_price,
        gross_amount=gross,
        fees=fees,
        taxes=taxes,
        net_amount=net,
        currency=(asset.currency or "BRL").upper(),
        raw_movement=op.movement,
        raw_product=f"{asset.ticker} - {asset.name}",
        raw_institution=broker_row.canonical_name if broker_row else "",
        # A unique key, never a content hash: two identical purchases on the
        # same day are a normal thing to enter, and no importer may ever mistake
        # one of these for a row it owns.
        dedup_key=f"{MANUAL_PREFIX}{uuid.uuid4().hex}",
        occurrence=0,
        notes=(notes or None),
    )
    db.add(movement)
    db.commit()
    db.refresh(movement)
    return movement


def delete(db: Session, portfolio_id: int, transaction_id: int) -> None:
    """Remove a hand-entered movement. Imported rows are refused."""
    movement = db.scalar(
        select(Transaction).where(
            Transaction.id == transaction_id, Transaction.portfolio_id == portfolio_id
        )
    )
    if movement is None:
        raise ManualEntryError("lançamento não encontrado")
    if not is_manual(movement):
        raise ManualEntryError(
            "este lançamento veio de um arquivo importado e não pode ser excluído aqui — "
            "corrija o arquivo e reimporte, ou o histórico deixa de bater com a origem"
        )
    db.delete(movement)
    db.commit()
