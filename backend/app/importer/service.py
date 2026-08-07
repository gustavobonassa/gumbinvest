"""Import orchestration: parse -> classify -> de-duplicate -> persist.

Two front doors, one pipeline. :meth:`ImportService.import_csv` reads the B3
"Movimentação" export; :meth:`ImportService.import_pdf` reads a broker statement
(see :mod:`app.importer.pdf`). Both produce the same normalised rows, so
classification, de-duplication and persistence are shared.

De-duplication strategy
-----------------------
Each row gets a deterministic ``dedup_key`` built from the fields that identify
a movement (date, direction, movement, ticker, broker, quantity, price, amount)
plus an **occurrence counter**. The counter is what makes the scheme both
idempotent *and* lossless:

* re-importing the same file produces the same keys -> every row is a duplicate;
* a monthly file that overlaps the previous one only adds what is genuinely new;
* two identical movements on the same day (they do happen — the sample export
  contains one) get occurrences 0 and 1 and are both kept.

Statements add a second problem the counter cannot solve: two reports of the
same month, from the same broker, describing the *same* event differently. That
is handled by the fuzzy index in :mod:`app.importer.dedup`.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import (
    Asset,
    AuditLog,
    Broker,
    ImportBatch,
    Portfolio,
    PriceHistory,
    Quote,
    Transaction,
)
from app.domain.enums import AssetKind, Direction, ImportStatus, PositionEffect
from app.importer.classifier import Classification, classify, parse_direction
from app.importer.crypto import (
    CryptoEvent,
    CryptoFormatError,
    CryptoTrade,
    ParsedTradeFile,
    parse_crypto_csv,
)
from app.importer.crypto import symbols as coins
from app.importer.dedup import FuzzyIndex, exact_fingerprint, fuzzy_key
from app.importer.parser import (
    CsvFormatError,
    ParseResult,
    RawRow,
    classify_asset_kind,
    decode_bytes,
    normalize_key,
    parse_movements,
)
from app.importer.pdf import ParsedStatement, PdfFormatError, StatementRow, parse_pdf
from app.importer.pdf.movements import CASH_ONLY
from app.importer.pdf.symbols import (
    REIT_TICKERS,
    canonical_ticker,
    market_symbol_for,
    provisional_ticker,
    resolve_ticker,
)

logger = get_logger(__name__)

_CASH_IN_EFFECTS = {PositionEffect.DISPOSE, PositionEffect.CASH_IN, PositionEffect.RETURN_OF_CAPITAL}
_CASH_OUT_EFFECTS = {PositionEffect.ACQUIRE, PositionEffect.CASH_OUT}
#: How far outside a statement's own period to look for cross-source duplicates.
_FUZZY_LOOKBACK = timedelta(days=10)

#: Instrument families held on a crypto exchange.
CRYPTO_KINDS = coins.CRYPTO_KINDS
#: How far two exchange exports may disagree on the value of the same trade and
#: still be recognised as one event. Binance's order history aggregates the
#: fills of an order, so the sums round differently in the last decimal — but
#: nothing else moves, because both files quote the trade in the same currency.
_CRYPTO_MATCH_TOLERANCE = Decimal("0.005")


@dataclass(slots=True)
class ImportResult:
    batch_id: int
    filename: str
    status: str
    rows_total: int
    rows_imported: int
    rows_duplicate: int
    rows_failed: int
    issues: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def _q(value: Decimal | None) -> Decimal:
    return Decimal(0) if value is None else value


def _net_amount(effect: PositionEffect, gross: Decimal) -> Decimal:
    """The cash the movement actually moved, signed."""
    if effect in _CASH_IN_EFFECTS:
        return gross
    if effect in _CASH_OUT_EFFECTS:
        return -gross
    return Decimal(0)


def reclassify_transactions(db: Session, portfolio_id: int | None = None) -> dict:
    """Re-derive ``op_type``/``effect`` for stored transactions.

    Both columns are *derived* from the raw movement label, so a classifier fix
    would otherwise only reach rows imported afterwards — and de-duplication
    means re-uploading the same file changes nothing. Running this keeps the
    database consistent with the current rules; it is idempotent and cheap
    (a few thousand rows), so the API does it on every start.
    """
    stmt = select(Transaction)
    if portfolio_id is not None:
        stmt = stmt.where(Transaction.portfolio_id == portfolio_id)

    changed = 0
    details: Counter[str] = Counter()
    for transaction in db.scalars(stmt).all():
        direction = Direction(transaction.direction)
        classification = classify(transaction.raw_movement, direction, transaction.gross_amount)
        if (
            classification.op_type.value == transaction.op_type
            and classification.effect.value == transaction.effect
        ):
            continue
        details[f"{transaction.raw_movement}: {transaction.effect} -> {classification.effect.value}"] += 1
        transaction.op_type = classification.op_type.value
        transaction.effect = classification.effect.value
        transaction.net_amount = _net_amount(classification.effect, transaction.gross_amount)
        changed += 1

    if changed:
        db.add(AuditLog(action="import.reclassify", detail={"updated": changed, "changes": dict(details)}))
        db.commit()
        logger.info("reclassified %s transactions: %s", changed, dict(details))
    return {"updated": changed, "changes": dict(details)}


def reconcile_market_symbols(db: Session) -> dict:
    """Point renamed tickers at the symbol the market uses today.

    A company can rename itself years into a holding — Medical Properties Trust
    became MPT and Bank of New York Mellon became BNY — and the quote provider
    then returns 404 for the ticker the whole history is filed under, so the
    position silently stops being priced. The holding keeps its historical
    ticker; only the symbol the provider is asked for changes.

    Runs on every start and is idempotent.
    """
    changed = 0
    details: Counter[str] = Counter()
    for asset in db.scalars(select(Asset)).all():
        # A coin is quoted as a *pair* ("BTC-USD"), so it has its own rule —
        # running the equities table over it would ask the provider for "BTC"
        # and get whatever else trades under that name.
        expected = (
            coins.market_symbol_for(_coin_symbol(asset.ticker))
            if asset.kind in CRYPTO_KINDS
            else market_symbol_for(asset.ticker)
        )
        if expected == (asset.market_symbol or asset.ticker).upper():
            continue
        details[f"{asset.ticker}: {asset.market_symbol} -> {expected}"] += 1
        asset.market_symbol = expected
        changed += 1

    if changed:
        db.add(
            AuditLog(
                action="import.market_symbols",
                detail={"updated": changed, "changes": dict(details)},
            )
        )
        db.commit()
        logger.info("market symbols reconciled for %s assets: %s", changed, dict(details))
    return {"updated": changed, "changes": dict(details)}


def reclassify_assets(db: Session) -> dict:
    """Re-derive ``Asset.kind`` from the product description on file.

    The kind is inferred once, when an asset is first seen, so a rule change
    would otherwise only apply to assets imported afterwards. Like
    :func:`reclassify_transactions` this runs on every start: it is idempotent,
    reads the raw product text stored with the asset's first transaction, and
    keeps existing portfolios consistent with the current rules.

    Which rule set applies depends on the currency. The B3 classifier reads the
    ticker suffix, which is meaningless on a US listing — run it over ``NKE`` or
    ``VOO`` and every offshore holding collapses to ``OTHER``, taking its quotes
    with it, since ``OTHER`` is a family no price provider is asked about.
    """
    changed = 0
    details: Counter[str] = Counter()
    for asset in db.scalars(select(Asset)).all():
        if asset.is_cash_account:
            # Entered by hand, not imported: there is no product description to
            # re-read, and re-deriving a kind from the synthetic one would move
            # a bank balance out of renda fixa.
            continue
        if asset.kind in CRYPTO_KINDS:
            # A coin has no product description to read: the exchange export
            # names it by symbol and nothing else, so the split between crypto
            # and dollar-pegged tokens comes from the symbol table instead.
            kind = coins.coin_kind(_coin_symbol(asset.ticker)).value
            if kind != asset.kind:
                details[f"{asset.ticker}: {asset.kind} -> {kind}"] += 1
                asset.kind = kind
                changed += 1
            continue
        product = db.scalar(
            select(Transaction.raw_product)
            .where(Transaction.asset_id == asset.id, Transaction.raw_product.isnot(None))
            .limit(1)
        )
        if not product:
            continue
        if (asset.currency or "BRL").upper() == "BRL":
            kind = classify_asset_kind(asset.ticker, product).value
        else:
            kind = classify_us_asset_kind(asset.ticker, product).value
        if kind == asset.kind:
            continue
        details[f"{asset.ticker}: {asset.kind} -> {kind}"] += 1
        asset.kind = kind
        changed += 1

    if changed:
        db.add(
            AuditLog(action="import.reclassify_assets", detail={"updated": changed, "changes": dict(details)})
        )
        db.commit()
        logger.info("reclassified %s assets: %s", changed, dict(details))
    return {"updated": changed, "changes": dict(details)}


def _fingerprint(row: RawRow, direction: Direction) -> str:
    """Stable hash of a CSV movement's identifying fields (occurrence excluded)."""
    return exact_fingerprint(
        row.trade_date.isoformat(),
        direction.value,
        normalize_key(row.movement),
        row.product.ticker.upper(),
        normalize_key(row.broker),
        format(_q(row.quantity).normalize(), "f"),
        format(_q(row.unit_price).normalize(), "f"),
        format(_q(row.total).normalize(), "f"),
    )


