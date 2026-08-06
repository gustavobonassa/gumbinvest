"""Tesouro Direto prices from Tesouro Transparente (open data, no key).

A Tesouro Direto title has no ticker and no broker quote, but the Treasury
itself publishes every title's price and yield for every business day since
2002 as a single CSV:

    https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-.../download/precotaxatesourodireto.csv

Columns (semicolon separated, pt-BR decimals)::

    Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;
    PU Compra Manha;PU Venda Manha;PU Base Manha

Buy side vs sell side
---------------------
The Treasury quotes a spread, and the column names are from the *investor's*
side: ``PU Compra`` is what the investor pays, ``PU Venda`` is what the
Treasury pays to buy the paper back. ``PU Compra`` is therefore always the
higher of the two, and ``PU Base`` mirrors ``PU Venda``.

This was verified against real purchases: the reference portfolio bought
Renda+ 2065 at 177,93 on 12/01/2026 (``PU Compra`` that morning: 178,00) and at
173,70 on 10/06/2026 (``PU Compra``: 173,78) — the residual is the intraday
move, since the file is a 9 a.m. snapshot.

Positions are valued at ``PU Venda``: that is what an early redemption
actually pays, which is also what the official Tesouro Direto statement shows.
For long papers the spread is wide (Renda+ 2065 trades ~5 % apart), so pricing
a position at the buy side would book a profit nobody can realise.

Naming
------
For most titles the year in the product name is the maturity year. The two
instalment products are different: Renda+ pays 240 monthly instalments (20
years) and Educa+ pays 60 (5 years), and the year in the name is when payments
*start* — the series is keyed by the *last* one. "Renda+ 2065" is therefore the
series maturing 15/12/2084.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.core.dates import local_today
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Asset, PriceHistory, Quote, Transaction, TreasuryPrice
from app.domain.enums import AssetKind
from app.market.service import _upsert_price

logger = get_logger(__name__)

CSV_URL = (
    "https://www.tesourotransparente.gov.br/ckan/dataset/"
    "df56aa42-484a-4a59-8184-7676580c81e3/resource/"
    "796d2059-14e9-44e3-80c9-2d9e30b405c1/download/precotaxatesourodireto.csv"
)

SOURCE = "tesouro"

#: Instrument families priced from this feed.
TREASURY_KINDS = {AssetKind.TREASURY.value}

#: Instalment products: name year -> maturity year offset (see module docstring).
PAYOUT_OFFSET_YEARS = {"renda": 19, "educa": 4}

#: The file is republished every business morning; a wider gap means the feed
#: (or this container's network) is behind, and the UI says so.
STALE_TOLERANCE_DAYS = 5

_YEAR = re.compile(r"\b(19|20)\d{2}\b")


@dataclass(frozen=True, slots=True)
class SeriesKey:
    """Identifies one tradable paper in the feed."""

    title: str
    maturity: date


@dataclass(slots=True)
class TreasuryQuote:
    """One business day of one paper."""

    day: date
    buy_price: Decimal
    sell_price: Decimal
    buy_rate: Decimal | None
    sell_rate: Decimal | None


# -- parsing ---------------------------------------------------------------
def _normalize(text: str) -> str:
    """Fold a product name to a comparable key: no accents, no punctuation."""
    stripped = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(ch for ch in stripped if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_only.lower()).split())


def _decimal(value: str) -> Decimal | None:
    text = (value or "").strip().replace(".", "").replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _day(value: str) -> date | None:
    try:
        return datetime.strptime((value or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def split_product_year(name: str) -> tuple[str, int | None]:
    """``"Tesouro Renda+ Aposentadoria Extra 2065"`` -> ``("tesouro renda aposentadoria extra", 2065)``."""
    normalized = _normalize(name)
    years = _YEAR.findall(normalized)
    if not years:
        return normalized, None
    # The year is the last token in every B3 spelling of these products.
    match = list(re.finditer(r"\b(?:19|20)\d{2}\b", normalized))[-1]
    year = int(match.group())
    base = " ".join((normalized[: match.start()] + normalized[match.end() :]).split())
    return base, year


def candidate_maturity_years(base: str, year: int) -> list[int]:
    """Maturity years a product name could refer to, best guess first."""
    years = [year]
    for marker, offset in PAYOUT_OFFSET_YEARS.items():
        if marker in base:
            years.append(year + offset)
    return years


def _title_matches(asset_base: str, title_norm: str) -> bool:
    """True when a feed title names the same product as an asset name.

    Exact after normalisation in the common case; prefix matching covers the
    broker spellings that append noise ("Tesouro IPCA+ 2029 NTNB Princ").
    """
    return asset_base == title_norm or asset_base.startswith(title_norm + " ")


def fetch_csv(url: str | None = None) -> str:
    """Download the price file (~14 MB, republished every business morning)."""
    response = httpx.get(
        url or CSV_URL, timeout=max(settings.request_timeout * 6, 120.0), follow_redirects=True
    )
    response.raise_for_status()
    return response.content.decode("utf-8-sig", errors="replace")


def parse_series(text: str, wanted: dict[str, set[int]] | None = None) -> dict[SeriesKey, list[TreasuryQuote]]:
    """Parse the feed into per-paper day series.

    ``wanted`` maps a normalised product base to the maturity years worth
    keeping; passing it keeps memory flat, since the full file holds ~185 000
    rows and a portfolio only ever holds a handful of papers.
    """
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    series: dict[SeriesKey, list[TreasuryQuote]] = defaultdict(list)
    normalized_titles: dict[str, str] = {}

    for row in reader:
        raw_title = (row.get("Tipo Titulo") or "").strip()
        if not raw_title:
            continue
        title_norm = normalized_titles.get(raw_title)
        if title_norm is None:
            title_norm = normalized_titles.setdefault(raw_title, _normalize(raw_title))

        maturity = _day(row.get("Data Vencimento", ""))
        day = _day(row.get("Data Base", ""))
        if maturity is None or day is None:
            continue

        if wanted is not None and not any(
            _title_matches(base, title_norm) and maturity.year in years
            for base, years in wanted.items()
        ):
            continue

        buy_price = _decimal(row.get("PU Compra Manha", ""))
        sell_price = _decimal(row.get("PU Venda Manha", "")) or _decimal(row.get("PU Base Manha", ""))
        if buy_price is None or sell_price is None:
            continue

        series[SeriesKey(raw_title, maturity)].append(
            TreasuryQuote(
                day=day,
                buy_price=buy_price,
                sell_price=sell_price,
                buy_rate=_decimal(row.get("Taxa Compra Manha", "")),
                sell_rate=_decimal(row.get("Taxa Venda Manha", "")),
            )
        )

    for quotes in series.values():
        quotes.sort(key=lambda q: q.day)
    return dict(series)


def match_series(name: str, series: dict[SeriesKey, list[TreasuryQuote]]) -> SeriesKey | None:
    """Find the paper a product name refers to, or ``None`` if it is unknown."""
    base, year = split_product_year(name)
    if year is None:
        return None
    years = candidate_maturity_years(base, year)
    # Longest title first: "Tesouro IPCA+ com Juros Semestrais" must win over
    # "Tesouro IPCA+", which is a prefix of it.
    keys = sorted(series, key=lambda k: len(k.title), reverse=True)
    for wanted_year in years:
        for key in keys:
            if key.maturity.year == wanted_year and _title_matches(base, _normalize(key.title)):
                return key
    return None


# -- syncing ---------------------------------------------------------------
def treasury_assets(db: Session) -> list[Asset]:
    return list(
        db.scalars(
            select(Asset)
            .where(Asset.kind.in_(TREASURY_KINDS), Asset.price_manual.is_(False))
            .order_by(Asset.ticker)
        ).all()
    )


def _asset_name(asset: Asset) -> str:
    """The product name to match on — falling back to the synthetic ticker."""
    return asset.name or asset.ticker.replace("-", " ")


def _store_series(db: Session, asset: Asset, quotes: list[TreasuryQuote]) -> int:
    """Upsert one paper's history, mirroring the sell side into price history."""
    existing = {
        row.date: row
        for row in db.scalars(select(TreasuryPrice).where(TreasuryPrice.asset_id == asset.id)).all()
    }
    for quote in quotes:
        row = existing.get(quote.day)
        if row is None:
            db.add(
                TreasuryPrice(
                    asset_id=asset.id,
                    date=quote.day,
                    buy_price=quote.buy_price,
                    sell_price=quote.sell_price,
                    buy_rate=quote.buy_rate,
                    sell_rate=quote.sell_rate,
                    source="tesouro-transparente",
                )
            )
        else:
            row.buy_price = quote.buy_price
            row.sell_price = quote.sell_price
            row.buy_rate = quote.buy_rate
            row.sell_rate = quote.sell_rate
        # The history charts read price_history, so the sell side lands there
        # too — a Tesouro position then has a real curve, not a flat cost line.
        _upsert_price(db, asset.id, quote.day, quote.sell_price, SOURCE)
    return len(quotes)


