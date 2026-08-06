"""Which statements are missing.

The offshore history is only as good as the pile of PDFs it was built from, and
a month never downloaded looks exactly like a quiet month: no error, no gap in
the numbers, just a position that is quietly wrong from then on.

Three independent checks, weakest to strongest:

1. **Calendar gaps** — a month with no statement between the first and the last
   one held for that account. Catches the plain "I forgot to download March".
2. **Balance breaks** — a statement whose opening balance does not match the
   previous statement's closing balance. Catches a month that is missing even
   when the calendar looks complete, because two consecutive files then fail to
   join up.
3. **Position drift** — the quantity the engine computes for an asset versus the
   quantity the most recent statement says is held. This is the check that
   catches everything else: a movement in a section the parser skipped, a
   corporate action nobody recorded, a statement that imported but only partly.

Only the first two can suggest *which file* to fetch, so all three are reported
separately rather than collapsed into one number.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Asset, ImportBatch
from app.domain.enums import ImportStatus

#: Balances below this are treated as equal (statements round to the cent).
BALANCE_TOLERANCE = Decimal("0.05")
#: Fractional-share dust: brokers and the replay disagree in the 8th decimal.
QUANTITY_TOLERANCE = Decimal("0.00001")


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _months_between(start: date, end: date) -> list[str]:
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


@dataclass(slots=True)
class StatementMonth:
    """Every statement held for one broker-month."""

    month: str
    period_start: date
    period_end: date
    files: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    transactions: int = 0


@dataclass(slots=True)
class AccountCoverage:
    """Coverage of one statement *series*.

    Keyed by broker **and** account number, because a broker can issue two
    unrelated series: Avenue's history runs through an Apex account numbered
    ``6AV-56990-17`` and, from 2025, a second series under Avenue's own
    ``098455499`` describing the same holdings. Balance continuity only means
    anything inside one series.
    """

    broker: str
    account_ref: str
    currency: str
    months: list[StatementMonth] = field(default_factory=list)
    missing_months: list[str] = field(default_factory=list)
    balance_breaks: list[dict] = field(default_factory=list)
    position_drift: list[dict] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not (self.missing_months or self.balance_breaks or self.position_drift)

    def to_dict(self) -> dict:
        return {
            "broker": self.broker,
            "account_ref": self.account_ref,
            "currency": self.currency,
            "first_month": self.months[0].month if self.months else None,
            "last_month": self.months[-1].month if self.months else None,
            "statements": len(self.months),
            "is_complete": self.is_complete,
            "months": [
                {
                    "month": month.month,
                    "files": month.files,
                    "formats": month.formats,
                    "opening_balance": _str(month.opening_balance),
                    "closing_balance": _str(month.closing_balance),
                    "transactions": month.transactions,
                }
                for month in self.months
            ],
            "missing_months": self.missing_months,
            "balance_breaks": self.balance_breaks,
            "position_drift": self.position_drift,
        }


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def statement_coverage(db: Session, portfolio_id: int) -> list[dict]:
    """Coverage report for every broker whose history came from statements."""
    batches = db.scalars(
        select(ImportBatch)
        .where(
            ImportBatch.portfolio_id == portfolio_id,
            ImportBatch.source_kind == "PDF",
            ImportBatch.status == ImportStatus.COMPLETED.value,
            ImportBatch.period_start.isnot(None),
        )
        .order_by(ImportBatch.period_start, ImportBatch.id)
    ).all()
    if not batches:
        return []

    grouped: dict[tuple[str, str], dict[str, StatementMonth]] = defaultdict(dict)
    currencies: dict[tuple[str, str], str] = {}

    for batch in batches:
        series = (batch.broker_name or "Desconhecida", batch.account_ref or "—")
        currencies.setdefault(series, batch.currency or "USD")
        month_key = _month_key(batch.period_end or batch.period_start)
        month = grouped[series].get(month_key)
        if month is None:
            month = StatementMonth(
                month=month_key,
                period_start=batch.period_start,
                period_end=batch.period_end or batch.period_start,
            )
            grouped[series][month_key] = month
        month.files.append(batch.filename)
        if batch.source_format and batch.source_format not in month.formats:
            month.formats.append(batch.source_format)
        month.transactions += batch.rows_imported
        # Several files can describe one month; keep the widest balances, which
        # are the ones covering the whole period rather than a fragment of it.
        if batch.opening_balance is not None and month.opening_balance is None:
            month.opening_balance = batch.opening_balance
        if batch.closing_balance is not None:
            month.closing_balance = batch.closing_balance

    drift = _position_drift(db, portfolio_id, batches)

    report: list[AccountCoverage] = []
    for series, months_by_key in grouped.items():
        broker, account_ref = series
        months = [months_by_key[key] for key in sorted(months_by_key)]
        report.append(
            AccountCoverage(
                broker=broker,
                account_ref=account_ref,
                currency=currencies.get(series, "USD"),
                months=months,
                missing_months=_missing_months(months),
                balance_breaks=_balance_breaks(months),
                # Drift is a property of the whole broker, not of one series:
                # two series can describe the same holdings, so it is reported
                # against the broker's most recent statement only.
                position_drift=drift.pop(broker, []),
            )
        )

    report.sort(key=lambda item: (item.broker, item.months[0].month if item.months else ""))
    return [item.to_dict() for item in report]


def _missing_months(months: list[StatementMonth]) -> list[str]:
    if len(months) < 2:
        return []
    present = {month.month for month in months}
    expected = _months_between(months[0].period_start, months[-1].period_end)
    return [month for month in expected if month not in present]


def _balance_breaks(months: list[StatementMonth]) -> list[dict]:
    """Months whose opening balance does not continue the previous close.

    A break means value appeared or vanished between two statements, which is
    what a missing month looks like from the outside.
    """
    breaks: list[dict] = []
    for previous, current in zip(months, months[1:]):
        if previous.closing_balance is None or current.opening_balance is None:
            continue
        difference = current.opening_balance - previous.closing_balance
        if abs(difference) <= BALANCE_TOLERANCE:
            continue
        breaks.append(
            {
                "month": current.month,
                "previous_month": previous.month,
                "previous_closing": str(previous.closing_balance),
                "opening": str(current.opening_balance),
                "difference": str(difference),
            }
        )
    return breaks


def _drift_rank(batch: ImportBatch) -> tuple:
    """Newest statement wins; among equals, the one reporting most positions."""
    holdings = (batch.summary or {}).get("statement", {}).get("holdings", []) or []
    return (batch.period_end or batch.period_start, len(holdings), batch.id)


def _position_drift(
    db: Session, portfolio_id: int, batches: list[ImportBatch]
) -> dict[str, list[dict]]:
    """Compare replayed quantities with the latest statements' own holdings.

    Exactly one statement per broker is used — the most recent, and among
    equally recent ones the one listing the most positions. Two series can
    describe the same account (Avenue issues both an Apex-numbered and an
    Avenue-numbered report), so adding up every current statement would count
    those holdings twice.

    The comparison is portfolio-wide because the engine tracks positions per
    asset, not per custodian: a ticker held at two brokers has one position, and
    only the sum of both brokers' reported holdings can be checked against it.
    """
    latest: dict[str, ImportBatch] = {}
    for batch in batches:
        broker = batch.broker_name or "Desconhecida"
        current = latest.get(broker)
        if current is None or _drift_rank(batch) > _drift_rank(current):
            latest[broker] = batch
    if not latest:
        return {}

    from app.importer.pdf.symbols import canonical_ticker
    from app.portfolio.service import PortfolioService

    service = PortfolioService(db, portfolio_id)
    positions = service.positions()
    tickers = {
        asset.id: asset.ticker
        for asset in db.scalars(select(Asset)).all()
    }
    held: dict[str, Decimal] = {}
    for asset_id, position in positions.items():
        ticker = tickers.get(asset_id)
        if ticker:
            held[ticker] = held.get(ticker, Decimal(0)) + position.quantity

    # A ticker held at two brokers has one combined position, so drift can only
    # be judged when every statement covering it is the latest for its broker.
    reported: dict[str, Decimal] = defaultdict(Decimal)
    broker_of: dict[str, str] = {}
    for broker, batch in latest.items():
        for entry in (batch.summary or {}).get("statement", {}).get("holdings", []) or []:
            ticker = canonical_ticker(entry.get("symbol", ""))
            if not ticker:
                continue
            reported[ticker] += Decimal(entry.get("quantity", "0"))
            broker_of.setdefault(ticker, broker)

    drift: dict[str, list[dict]] = defaultdict(list)
    for ticker, quantity in sorted(reported.items()):
        computed = held.get(ticker, Decimal(0))
        difference = computed - quantity
        if abs(difference) <= QUANTITY_TOLERANCE:
            continue
        drift[broker_of.get(ticker, "Desconhecida")].append(
            {
                "ticker": ticker,
                "reported": str(quantity),
                "computed": str(computed),
                "difference": str(difference),
            }
        )
    return drift