def _statement_fingerprint(row: StatementRow, ticker: str, broker: str, op_type: str) -> str:
    """Stable hash of a statement movement.

    Deliberately *not* built from the broker's own wording or section names: the
    same dividend is "Crédito dividendos" in one Avenue report and "CASH DIV ON"
    in the other, so hashing the label would make re-importing either file add
    the movement a second time.
    """
    return exact_fingerprint(
        row.trade_date.isoformat(),
        row.direction.value,
        op_type,
        ticker.upper(),
        normalize_key(broker),
        format(_q(row.quantity).normalize(), "f"),
        format(_q(row.amount).normalize(), "f"),
    )


def classify_us_asset_kind(ticker: str, description: str) -> AssetKind:
    """Instrument family for a security listed abroad.

    The B3 classifier reads the ticker suffix, which means nothing on a US
    listing, so this reads the security itself instead.

    REITs are recognised by ticker, not by name. Being a REIT is a tax
    structure, and almost none of them say so: "SL Green Realty", "STAG
    Industrial", "W. P. Carey" and "Macerich" are all REITs whose descriptions
    give no hint of it. The name check that follows only catches the few that
    spell it out, so a REIT the list has never heard of lands in stocks — wrong,
    but visibly wrong on a page that lists both.

    Every family returned here is an offshore one, even where the domestic
    equivalent exists: a US share is ``STOCK_INTL`` rather than ``STOCK`` and a
    US fund is ``ETF_INTL`` rather than ``ETF``. Keeping them apart is the whole
    point — a chart that merges Petrobras with Nike answers no useful question,
    and the two are not comparable on currency or on tax treatment.
    """
    from app.importer.pdf.layout import normalize

    text = normalize(description)
    # Funds first: a real-estate ETF is an ETF, not a REIT.
    if any(marker in text for marker in ("etf", "indexfds", "indexfunds", "proshares", "admiralfds")):
        return AssetKind.ETF_INTL
    if canonical_ticker(ticker) in REIT_TICKERS:
        return AssetKind.REIT
    if any(marker in text for marker in ("reit", "realtyincome", "propertiestrust", "realestate")):
        return AssetKind.REIT
    return AssetKind.STOCK_INTL


def _rekey_after_rename(db: Session, asset: Asset) -> int:
    """Recompute the de-duplication keys of a renamed asset's movements.

    The key embeds the ticker. Rename the asset without touching it and every
    stored movement becomes invisible to the next import, which cheerfully adds
    the whole history a second time under the new name — 92 movements became
    184 the first time this was missed.

    The fingerprint is rebuilt from the columns it was derived from, and the
    occurrence counter is carried across unchanged, so the result is byte for
    byte what a fresh import of the same files would produce.
    """
    rows = db.execute(
        select(Transaction, ImportBatch.source_kind, Broker.canonical_name)
        .outerjoin(ImportBatch, ImportBatch.id == Transaction.import_batch_id)
        .outerjoin(Broker, Broker.id == Transaction.broker_id)
        .where(Transaction.asset_id == asset.id)
    ).all()

    updated = 0
    for transaction, source_kind, broker_name in rows:
        broker = normalize_key(broker_name or "")
        if source_kind == "PDF":
            fingerprint = exact_fingerprint(
                transaction.trade_date.isoformat(),
                transaction.direction,
                transaction.op_type,
                asset.ticker.upper(),
                broker,
                format(_q(transaction.quantity).normalize(), "f"),
                format(_q(transaction.gross_amount).normalize(), "f"),
            )
        elif source_kind == "CSV":
            fingerprint = exact_fingerprint(
                transaction.trade_date.isoformat(),
                transaction.direction,
                normalize_key(transaction.raw_movement),
                asset.ticker.upper(),
                broker,
                format(_q(transaction.quantity).normalize(), "f"),
                format(_q(transaction.unit_price).normalize(), "f"),
                format(_q(transaction.gross_amount).normalize(), "f"),
            )
        else:
            # Exchange exports key on the coin, which no alias renames.
            continue
        transaction.dedup_key = f"{fingerprint}:{transaction.occurrence}"
        updated += 1
    return updated


