"""Bank balances the user keeps by hand.

Money in a Nubank account is part of the net worth and it earns the CDI, but no
export reaches it. Rather than bolt a parallel "cash" concept onto every screen —
net worth, allocation, the rentabilidade chart, the history curve — an account is
modelled as what it behaves like: a **fixed income asset whose unit is one real**.

* one ``Asset`` per account, ``kind=FIXED_INCOME``, ``is_cash_account=True``;
* one ``FixedIncomeTerms`` row carrying the rate (100 % of CDI by default);
* one ``Transaction`` per movement — a deposit is a purchase of *amount* units
  at R$ 1,00, a withdrawal is a sale of the same.

Everything downstream then works unchanged: the replay tracks the balance, the
allocation counts it as renda fixa, and the accrual values it. The one rule that
differs from a paper is what a withdrawal does to the accrued interest, and that
lives in :func:`app.market.fixed_income.value_account`.
"""
from __future__ import annotations

import re
import uuid
from datetime import date

from app.core.dates import local_today
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Asset, FixedIncomeTerms, Transaction
from app.domain.enums import AssetKind, Direction, OperationType, PositionEffect
from app.market.fixed_income import value_account

ZERO = Decimal(0)
ONE = Decimal(1)

#: Prefix that makes an account's synthetic ticker recognisable at a glance in
#: the ledger, and impossible to confuse with a B3 one.
TICKER_PREFIX = "CONTA-"

#: Raw movement labels, mirroring the classifier entries that map them back to
#: BUY/ACQUIRE and SELL/DISPOSE (see app.importer.classifier).
DEPOSIT_LABEL = "Depósito em conta"
WITHDRAWAL_LABEL = "Saque em conta"


class AccountError(ValueError):
    """Something the user can fix — reported as a 4xx, not a crash."""


