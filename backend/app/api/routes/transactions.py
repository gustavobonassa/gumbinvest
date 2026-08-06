"""Transaction browsing: filtering, sorting, pagination and CSV export."""
from __future__ import annotations

import csv
import io
from datetime import date

from app.core.dates import local_today
from typing import Literal

from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, or_, select

from app.api.deps import CurrentPortfolio, DbSession
from app.db.models import Asset, Broker, Transaction
from app.services import manual_entries

router = APIRouter(prefix="/transactions", tags=["transactions"])

SORTABLE = {
    "date": Transaction.trade_date,
    "ticker": Asset.ticker,
    "op_type": Transaction.op_type,
    "quantity": Transaction.quantity,
    "unit_price": Transaction.unit_price,
    "gross_amount": Transaction.gross_amount,
    "net_amount": Transaction.net_amount,
}


def _base_query(
    portfolio_id: int,
    search: str | None,
    ticker: str | None,
    op_types: list[str] | None,
    broker: str | None,
    start: date | None,
    end: date | None,
):
    stmt = (
        select(Transaction, Asset, Broker)
        .join(Asset, Asset.id == Transaction.asset_id)
        .outerjoin(Broker, Broker.id == Transaction.broker_id)
        .where(Transaction.portfolio_id == portfolio_id)
    )
    if ticker:
        stmt = stmt.where(Asset.ticker == ticker.upper())
    if op_types:
        stmt = stmt.where(Transaction.op_type.in_(op_types))
    if broker:
        stmt = stmt.where(Broker.canonical_name == broker)
    if start:
        stmt = stmt.where(Transaction.trade_date >= start)
    if end:
        stmt = stmt.where(Transaction.trade_date <= end)
    if search:
        needle = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Asset.ticker).like(needle),
                func.lower(Asset.name).like(needle),
                func.lower(Transaction.raw_movement).like(needle),
                func.lower(Transaction.raw_product).like(needle),
                cast(Transaction.trade_date, String).like(needle),
            )
        )
    return stmt


@router.get("", response_model=None, summary="Paginated, filterable transaction list")
def list_transactions(
    db: DbSession,
    portfolio: CurrentPortfolio,
    search: str | None = None,
    ticker: str | None = None,
    op_type: list[str] | None = Query(default=None),
    broker: str | None = None,
    start: date | None = None,
    end: date | None = None,
    sort: str = "date",
    order: Literal["asc", "desc"] = "desc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> dict:
    stmt = _base_query(portfolio.id, search, ticker, op_type, broker, start, end)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    column = SORTABLE.get(sort, Transaction.trade_date)
    stmt = stmt.order_by(column.asc() if order == "asc" else column.desc(), Transaction.id.desc())
    rows = db.execute(stmt.offset((page - 1) * page_size).limit(page_size)).all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "items": [
            {
                "id": t.id,
                "date": t.trade_date,
                "ticker": a.ticker,
                "name": a.name,
                "kind": a.kind,
                "op_type": t.op_type,
                "effect": t.effect,
                "movement": t.raw_movement,
                "direction": t.direction,
                "quantity": t.quantity,
                "unit_price": t.unit_price,
                "fees": t.fees,
                "taxes": t.taxes,
                "gross_amount": t.gross_amount,
                "net_amount": t.net_amount,
                "broker": b.canonical_name if b else None,
                "notes": t.notes,
                # Only a hand-entered row may be deleted from the UI: an
                # imported one belongs to its file. See app.services.manual_entries.
                "is_manual": manual_entries.is_manual(t),
            }
            for t, a, b in rows
        ],
    }