def reconcile_ticker_aliases(db: Session) -> dict:
    """Move assets onto the ticker the market uses today.

    A company can rename itself years into a holding — Medical Properties Trust
    became MPT, Bank of New York Mellon became BNY — and the statements from
    before the change keep printing the old symbol. Both spellings already
    resolve to one asset on import; this renames the asset itself so the app
    shows the name the broker and the market now use.

    Transactions reference the asset by id, so nothing moves and no history is
    lost. If both tickers somehow exist as separate assets the older one is
    folded into the newer, which is the same outcome the alias would have
    produced had it been in place from the start.

    Runs on every start and is idempotent.
    """
    renamed = 0
    merged = 0
    details: Counter[str] = Counter()

    for asset in db.scalars(select(Asset)).all():
        target = canonical_ticker(asset.ticker)
        if target == asset.ticker:
            continue
        existing = db.scalar(select(Asset).where(Asset.ticker == target))
        if existing is None:
            details[f"{asset.ticker} -> {target}"] += 1
            asset.ticker = target
            asset.market_symbol = market_symbol_for(target)
            # Must happen with the rename, not after: the keys are what stop
            # the next import re-adding the whole history under the new name.
            _rekey_after_rename(db, asset)
            renamed += 1
            continue
        # Both exist: point every movement at the survivor and drop the alias.
        db.execute(
            update(Transaction)
            .where(Transaction.asset_id == asset.id)
            .values(asset_id=existing.id)
        )
        if asset.cusip and not existing.cusip:
            existing.cusip = asset.cusip
        # Market data is per-asset and refetched, so the loser's rows are
        # dropped rather than merged. They are deleted explicitly because the
        # ORM would try to blank out `quotes.asset_id`, which is that table's
        # primary key, instead of removing the row.
        for model in (Quote, PriceHistory):
            db.execute(sa_delete(model).where(model.asset_id == asset.id))
        details[f"{asset.ticker} + {target} (fundidos)"] += 1
        db.execute(sa_delete(Asset).where(Asset.id == asset.id))
        merged += 1

    if renamed or merged:
        db.commit()
        db.expire_all()
        db.add(
            AuditLog(
                action="import.ticker_aliases",
                detail={"renamed": renamed, "merged": merged, "changes": dict(details)},
            )
        )
        db.commit()
        logger.info("ticker aliases applied: %s", dict(details))
    return {"renamed": renamed, "merged": merged, "changes": dict(details)}


#: Re-exported so callers that already talk to the importer do not have to
#: reach into the coin vocabulary for one constant.
CRYPTO_TICKER_SUFFIX = coins.TICKER_SUFFIX
_coin_symbol = coins.asset_symbol