def slugify(name: str) -> str:
    """A ticker-shaped code for an account name ("Nubank" -> "CONTA-NUBANK")."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", name).upper()
    if not cleaned:
        raise AccountError("informe um nome para a conta")
    return f"{TICKER_PREFIX}{cleaned[:30]}"


def accounts(db: Session) -> list[Asset]:
    return list(
        db.scalars(
            select(Asset).where(Asset.is_cash_account.is_(True)).order_by(Asset.name, Asset.ticker)
        ).all()
    )


def get_account(db: Session, ticker: str) -> Asset:
    asset = db.scalar(
        select(Asset).where(Asset.ticker == ticker.upper(), Asset.is_cash_account.is_(True))
    )
    if asset is None:
        raise AccountError(f"conta {ticker} não encontrada")
    return asset


def create_account(
    db: Session,
    portfolio_id: int,
    name: str,
    *,
    index_code: str = "CDI",
    percent_of_index: Decimal = Decimal(100),
    opening_amount: Decimal | None = None,
    opening_date: date | None = None,
    notes: str | None = None,
) -> Asset:
    """Register an account, optionally with the balance it starts from."""
    name = name.strip()
    ticker = slugify(name)
    if db.scalar(select(Asset.id).where(Asset.ticker == ticker)) is not None:
        raise AccountError(f"já existe uma conta chamada {name}")

    asset = Asset(
        ticker=ticker,
        name=name,
        kind=AssetKind.FIXED_INCOME.value,
        currency="BRL",
        is_cash_account=True,
        notes=notes,
    )
    db.add(asset)
    db.flush()
    db.add(
        FixedIncomeTerms(
            asset_id=asset.id,
            index_code=index_code,
            percent_of_index=percent_of_index,
            spread_annual=ZERO,
            fixed_rate_annual=ZERO,
        )
    )
    if opening_amount and opening_amount > ZERO:
        add_entry(db, portfolio_id, asset, opening_amount, opening_date or local_today(), deposit=True)
    db.commit()
    db.refresh(asset)
    return asset


def update_account(
    db: Session,
    asset: Asset,
    *,
    name: str | None = None,
    index_code: str | None = None,
    percent_of_index: Decimal | None = None,
    notes: str | None = None,
) -> Asset:
    """Rename an account or change the rate it earns.

    The ticker is deliberately left alone: it is what the ledger's movements
    point at, and renaming "Nubank" to "Nu" must not orphan a year of entries.
    """
    if name is not None and name.strip():
        asset.name = name.strip()
    if notes is not None:
        asset.notes = notes or None
    terms = db.get(FixedIncomeTerms, asset.id)
    if terms is not None:
        if index_code is not None:
            terms.index_code = index_code
        if percent_of_index is not None:
            terms.percent_of_index = percent_of_index
    db.commit()
    db.refresh(asset)
    return asset


def delete_account(db: Session, asset: Asset) -> None:
    """Remove the account and everything that hangs off it (ON DELETE CASCADE)."""
    db.delete(asset)
    db.commit()


def add_entry(
    db: Session,
    portfolio_id: int,
    asset: Asset,
    amount: Decimal,
    when: date,
    *,
    deposit: bool,
    commit: bool = False,
) -> Transaction:
    """Record money going into or out of the account.

    The unit is one real, so quantity *is* the amount: the replay then tracks the
    balance with no special case, and the average price stays at 1,00 — which is
    what makes a withdrawal realise nothing. Interest is not a realised result
    here; it is the gap between the balance and what was put in, and it belongs
    to the money still in the account.
    """
    if amount is None or amount <= ZERO:
        raise AccountError("o valor precisa ser maior que zero")
    if when > local_today():
        raise AccountError("a data não pode estar no futuro")
    if not deposit:
        available = balance_on(db, portfolio_id, asset, when)
        if amount > available:
            raise AccountError(
                f"saldo insuficiente em {when.strftime('%d/%m/%Y')}: "
                f"disponível R$ {available:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            )

    movement = Transaction(
        portfolio_id=portfolio_id,
        asset_id=asset.id,
        trade_date=when,
        direction=(Direction.CREDIT if deposit else Direction.DEBIT).value,
        op_type=(OperationType.BUY if deposit else OperationType.SELL).value,
        effect=(PositionEffect.ACQUIRE if deposit else PositionEffect.DISPOSE).value,
        quantity=amount,
        unit_price=ONE,
        gross_amount=amount,
        fees=ZERO,
        taxes=ZERO,
        net_amount=amount if deposit else -amount,
        currency="BRL",
        raw_movement=DEPOSIT_LABEL if deposit else WITHDRAWAL_LABEL,
        raw_product=f"{asset.ticker} - {asset.name}",
        raw_institution=asset.name,
        # Manual entries carry a unique key rather than a content hash: two
        # deposits of the same amount on the same day are a normal thing to do,
        # and an importer must never mistake one of these for a row it owns.
        dedup_key=f"manual:{uuid.uuid4().hex}",
        occurrence=0,
    )
    db.add(movement)
    if commit:
        db.commit()
        db.refresh(movement)
    else:
        db.flush()
    return movement


def delete_entry(db: Session, portfolio_id: int, asset: Asset, entry_id: int) -> None:
    movement = db.scalar(
        select(Transaction).where(
            Transaction.id == entry_id,
            Transaction.asset_id == asset.id,
            Transaction.portfolio_id == portfolio_id,
        )
    )
    if movement is None:
        raise AccountError("lançamento não encontrado")
    db.delete(movement)
    db.commit()


def balance_on(db: Session, portfolio_id: int, asset: Asset, when: date) -> Decimal:
    """Accrued balance on ``when`` — what a withdrawal that day could take."""
    terms = db.get(FixedIncomeTerms, asset.id)
    if terms is None:
        return ZERO
    accrual = value_account(db, asset, terms, portfolio_id, through=when)
    return accrual.value if accrual else ZERO


def entries(db: Session, portfolio_id: int, asset: Asset) -> list[dict]:
    rows = db.execute(
        select(
            Transaction.id,
            Transaction.trade_date,
            Transaction.effect,
            Transaction.gross_amount,
        )
        .where(Transaction.portfolio_id == portfolio_id, Transaction.asset_id == asset.id)
        .order_by(Transaction.trade_date.desc(), Transaction.id.desc())
    ).all()
    return [
        {
            "id": row.id,
            "date": row.trade_date,
            "kind": "deposit" if row.effect == PositionEffect.ACQUIRE.value else "withdrawal",
            "amount": Decimal(row.gross_amount or 0),
        }
        for row in rows
    ]


def serialize(db: Session, portfolio_id: int, asset: Asset) -> dict:
    terms = db.get(FixedIncomeTerms, asset.id)
    accrual = value_account(db, asset, terms, portfolio_id) if terms else None
    rows = entries(db, portfolio_id, asset)
    first = db.scalar(
        select(func.min(Transaction.trade_date)).where(
            Transaction.portfolio_id == portfolio_id, Transaction.asset_id == asset.id
        )
    )
    return {
        "ticker": asset.ticker,
        "name": asset.name,
        "notes": asset.notes,
        "index_code": (terms.index_code if terms else "CDI") or "CDI",
        "percent_of_index": (terms.percent_of_index if terms else Decimal(100)),
        "since": first,
        "principal": accrual.principal if accrual else ZERO,
        "balance": accrual.value if accrual else ZERO,
        "interest": accrual.interest if accrual else ZERO,
        "yield_percent": accrual.yield_percent if accrual else ZERO,
        "business_days": accrual.business_days if accrual else 0,
        "stale": accrual.stale if accrual else False,
        "entries": rows,
    }


def overview(db: Session, portfolio_id: int) -> dict:
    items = [serialize(db, portfolio_id, asset) for asset in accounts(db)]
    return {
        "items": items,
        "totals": {
            "principal": sum((item["principal"] for item in items), ZERO),
            "balance": sum((item["balance"] for item in items), ZERO),
            "interest": sum((item["interest"] for item in items), ZERO),
        },
    }