@router.get("/filters", response_model=None, summary="Distinct values available for filtering")
def filters(db: DbSession, portfolio: CurrentPortfolio) -> dict:
    op_types = db.scalars(
        select(Transaction.op_type)
        .where(Transaction.portfolio_id == portfolio.id)
        .group_by(Transaction.op_type)
        .order_by(Transaction.op_type)
    ).all()
    brokers = db.scalars(select(Broker.canonical_name).order_by(Broker.canonical_name)).all()
    bounds = db.execute(
        select(func.min(Transaction.trade_date), func.max(Transaction.trade_date)).where(
            Transaction.portfolio_id == portfolio.id
        )
    ).one()
    return {
        "op_types": list(op_types),
        "brokers": list(brokers),
        "date_range": {"start": bounds[0], "end": bounds[1]},
    }


# ---------------------------------------------------------------------------
# Hand-entered movements
# ---------------------------------------------------------------------------
class ManualPayload(BaseModel):
    operation: str
    ticker: str = Field(min_length=1, max_length=40)
    date: date
    quantity: Decimal = Field(default=Decimal(0), ge=0)
    unit_price: Decimal = Field(default=Decimal(0), ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    fees: Decimal = Field(default=Decimal(0), ge=0)
    taxes: Decimal = Field(default=Decimal(0), ge=0)
    name: str = ""
    kind: str | None = None
    currency: str = "BRL"
    broker: str | None = None
    notes: str | None = None


@router.get("/operations", response_model=None, summary="Operations a manual entry may use")
def operations() -> list[dict]:
    return manual_entries.catalogue()


@router.post("", response_model=None, summary="Add a movement by hand")
def create_transaction(payload: ManualPayload, db: DbSession, portfolio: CurrentPortfolio) -> dict:
    try:
        movement = manual_entries.create(
            db,
            portfolio.id,
            operation=payload.operation,
            ticker=payload.ticker,
            when=payload.date,
            quantity=payload.quantity,
            unit_price=payload.unit_price,
            amount=payload.amount,
            fees=payload.fees,
            taxes=payload.taxes,
            name=payload.name,
            kind=payload.kind,
            currency=payload.currency,
            broker=payload.broker,
            notes=payload.notes,
        )
    except manual_entries.ManualEntryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    asset = db.get(Asset, movement.asset_id)
    return {
        "id": movement.id,
        "date": movement.trade_date,
        "ticker": asset.ticker if asset else "",
        "op_type": movement.op_type,
        "effect": movement.effect,
        "quantity": movement.quantity,
        "unit_price": movement.unit_price,
        "gross_amount": movement.gross_amount,
        "net_amount": movement.net_amount,
    }


@router.delete("/{transaction_id}", status_code=204, response_model=None, summary="Remove a manual movement")
def delete_transaction(transaction_id: int, db: DbSession, portfolio: CurrentPortfolio) -> None:
    try:
        manual_entries.delete(db, portfolio.id, transaction_id)
    except manual_entries.ManualEntryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/export", response_model=None, summary="Export the filtered selection as CSV")
def export_transactions(
    db: DbSession,
    portfolio: CurrentPortfolio,
    search: str | None = None,
    ticker: str | None = None,
    op_type: list[str] | None = Query(default=None),
    broker: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> StreamingResponse:
    stmt = _base_query(portfolio.id, search, ticker, op_type, broker, start, end).order_by(
        Transaction.trade_date.asc(), Transaction.id.asc()
    )
    rows = db.execute(stmt).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        [
            "Data",
            "Ticker",
            "Nome",
            "Classe",
            "Operacao",
            "Movimento",
            "Entrada/Saida",
            "Quantidade",
            "Preco unitario",
            "Taxas",
            "Impostos",
            "Valor bruto",
            "Valor liquido",
            "Instituicao",
        ]
    )
    for t, a, b in rows:
        writer.writerow(
            [
                t.trade_date.isoformat(),
                a.ticker,
                a.name,
                a.kind,
                t.op_type,
                t.raw_movement,
                t.direction,
                t.quantity,
                t.unit_price,
                t.fees,
                t.taxes,
                t.gross_amount,
                t.net_amount,
                b.canonical_name if b else "",
            ]
        )
    buffer.seek(0)
    filename = f"gumbinvest-transacoes-{local_today().isoformat()}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