class ImportService:
    """Stateless-per-call importer bound to a SQLAlchemy session."""

    def __init__(self, db: Session, portfolio: Portfolio) -> None:
        self.db = db
        self.portfolio = portfolio
        self._asset_cache: dict[str, Asset] = {}
        self._broker_cache: dict[str, Broker] = {}
        #: Rate series loaded on demand, one per currency seen in the file.
        self._fx_cache: dict[str, object] = {}

    # -- entity resolution -------------------------------------------------
    def _get_asset(self, row: RawRow) -> Asset:
        return self._asset_for(
            ticker=row.product.ticker.upper(),
            name=row.product.name,
            kind=row.product.kind.value,
            currency=self.portfolio.base_currency,
        )

    def _asset_for(
        self,
        ticker: str,
        name: str,
        kind: str,
        currency: str,
        cusip: str = "",
        market_symbol: str | None = None,
    ) -> Asset:
        ticker = ticker.upper()
        cached = self._asset_cache.get(ticker)
        if cached is not None:
            self._enrich_asset(cached, name, cusip)
            return cached
        asset = self.db.scalar(select(Asset).where(Asset.ticker == ticker))
        if asset is None:
            asset = Asset(
                ticker=ticker,
                name=name[:255],
                kind=kind,
                currency=currency,
                cusip=cusip or None,
                # A renamed company keeps its historical ticker here but must be
                # quoted under the one the market uses now.
                market_symbol=market_symbol or market_symbol_for(ticker),
            )
            self.db.add(asset)
            self.db.flush()
        else:
            self._enrich_asset(asset, name, cusip)
        self._asset_cache[ticker] = asset
        return asset

    @staticmethod
    def _enrich_asset(asset: Asset, name: str, cusip: str) -> None:
        """Fill in identifiers a later, richer statement happens to supply."""
        if not asset.name and name:
            asset.name = name[:255]
        if cusip and not asset.cusip:
            asset.cusip = cusip

    def _get_broker(self, row: RawRow) -> Broker:
        return self._broker_named(row.broker, row.institution_raw)

    def _broker_named(self, canonical: str, raw_name: str) -> Broker:
        cached = self._broker_cache.get(canonical)
        if cached is not None:
            return cached
        broker = self.db.scalar(select(Broker).where(Broker.canonical_name == canonical))
        if broker is None:
            broker = Broker(canonical_name=canonical, raw_names=[raw_name] if raw_name else [])
            self.db.add(broker)
            self.db.flush()
        elif raw_name and raw_name not in (broker.raw_names or []):
            broker.raw_names = [*(broker.raw_names or []), raw_name]
        self._broker_cache[canonical] = broker
        return broker

    # -- shared batch plumbing ---------------------------------------------
    def _existing_keys(self) -> set[str]:
        """Every dedup key already stored — one query, then pure set maths."""
        return set(
            self.db.scalars(
                select(Transaction.dedup_key).where(Transaction.portfolio_id == self.portfolio.id)
            ).all()
        )

    def _known_cusips(self) -> dict[str, str]:
        """CUSIP -> ticker learned from statements imported earlier."""
        rows = self.db.execute(select(Asset.cusip, Asset.ticker).where(Asset.cusip.isnot(None))).all()
        return {cusip: ticker for cusip, ticker in rows}

    def _start_batch(self, raw_bytes: bytes, filename: str, source_kind: str) -> ImportBatch:
        batch = ImportBatch(
            portfolio_id=self.portfolio.id,
            filename=filename[:255],
            file_hash=hashlib.sha256(raw_bytes).hexdigest(),
            status=ImportStatus.RUNNING.value,
            source_kind=source_kind,
        )
        self.db.add(batch)
        self.db.flush()
        return batch

    def _fail_batch(self, batch: ImportBatch, error: str) -> None:
        batch.status = ImportStatus.FAILED.value
        batch.issues = [{"line": 1, "error": error}]
        batch.finished_at = datetime.now(UTC)
        self.db.commit()

    def _finish_batch(
        self,
        batch: ImportBatch,
        *,
        rows_total: int,
        imported: int,
        duplicates: int,
        issues: list[dict],
        summary: dict,
        action: str,
    ) -> ImportResult:
        batch.status = ImportStatus.COMPLETED.value
        batch.rows_total = rows_total
        batch.rows_imported = imported
        batch.rows_duplicate = duplicates
        batch.rows_failed = len(issues)
        batch.issues = issues[:500]
        batch.summary = summary
        batch.finished_at = datetime.now(UTC)
        self.db.add(
            AuditLog(
                action=action,
                detail={
                    "batch_id": batch.id,
                    "filename": batch.filename,
                    "imported": imported,
                    "duplicates": duplicates,
                    "failed": batch.rows_failed,
                },
            )
        )
        self.db.commit()
        logger.info(
            "import %s: %s rows, %s imported, %s duplicates, %s failed",
            batch.filename,
            rows_total,
            imported,
            duplicates,
            batch.rows_failed,
        )
        return ImportResult(
            batch_id=batch.id,
            filename=batch.filename,
            status=batch.status,
            rows_total=batch.rows_total,
            rows_imported=batch.rows_imported,
            rows_duplicate=batch.rows_duplicate,
            rows_failed=batch.rows_failed,
            issues=batch.issues,
            summary=batch.summary,
        )

    # -- public API: B3 CSV -------------------------------------------------
    def import_csv(self, payload: bytes | str, filename: str) -> ImportResult:
        """Parse and persist a B3 export, skipping already-known movements.

        Takes the CSV and the XLSX form of the same report; the payload says
        which it is. De-duplication is unaffected — the fingerprint is built
        from the movement's own fields, so the same history downloaded in the
        other format imports as zero new rows.
        """
        raw_bytes = payload if isinstance(payload, (bytes, bytearray)) else payload.encode("utf-8")
        batch = self._start_batch(raw_bytes, filename, "CSV")
        batch.currency = self.portfolio.base_currency

        try:
            parsed: ParseResult = parse_movements(raw_bytes)
        except CsvFormatError as exc:
            self._fail_batch(batch, str(exc))
            raise

        issues: list[dict] = list(parsed.errors)
        op_counter: Counter[str] = Counter()
        warning_counter: Counter[str] = Counter()
        unknown_movements: Counter[str] = Counter()

        existing_keys = self._existing_keys()
        # Occurrence is the row's index *within this file* among identical
        # movements — not "the next free slot". That is what makes a re-import
        # map onto the exact same keys and therefore add nothing.
        occurrence_in_file: dict[str, int] = defaultdict(int)

        imported = 0
        pending: list[Transaction] = []

        for row in parsed.rows:
            try:
                direction = parse_direction(row.direction_raw)
                gross = row.total
                if gross is None and row.unit_price is not None:
                    gross = (_q(row.quantity) * row.unit_price).quantize(Decimal("0.000001"))
                classification = classify(row.movement, direction, gross)

                fingerprint = _fingerprint(row, direction)
                occurrence = occurrence_in_file[fingerprint]
                occurrence_in_file[fingerprint] = occurrence + 1
                dedup_key = f"{fingerprint}:{occurrence}"
                if dedup_key in existing_keys:
                    continue

                asset = self._get_asset(row)
                broker = self._get_broker(row)

                gross_amount = _q(gross)
                unit_price = row.unit_price
                if unit_price is None and _q(row.quantity) != 0 and gross_amount != 0:
                    unit_price = gross_amount / row.quantity

                pending.append(
                    Transaction(
                        portfolio_id=self.portfolio.id,
                        asset_id=asset.id,
                        broker_id=broker.id,
                        import_batch_id=batch.id,
                        trade_date=row.trade_date,
                        direction=direction.value,
                        op_type=classification.op_type.value,
                        effect=classification.effect.value,
                        quantity=_q(row.quantity),
                        unit_price=_q(unit_price),
                        gross_amount=gross_amount,
                        fees=Decimal(0),
                        taxes=Decimal(0),
                        net_amount=_net_amount(classification.effect, gross_amount),
                        currency=self.portfolio.base_currency,
                        fx_rate=None,
                        raw_movement=row.movement[:120],
                        raw_product=row.product_raw[:255],
                        raw_institution=row.institution_raw[:255],
                        source_line=row.line_number,
                        dedup_key=dedup_key,
                        occurrence=occurrence,
                        notes=classification.warning,
                    )
                )
                existing_keys.add(dedup_key)
                imported += 1
                op_counter[classification.op_type.value] += 1
                if classification.warning:
                    warning_counter[classification.warning] += 1
                if classification.op_type.value == "UNKNOWN":
                    unknown_movements[row.movement] += 1
            except Exception as exc:  # noqa: BLE001 — never abort a whole import
                logger.exception("failed to import line %s", row.line_number)
                issues.append({"line": row.line_number, "error": str(exc), "raw": row.product_raw})

        if pending:
            self.db.bulk_save_objects(pending)

        dates = [r.trade_date for r in parsed.rows]
        if dates:
            batch.period_start, batch.period_end = min(dates), max(dates)
        summary = {
            "operations": dict(op_counter),
            "warnings": [{"message": m, "count": c} for m, c in warning_counter.most_common(50)],
            "unknown_movements": [
                {"movement": m, "count": c} for m, c in unknown_movements.most_common(50)
            ],
            "date_range": {
                "start": min(dates).isoformat() if dates else None,
                "end": max(dates).isoformat() if dates else None,
            },
            "file_hash": batch.file_hash,
        }
        return self._finish_batch(
            batch,
            rows_total=parsed.total_lines,
            imported=imported,
            duplicates=parsed.total_lines - imported - len(issues),
            issues=issues,
            summary=summary,
            action="import.csv",
        )

    # -- public API: broker statement PDF -----------------------------------
    def import_pdf(
        self, payload: bytes, filename: str, statement: ParsedStatement | None = None
    ) -> ImportResult:
        """Parse and persist a broker statement PDF.

        ``statement`` lets a caller that has already parsed the file pass the
        result in — the startup auto-import needs the period and broker to
        decide the import *order*, and re-reading a hundred PDFs to learn it
        twice would double the cold-start time.
        """
        batch = self._start_batch(payload, filename, "PDF")

        try:
            if statement is None:
                statement = parse_pdf(payload)
        except PdfFormatError as exc:
            self._fail_batch(batch, str(exc))
            raise

        batch.source_format = statement.format
        batch.broker_name = statement.broker
        batch.account_ref = statement.account_ref or None
        batch.currency = statement.currency
        batch.period_start = statement.period_start
        batch.period_end = statement.period_end
        batch.opening_balance = statement.opening_balance
        batch.closing_balance = statement.closing_balance

        state = _PdfImportState()
        # Parser-level notes and any control-total mismatch surface as issues:
        # a statement whose own printed totals disagree with what was read is
        # exactly the case a human needs to look at.
        state.issues.extend({"line": None, "error": message} for message in statement.warnings)
        state.issues.extend(
            {"line": None, "error": message} for message in statement.reconciliation_warnings()
        )

        existing_keys = self._existing_keys()
        known_cusips = self._known_cusips()
        fuzzy = self._fuzzy_index(statement)
        rates = self._fx_table(statement.currency)
        broker = self._broker_named(statement.broker, statement.institution_raw)
        occurrence_in_file: dict[str, int] = defaultdict(int)
        pending: list[Transaction] = []

        for row in statement.rows:
            try:
                if row.movement in CASH_ONLY:
                    # Deposits, withdrawals, journals and money-market sweeps:
                    # parsed so the section totals reconcile, but they belong to
                    # no asset and the portfolio model tracks positions, not the
                    # broker's cash account.
                    state.cash_rows += 1
                    continue

                classification = classify(row.movement, row.direction, row.amount)
                ticker = resolve_ticker(row.symbol, row.cusip, row.description, known_cusips)
                if ticker is None:
                    ticker = self._unidentified(row, state)
                    if ticker is None:
                        continue

                fingerprint = _statement_fingerprint(
                    row, ticker, statement.broker, classification.op_type.value
                )
                occurrence = occurrence_in_file[fingerprint]
                occurrence_in_file[fingerprint] = occurrence + 1
                dedup_key = f"{fingerprint}:{occurrence}"
                if dedup_key in existing_keys:
                    state.duplicates += 1
                    continue

                key = fuzzy_key(
                    statement.broker,
                    classification.op_type.value,
                    classification.effect.value,
                    ticker,
                    _q(row.quantity),
                    _q(row.amount),
                )
                if fuzzy.take(key, row.trade_date):
                    state.cross_source += 1
                    state.cross_source_detail[
                        f"{row.trade_date} {classification.op_type.value} {ticker}"
                    ] += 1
                    continue

                asset = self._asset_for(
                    ticker=ticker,
                    name=row.description,
                    kind=classify_us_asset_kind(ticker, row.description).value,
                    currency=statement.currency,
                    cusip=row.cusip,
                )
                gross = _q(row.amount)
                pending.append(
                    Transaction(
                        portfolio_id=self.portfolio.id,
                        asset_id=asset.id,
                        broker_id=broker.id,
                        import_batch_id=batch.id,
                        trade_date=row.trade_date,
                        direction=row.direction.value,
                        op_type=classification.op_type.value,
                        effect=classification.effect.value,
                        quantity=_q(row.quantity),
                        unit_price=_q(row.unit_price),
                        gross_amount=gross,
                        fees=Decimal(0),
                        taxes=Decimal(0),
                        net_amount=_net_amount(classification.effect, gross),
                        currency=statement.currency,
                        fx_rate=rates.rate_on(row.trade_date) if rates else None,
                        raw_movement=row.movement[:120],
                        raw_product=(row.description or ticker)[:255],
                        raw_institution=statement.institution_raw[:255],
                        source_line=row.page_number,
                        dedup_key=dedup_key,
                        occurrence=occurrence,
                        notes=classification.warning,
                    )
                )
                existing_keys.add(dedup_key)
                state.imported += 1
                state.operations[classification.op_type.value] += 1
                if classification.warning:
                    state.warnings[classification.warning] += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not kill the import
                logger.exception("failed to import statement row %r", row.raw_text[:120])
                state.issues.append(
                    {"line": row.page_number, "error": str(exc), "raw": row.raw_text[:200]}
                )

        if pending:
            self.db.bulk_save_objects(pending)

        return self._finish_batch(
            batch,
            rows_total=len(statement.rows),
            imported=state.imported,
            duplicates=state.duplicates + state.cross_source + state.cash_rows,
            issues=state.issues,
            summary=state.summary(statement, batch.file_hash),
            action="import.pdf",
        )

    # -- public API: crypto exchange CSV ------------------------------------
    def import_crypto_csv(
        self, payload: bytes | str, filename: str, parsed: ParsedTradeFile | None = None
    ) -> ImportResult:
        """Parse and persist a crypto exchange trade export.

        An exchange trade is a swap, so one row can produce up to three
        movements — see :meth:`_crypto_legs`. Everything is booked in **dollars**
        for the reason given in :mod:`app.importer.crypto.symbols`: a coin is a
        globally priced, dollar-quoted holding, and treating it as one means the
        multi-currency machinery already in place carries it to reais without a
        second conversion path.
        """
        raw_bytes = payload if isinstance(payload, (bytes, bytearray)) else payload.encode("utf-8")
        batch = self._start_batch(raw_bytes, filename, "CRYPTO")

        try:
            if parsed is None:
                parsed = parse_crypto_csv(raw_bytes)
        except CryptoFormatError as exc:
            self._fail_batch(batch, str(exc))
            raise

        batch.source_format = parsed.format
        batch.broker_name = parsed.exchange
        batch.currency = "USD"
        batch.period_start = parsed.period_start
        batch.period_end = parsed.period_end

        state = _CryptoImportState()
        state.cancelled = parsed.skipped_rows
        # Only genuine per-row failures go into ``issues``; the file-level notes
        # are advice, not errors, and counting them would report a clean import
        # as having failed rows.
        state.issues.extend(parsed.errors)

        broker = self._broker_named(parsed.exchange, parsed.exchange)
        existing_keys = self._existing_keys()
        coverage = self._crypto_coverage(broker.id, parsed.format)
        occurrence_in_file: dict[str, int] = defaultdict(int)
        pending: list[Transaction] = []

        for trade in parsed.trades:
            try:
                legs = self._crypto_legs(trade, state)
                if not legs:
                    continue
                # The legs of one trade stand or fall together: booking the coin
                # bought without the stablecoin that paid for it would invent
                # capital, and removing a fee for a purchase that was already
                # imported would remove the same coins twice.
                if self._crypto_covered(coverage, legs[0]):
                    state.cross_source += 1
                    state.cross_source_detail[
                        f"{trade.trade_date} {trade.side} {trade.pair}"
                    ] += 1
                    continue

                for leg in legs:
                    row = self._crypto_transaction(
                        leg,
                        batch=batch,
                        broker=broker,
                        exchange=parsed.exchange,
                        identity=(trade.pair, trade.side, leg.tag),
                        source_line=trade.line_number,
                        existing_keys=existing_keys,
                        occurrence_in_file=occurrence_in_file,
                        state=state,
                    )
                    if row is not None:
                        pending.append(row)
                state.trades += 1
                state.pairs[trade.pair] += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not kill the import
                logger.exception("failed to import exchange trade %r", trade.raw_text[:120])
                state.issues.append(
                    {"line": trade.line_number, "error": str(exc), "raw": trade.raw_text[:200]}
                )

        for event in parsed.events:
            # Balance changes with no counterparty: rewards, deposits,
            # withdrawals, futures settlements. Only a ledger export has these,
            # and they are the whole reason it beats a trade history — without
            # them a staked coin looks like it was sold from nowhere.
            try:
                leg = self._crypto_event_leg(event)
                if self._crypto_covered(coverage, leg):
                    state.cross_source += 1
                    continue
                row = self._crypto_transaction(
                    leg,
                    batch=batch,
                    broker=broker,
                    exchange=parsed.exchange,
                    identity=(event.operation, event.direction, event.symbol),
                    source_line=event.line_number,
                    existing_keys=existing_keys,
                    occurrence_in_file=occurrence_in_file,
                    state=state,
                )
                if row is not None:
                    pending.append(row)
                state.events[leg.classification.op_type.value] += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not kill the import
                logger.exception("failed to import ledger movement %r", event.raw_text[:120])
                state.issues.append(
                    {"line": event.line_number, "error": str(exc), "raw": event.raw_text[:200]}
                )

        if pending:
            self.db.bulk_save_objects(pending)

        return self._finish_batch(
            batch,
            rows_total=parsed.total_rows,
            imported=state.imported,
            duplicates=state.duplicates + state.cross_source + state.cancelled,
            issues=state.issues,
            summary=state.summary(parsed, batch.file_hash),
            action="import.crypto",
        )

    def _crypto_transaction(
        self,
        leg: "_CryptoLeg",
        *,
        batch: ImportBatch,
        broker: Broker,
        exchange: str,
        identity: tuple[str, ...],
        source_line: int | None,
        existing_keys: set[str],
        occurrence_in_file: dict[str, int],
        state: "_CryptoImportState",
    ) -> Transaction | None:
        """Persist one leg, unless the portfolio already has it.

        ``identity`` is what makes two legs of the same movement distinguishable
        — the pair and side for a trade, the operation and coin for a ledger
        event — so the occurrence counter only ever separates genuine repeats.
        """
        fingerprint = exact_fingerprint(
            leg.trade_date.isoformat(),
            exchange,
            *identity,
            leg.asset.ticker,
            format(leg.quantity.normalize(), "f"),
            format(leg.gross_amount.normalize(), "f"),
        )
        occurrence = occurrence_in_file[fingerprint]
        occurrence_in_file[fingerprint] = occurrence + 1
        dedup_key = f"{fingerprint}:{occurrence}"
        if dedup_key in existing_keys:
            state.duplicates += 1
            return None

        existing_keys.add(dedup_key)
        state.imported += 1
        state.operations[leg.classification.op_type.value] += 1
        return Transaction(
            portfolio_id=self.portfolio.id,
            asset_id=leg.asset.id,
            broker_id=broker.id,
            import_batch_id=batch.id,
            trade_date=leg.trade_date,
            direction=leg.direction.value,
            op_type=leg.classification.op_type.value,
            effect=leg.classification.effect.value,
            quantity=leg.quantity,
            unit_price=leg.unit_price,
            gross_amount=leg.gross_amount,
            fees=leg.fees,
            taxes=Decimal(0),
            net_amount=_net_amount(leg.classification.effect, leg.gross_amount),
            currency=leg.currency,
            fx_rate=leg.fx_rate,
            raw_movement=leg.movement[:120],
            raw_product=leg.description[:255],
            raw_institution=exchange,
            source_line=source_line,
            dedup_key=dedup_key,
            occurrence=occurrence,
            notes=leg.note or leg.classification.warning,
        )

    def _crypto_legs(self, trade: CryptoTrade, state: "_CryptoImportState") -> list["_CryptoLeg"]:
        """Turn one swap into the movements it actually consists of.

        Up to three, and each is there for a reason:

        * the **instrument leg** — the coin the trade is about, acquired or
          disposed of;
        * the **funding leg** — what paid for it, when that is itself a holding.
          A purchase settled in Tether has to take those Tethers out of the
          portfolio; leaving them in would show a balance that was already
          spent, and would read the swap as fresh capital arriving;
        * the **fee leg** — when the exchange charged the fee in a *third* coin
          (Binance discounts fees paid in BNB), which is a small disposal of
          that coin and cannot be expressed as a cost on either of the others.

        A fee charged in the coin bought is not a leg: it never arrives, so the
        quantity acquired is simply reduced. A fee charged in the funding
        currency is money, so it lands on the instrument leg's ``fees``.
        """
        base, quote = trade.base_symbol, trade.quote_symbol
        legs: list[_CryptoLeg] = []

        quantity = trade.base_quantity
        fee_in_money = Decimal(0)
        fee_in_money_symbol = ""
        fee_legs: list[tuple[Decimal, str]] = []

        # A single trade can be charged in more than one coin — part in BNB,
        # part in the quote currency — so each fee is placed independently.
        for fee_amount, fee_symbol in trade.fees:
            if fee_amount <= 0 or not fee_symbol:
                continue
            if fee_symbol == base and trade.is_buy:
                # Charged out of what was bought: it never reaches the wallet.
                quantity -= fee_amount
            elif fee_symbol == quote or not coins.is_tracked(fee_symbol):
                fee_in_money += fee_amount
                fee_in_money_symbol = fee_symbol
            else:
                fee_legs.append((fee_amount, fee_symbol))

        if quantity <= 0:
            raise ValueError(f"trade leaves no quantity after the fee: {trade.raw_text[:120]}")

        gross, currency, rate = self._crypto_money(trade.quote_amount, quote, trade.trade_date)
        fees = Decimal(0)
        if fee_in_money > 0:
            fees, fee_currency, _ = self._crypto_money(
                fee_in_money, fee_in_money_symbol, trade.trade_date
            )
            if fee_currency != currency:
                # A fee charged in something the trade was not priced in cannot
                # be added to its amount without inventing a rate. Reported
                # rather than folded in at the wrong scale.
                state.unconverted_fees += 1
                fees = Decimal(0)

        if currency != "USD":
            state.native_currency[currency] += 1

        legs.append(
            self._crypto_leg(
                symbol=base,
                movement="Buy" if trade.is_buy else "Sell",
                direction=Direction.CREDIT if trade.is_buy else Direction.DEBIT,
                quantity=quantity,
                gross=gross,
                fees=fees,
                currency=currency,
                fx_rate=rate,
                trade=trade,
                tag="base",
            )
        )

        if coins.is_tracked(quote):
            # What was spent (or received) on the other side of the swap. The
            # fee rides along when it was charged in this same currency: paying
            # 100 USDT plus 0,1 USDT of fee moves 100,1 USDT.
            quote_fee = trade.fee_in(quote)
            quote_quantity = trade.quote_amount + (quote_fee if trade.is_buy else -quote_fee)
            if quote_quantity > 0:
                quote_gross, quote_currency, quote_rate = self._crypto_money(
                    quote_quantity, quote, trade.trade_date
                )
                legs.append(
                    self._crypto_leg(
                        symbol=quote,
                        movement="Sell" if trade.is_buy else "Buy",
                        direction=Direction.DEBIT if trade.is_buy else Direction.CREDIT,
                        quantity=quote_quantity,
                        gross=quote_gross,
                        fees=Decimal(0),
                        currency=quote_currency,
                        fx_rate=quote_rate,
                        trade=trade,
                        tag="quote",
                    )
                )

        for fee_amount, fee_symbol in fee_legs:
            legs.append(
                self._crypto_leg(
                    symbol=fee_symbol,
                    movement="Trading fee",
                    direction=Direction.DEBIT,
                    quantity=fee_amount,
                    gross=Decimal(0),
                    fees=Decimal(0),
                    currency="USD",
                    fx_rate=self._crypto_rate("USD", trade.trade_date),
                    trade=trade,
                    tag=f"fee:{fee_symbol}",
                )
            )
        return legs

    def _crypto_event_leg(self, event: CryptoEvent) -> "_CryptoLeg":
        """A ledger movement that is not a trade, as a single leg.

        Rewards, deposits, withdrawals and futures settlements carry no
        counterparty, so there is nothing to fund them from — the label alone
        decides what the engine does with the quantity.
        """
        direction = Direction.CREDIT if event.is_credit else Direction.DEBIT
        gross, currency, rate = self._crypto_money(
            event.gross, event.currency, event.trade_date
        )
        asset = self._crypto_asset(event.symbol)
        classification = classify(event.movement, direction, gross)
        return _CryptoLeg(
            asset=asset,
            trade_date=event.trade_date,
            tag="event",
            movement=event.movement,
            direction=direction,
            classification=classification,
            quantity=event.quantity,
            unit_price=gross / event.quantity if event.quantity else Decimal(0),
            gross_amount=gross,
            fees=Decimal(0),
            currency=currency,
            fx_rate=rate,
            description=f"{coins.coin_name(event.symbol)} ({event.operation})"
            if event.operation
            else coins.coin_name(event.symbol),
            note=event.operation or None,
        )

    def _crypto_leg(
        self,
        *,
        symbol: str,
        movement: str,
        direction: Direction,
        quantity: Decimal,
        gross: Decimal,
        fees: Decimal,
        currency: str,
        fx_rate: Decimal | None,
        trade: CryptoTrade,
        tag: str,
    ) -> "_CryptoLeg":
        asset = self._crypto_asset(symbol)
        classification = classify(movement, direction, gross)
        unit_price = gross / quantity if quantity else Decimal(0)
        return _CryptoLeg(
            asset=asset,
            trade_date=trade.trade_date,
            tag=tag,
            movement=movement,
            direction=direction,
            classification=classification,
            quantity=quantity,
            unit_price=unit_price,
            gross_amount=gross,
            fees=fees,
            currency=currency,
            fx_rate=fx_rate,
            description=f"{coins.coin_name(symbol)} ({trade.pair})" if trade.pair else coins.coin_name(symbol),
            note=(
                None
                if currency == "USD"
                else f"movimento registrado em {currency}: o par {trade.pair} não é cotado em dólar"
            ),
        )

    def _crypto_asset(self, symbol: str) -> Asset:
        """The asset for a coin, kept apart from a share of the same name."""
        symbol = symbol.upper()
        ticker = symbol
        existing = self._asset_cache.get(ticker) or self.db.scalar(
            select(Asset).where(Asset.ticker == ticker)
        )
        if existing is not None and existing.kind not in CRYPTO_KINDS:
            ticker = f"{symbol}{CRYPTO_TICKER_SUFFIX}"
        return self._asset_for(
            ticker=ticker,
            name=coins.coin_name(symbol),
            kind=coins.coin_kind(symbol).value,
            currency="USD",
            market_symbol=coins.market_symbol_for(symbol),
        )

    def _crypto_money(
        self, amount: Decimal, symbol: str, day: date
    ) -> tuple[Decimal, str, Decimal | None]:
        """Express an exchange amount in the currency the movement is booked in.

        Dollar-pegged tokens *are* dollars — a hundred USDT is a hundred dollars,
        which is the whole point of holding them — so a trade settled in one is
        booked in USD directly. Reais are converted at that day's PTAX, and the
        rate is stored on the movement, so the base-currency replay converts it
        straight back to the reais actually paid.

        Anything else (euros, or a pair quoted in Bitcoin) is left in its own
        currency with whatever rate is on file. When no rate exists yet the
        movement still imports — ``backfill_transaction_fx`` fills it in as soon
        as one does, which is the same path a statement imported before the PTAX
        series existed already takes.
        """
        upper = (symbol or "USD").upper()
        base = self.portfolio.base_currency.upper()
        if upper == "USD" or coins.is_stablecoin(upper):
            return amount, "USD", self._crypto_rate("USD", day)
        if upper == base:
            rate = self._crypto_rate("USD", day)
            if rate:
                return amount / rate, "USD", rate
            return amount, base, None
        return amount, upper, self._crypto_rate(upper, day)

    def _crypto_rate(self, currency: str, day: date) -> Decimal | None:
        """Rate from ``currency`` to the portfolio's base, on ``day``."""
        base = self.portfolio.base_currency.upper()
        if currency.upper() == base:
            return None
        table = self._fx_cache.get(currency.upper())
        if table is None:
            from app.market.fx import load_table  # local import avoids a cycle

            table = self._fx_cache[currency.upper()] = load_table(self.db, currency, base)
        return None if table.is_empty else table.rate_on(day)

    def _crypto_coverage(
        self, broker_id: int, source_format: str
    ) -> dict[tuple, "_CryptoCoverage"]:
        """How much of each day is already accounted for by the *other* export.

        Binance publishes the same trades more than once — per fill, per order,
        and again in the account ledger — and a user who exports several tabs
        would otherwise buy everything twice. The files cannot be matched row
        for row (an order splits into several fills), so the day's total per
        asset and direction is what gets reconciled, and only ever against rows
        from a *different* export: inside one file, a repeat is a real repeat.

        Both the **quantity** and the **value** are tracked, because neither
        survives every pair of exports on its own:

        * The order history has no fee column, so it reports the gross quantity
          bought while the trade history reports it net of a fee paid in the
          same coin — 0.0565 BNB against 0.05645763. Their *values* are
          identical to the last decimal.
        * A pair that is not quoted in dollars has no comparable value. The one
          UNI/BNB trade here is written ``UNIBNB`` by the trade history and
          ``BNBUNI`` by the ledger, so one prices it in BNB and the other in
          UNI. Their *quantities* are identical to the last digit.

        Matching on value alone let that second trade in twice, and 0.109 BNB
        that had been sold years earlier sat on the books until the exchange's
        own balance was checked. Matching on quantity alone breaks the first
        case. So either dimension may carry the match, and both are consumed
        when one does.

        Values are held per currency and only ever compared within one, because
        the exports do not agree on the unit either: a DOT bought on ``DOTBRL``
        is priced in reais by the trade history and in dollars by the ledger,
        and letting those match wrote off 1.65 real DOT against a number that
        merely looked similar.
        """
        rows = self.db.execute(
            select(
                Transaction.asset_id,
                Transaction.trade_date,
                Transaction.op_type,
                Transaction.currency,
                func.sum(Transaction.quantity),
                func.sum(Transaction.gross_amount),
            )
            .join(ImportBatch, ImportBatch.id == Transaction.import_batch_id)
            .where(
                Transaction.portfolio_id == self.portfolio.id,
                Transaction.broker_id == broker_id,
                ImportBatch.source_kind == "CRYPTO",
                ImportBatch.source_format.isnot(None),
                ImportBatch.source_format != source_format,
            )
            .group_by(
                Transaction.asset_id,
                Transaction.trade_date,
                Transaction.op_type,
                Transaction.currency,
            )
        ).all()
        coverage: dict[tuple, _CryptoCoverage] = {}
        for asset_id, day, op_type, currency, quantity, amount in rows:
            entry = coverage.setdefault((asset_id, day, op_type), _CryptoCoverage())
            entry.quantity += _q(quantity)
            entry.amounts[(currency or "USD").upper()] += _q(amount)
        return coverage

    @staticmethod
    def _crypto_covered(coverage: dict[tuple, "_CryptoCoverage"], leg: "_CryptoLeg") -> bool:
        """Consume this leg from the other export's totals, if it is there.

        A ledger row that moves coins without pricing them — most of them, since
        a reward or a transfer has no counterparty — has no value to match on,
        which is exactly why the quantity has to be able to carry the match.

        Keyed on the *operation*, not on its position effect: a trading fee and
        a fee rebate are both ``QTY_OUT_FREE``, and pooling them let 15 rebates
        be written off against leftover fee quantity that had nothing to do with
        them.
        """
        remaining = coverage.get(
            (leg.asset.id, leg.trade_date, leg.classification.op_type.value)
        )
        if remaining is None:
            return False
        floor = Decimal(1) - _CRYPTO_MATCH_TOLERANCE
        currency = (leg.currency or "USD").upper()
        matched = (leg.quantity > 0 and remaining.quantity >= leg.quantity * floor) or (
            leg.gross_amount > 0
            and remaining.amounts[currency] >= leg.gross_amount * floor
        )
        if not matched:
            return False
        remaining.quantity -= leg.quantity
        remaining.amounts[currency] -= leg.gross_amount
        return True

    def _unidentified(self, row: StatementRow, state: "_PdfImportState") -> str | None:
        """Decide what to do with a row whose security could not be resolved.

        A movement that carries quantity is imported under a provisional code:
        dropping it would silently corrupt the position held, and an obviously
        odd ticker is something the user can see and fix. A cash-only row has no
        position to corrupt and nothing to attach to — Avenue's April 2025
        statement lists 27 withholding reversals with the ticker column simply
        left blank — so it is reported and left out rather than inventing an
        asset for it.
        """
        if row.quantity:
            state.provisional += 1
            state.issues.append(
                {
                    "line": row.page_number,
                    "error": "ativo não identificado, importado com código provisório: "
                    + row.raw_text[:140],
                }
            )
            return provisional_ticker(row.description, row.cusip)
        state.unattributed += 1
        state.unattributed_amount += row.amount
        state.unattributed_detail[f"{row.movement}: {row.description[:60]}"] += 1
        return None

    def _fuzzy_index(self, statement: ParsedStatement) -> FuzzyIndex:
        """Index movements this broker already has from a *different* report.

        Restricted to the statement's own period (plus a few days' slack) so the
        comparison stays small and cannot reach into an unrelated month that
        happens to look similar.
        """
        index = FuzzyIndex()
        if statement.period_start is None or statement.period_end is None:
            return index

        rows = self.db.execute(
            select(
                Transaction.trade_date,
                Transaction.op_type,
                Transaction.effect,
                Transaction.quantity,
                Transaction.gross_amount,
                Asset.ticker,
                Broker.canonical_name,
            )
            .join(Asset, Asset.id == Transaction.asset_id)
            .join(Broker, Broker.id == Transaction.broker_id)
            .join(ImportBatch, ImportBatch.id == Transaction.import_batch_id)
            .where(
                Transaction.portfolio_id == self.portfolio.id,
                Broker.canonical_name == statement.broker,
                Transaction.trade_date >= statement.period_start - _FUZZY_LOOKBACK,
                Transaction.trade_date <= statement.period_end + _FUZZY_LOOKBACK,
                or_(
                    ImportBatch.source_format.is_(None),
                    ImportBatch.source_format != statement.format,
                ),
            )
        ).all()

        for trade_date, op_type, effect, quantity, gross, ticker, broker_name in rows:
            index.add(
                fuzzy_key(broker_name, op_type, effect, ticker, _q(quantity), _q(gross)), trade_date
            )
        return index

    def _fx_table(self, currency: str):
        """PTAX rates for converting a foreign statement into the base currency."""
        if currency.upper() == self.portfolio.base_currency.upper():
            return None
        from app.market.fx import load_table  # local import avoids a cycle

        table = load_table(self.db, currency, self.portfolio.base_currency)
        return None if table.is_empty else table