def _store_quote(db: Session, asset: Asset, quotes: list[TreasuryQuote], title: str) -> None:
    latest = quotes[-1]
    previous = quotes[-2] if len(quotes) > 1 else None
    change = None if previous is None else latest.sell_price - previous.sell_price
    change_percent = (
        None
        if previous is None or not previous.sell_price
        else (change / previous.sell_price) * Decimal(100)
    )
    db.merge(
        Quote(
            asset_id=asset.id,
            price=latest.sell_price,
            previous_close=None if previous is None else previous.sell_price,
            change=change,
            change_percent=change_percent,
            currency="BRL",
            source=SOURCE,
            long_name=title,
            fetched_at=datetime.now(UTC),
        )
    )


def sync_treasury_prices(db: Session, csv_text: str | None = None) -> dict:
    """Download Tesouro Transparente and price every Tesouro Direto holding.

    Safe to re-run: rows are upserted by (asset, day). A paper the feed does
    not know about is reported as unmatched rather than silently priced.
    """
    assets = treasury_assets(db)
    if not assets:
        return {"assets": 0, "points": 0, "detail": "no treasury assets"}

    wanted: dict[str, set[int]] = defaultdict(set)
    for asset in assets:
        base, year = split_product_year(_asset_name(asset))
        if year is not None:
            wanted[base].update(candidate_maturity_years(base, year))

    try:
        text = csv_text if csv_text is not None else fetch_csv()
    except Exception as exc:  # noqa: BLE001 — a failed sync must not break pricing
        logger.warning("Tesouro Transparente request failed: %s", exc)
        return {"assets": 0, "points": 0, "error": str(exc)}

    series = parse_series(text, dict(wanted))
    matched, points, unmatched = 0, 0, []
    for asset in assets:
        key = match_series(_asset_name(asset), series)
        quotes = series.get(key) if key else None
        if not key or not quotes:
            unmatched.append(asset.ticker)
            continue
        points += _store_series(db, asset, quotes)
        _store_quote(db, asset, quotes, key.title)
        matched += 1
    db.commit()

    logger.info("treasury sync: %s assets, %s points, unmatched=%s", matched, points, unmatched)
    return {"assets": matched, "points": points, "unmatched": unmatched, "source": SOURCE}