@dataclass(slots=True)
class _CryptoCoverage:
    """What one asset-day-operation still has unclaimed in another export.

    Values are per currency: the exports price the same trade in whatever the
    pair was quoted in, so only same-currency amounts are comparable.
    """

    quantity: Decimal = Decimal(0)
    amounts: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))


@dataclass(slots=True)
class _CryptoLeg:
    """One side of an exchange swap, ready to become a transaction."""

    asset: Asset
    trade_date: date
    #: "base" (the instrument), "quote" (what funded it) or "fee".
    tag: str
    movement: str
    direction: Direction
    classification: Classification
    quantity: Decimal
    unit_price: Decimal
    gross_amount: Decimal
    fees: Decimal
    currency: str
    fx_rate: Decimal | None
    description: str
    note: str | None = None


@dataclass(slots=True)
class _CryptoImportState:
    """Counters accumulated while importing one exchange export."""

    imported: int = 0
    #: Swaps read (not rows: one swap can produce three movements).
    trades: int = 0
    duplicates: int = 0
    #: Trades the exchange's other export already reported.
    cross_source: int = 0
    #: Orders that never executed.
    cancelled: int = 0
    #: Fees charged in a currency the trade was not priced in.
    unconverted_fees: int = 0
    issues: list[dict] = field(default_factory=list)
    operations: Counter = field(default_factory=Counter)
    pairs: Counter = field(default_factory=Counter)
    cross_source_detail: Counter = field(default_factory=Counter)
    #: Movements that could not be expressed in dollars, by currency.
    native_currency: Counter = field(default_factory=Counter)
    #: Non-trade ledger movements, by operation type.
    events: Counter = field(default_factory=Counter)

    def summary(self, parsed: ParsedTradeFile, file_hash: str) -> dict:
        # A ledger export reports everything and needs no caveat; a spot export
        # is trades only, and reading its numbers without knowing that is the
        # difference between "I lost money" and "half the history is missing".
        note = (
            "movimentos não negociais (depósitos, saques, recompensas, Earn, futuros) "
            "importados a partir do extrato completo"
            if parsed.events
            else "a exportação spot cobre apenas negociações: depósitos, saques, conversões, "
            "Earn e compras por cartão não constam e podem deixar posições sem custo"
        )
        return {
            "operations": dict(self.operations),
            "warnings": [{"message": message, "count": 1} for message in parsed.warnings[:50]],
            "unknown_movements": [],
            "date_range": {
                "start": parsed.period_start.isoformat() if parsed.period_start else None,
                "end": parsed.period_end.isoformat() if parsed.period_end else None,
            },
            "file_hash": file_hash,
            "exchange": {
                "name": parsed.exchange,
                "format": parsed.format,
                "trades": self.trades,
                "pairs": [{"pair": p, "count": c} for p, c in self.pairs.most_common(50)],
                "native_currency": [
                    {"currency": c, "count": n} for c, n in self.native_currency.most_common(10)
                ],
                "events": dict(self.events),
                "note": note,
            },
            "skipped": {
                "cancelled_orders": self.cancelled,
                "cross_source_duplicates": self.cross_source,
                "cross_source_detail": [
                    {"movement": m, "count": c} for m, c in self.cross_source_detail.most_common(50)
                ],
                "unconverted_fees": self.unconverted_fees,
            },
        }