# -- reading ---------------------------------------------------------------
def latest_price(db: Session, asset_id: int) -> TreasuryPrice | None:
    return db.scalar(
        select(TreasuryPrice)
        .where(TreasuryPrice.asset_id == asset_id)
        .order_by(TreasuryPrice.date.desc())
        .limit(1)
    )


def price_on(db: Session, asset_id: int, day: date) -> TreasuryPrice | None:
    """The paper's price on ``day``, or the last one published before it."""
    return db.scalar(
        select(TreasuryPrice)
        .where(TreasuryPrice.asset_id == asset_id, TreasuryPrice.date <= day)
        .order_by(TreasuryPrice.date.desc())
        .limit(1)
    )


def contracted_rate(db: Session, asset_id: int, portfolio_id: int) -> Decimal | None:
    """Amount-weighted yield the position was actually bought at.

    The B3 export states the price paid but not the rate, so it is read back
    from the feed on each purchase date. Comparing it to today's rate explains
    the mark-to-market: a Tesouro position falls precisely when rates rise.
    """
    rows = db.execute(
        select(Transaction.trade_date, Transaction.gross_amount)
        .where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.asset_id == asset_id,
            Transaction.quantity > 0,
            Transaction.direction == "CREDIT",
        )
        .order_by(Transaction.trade_date)
    ).all()

    weighted, total = Decimal(0), Decimal(0)
    for trade_date, amount in rows:
        price = price_on(db, asset_id, trade_date)
        if price is None or price.buy_rate is None or not amount:
            continue
        weighted += price.buy_rate * Decimal(amount)
        total += Decimal(amount)
    return (weighted / total) if total else None


def coverage(db: Session) -> list[dict]:
    """Stored feed coverage per paper, for the UI's data-provenance strip."""
    rows = db.execute(
        select(
            Asset.ticker,
            func.min(TreasuryPrice.date),
            func.max(TreasuryPrice.date),
            func.count(TreasuryPrice.id),
        )
        .join(Asset, Asset.id == TreasuryPrice.asset_id)
        .group_by(Asset.ticker)
        .order_by(Asset.ticker)
    ).all()
    return [
        {"ticker": ticker, "start": start, "end": end, "points": count} for ticker, start, end, count in rows
    ]


def is_stale(last_known: date | None, today: date | None = None) -> bool:
    reference = today or local_today()
    return last_known is None or (reference - last_known) > timedelta(days=STALE_TOLERANCE_DAYS)