@dataclass(slots=True)
class _PdfImportState:
    """Counters accumulated while importing one statement."""

    imported: int = 0
    duplicates: int = 0
    #: Rows this broker already reported in a statement of another format.
    cross_source: int = 0
    #: Deposits, withdrawals, journals and money-market sweeps.
    cash_rows: int = 0
    #: Quantity-bearing rows imported under a provisional ticker.
    provisional: int = 0
    #: Cash rows the statement named no security for.
    unattributed: int = 0
    unattributed_amount: Decimal = Decimal(0)
    issues: list[dict] = field(default_factory=list)
    operations: Counter = field(default_factory=Counter)
    warnings: Counter = field(default_factory=Counter)
    cross_source_detail: Counter = field(default_factory=Counter)
    unattributed_detail: Counter = field(default_factory=Counter)

    def summary(self, statement: ParsedStatement, file_hash: str) -> dict:
        return {
            "operations": dict(self.operations),
            "warnings": [{"message": m, "count": c} for m, c in self.warnings.most_common(50)],
            "unknown_movements": [],
            "date_range": {
                "start": statement.period_start.isoformat() if statement.period_start else None,
                "end": statement.period_end.isoformat() if statement.period_end else None,
            },
            "file_hash": file_hash,
            "statement": statement.summary(),
            "skipped": {
                "cash_movements": self.cash_rows,
                "cross_source_duplicates": self.cross_source,
                "cross_source_detail": [
                    {"movement": m, "count": c} for m, c in self.cross_source_detail.most_common(50)
                ],
                "unattributed": self.unattributed,
                "unattributed_amount": str(self.unattributed_amount),
                "unattributed_detail": [
                    {"movement": m, "count": c} for m, c in self.unattributed_detail.most_common(20)
                ],
                "provisional_tickers": self.provisional,
            },
        }
