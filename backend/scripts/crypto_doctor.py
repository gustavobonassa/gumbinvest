"""Explain — and if necessary rebuild — the crypto side of a live database.

Positions are derived, so "why is this number what it is" always has an answer
in the movements behind it. This prints that answer for one asset, which is
faster than reasoning about it from the outside.

    docker compose exec backend python scripts/crypto_doctor.py
    docker compose exec backend python scripts/crypto_doctor.py --ticker BTC

``--reimport`` drops every crypto import batch and the movements that came with
it, then reads the exports under ``AUTO_IMPORT_DIR`` again. That is the honest
repair for a database that accumulated rows from more than one version of the
importer: de-duplication keys are derived from how a file was read, so a row
imported under older rules is invisible to the new ones and survives as a
duplicate. Nothing else is touched — the B3 history and the broker statements
are left exactly where they are.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

# Run as ``python scripts/crypto_doctor.py``, so the interpreter puts *this*
# directory on the path rather than the backend root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.models import Asset, ImportBatch, Transaction  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.domain.enums import Direction  # noqa: E402
from app.importer.classifier import classify  # noqa: E402
from app.portfolio.service import PortfolioService  # noqa: E402
from app.services.portfolio_registry import get_default_portfolio  # noqa: E402


def report(ticker: str) -> None:
    with session_scope() as db:
        portfolio = get_default_portfolio(db)

        batches = db.scalars(
            select(ImportBatch)
            .where(ImportBatch.source_kind == "CRYPTO")
            .order_by(ImportBatch.id)
        ).all()
        print("crypto imports on file:")
        if not batches:
            print("   none — no exchange export has been imported into this database")
        for batch in batches:
            print(
                f"   {batch.source_format or '?':22} {batch.rows_imported:>6} imported  "
                f"{batch.filename[:44]}"
            )

        # The startup reclassification derives these from the movement label, so
        # a stale image shows the old effects here and nothing else will explain
        # why a staked balance is missing.
        print(
            "\n'Earn transfer' currently maps to:",
            classify("Earn transfer", Direction.DEBIT).effect.value,
            "/",
            classify("Earn transfer", Direction.CREDIT).effect.value,
            "(expected QTY_OUT_STAKED / QTY_IN_STAKED)",
        )

        asset = db.scalar(
            select(Asset).where(Asset.ticker.in_([ticker, f"{ticker}.CRYPTO"]))
        )
        if asset is None:
            print(f"\nno {ticker} asset exists in this database")
            return

        rows = db.scalars(select(Transaction).where(Transaction.asset_id == asset.id)).all()
        print(f"\n{asset.ticker} ({asset.kind}, {asset.currency}): {len(rows)} movements")
        for (op_type, effect), count in Counter((t.op_type, t.effect) for t in rows).most_common():
            print(f"   {op_type:16} {effect:16} {count:>6}")

        position = PortfolioService(db, portfolio.id).positions().get(asset.id)
        if position is None:
            print("   no position")
            return
        print(
            f"\n   quantity {position.quantity} (staked {position.staked_quantity})"
            f"  cost {position.cost_basis:.2f}  open={position.is_open}"
        )
        for note in position.notes:
            print(f"   note: {note}")
        for warning in position.warnings:
            print(f"   warning: {warning}")


def reimport() -> None:
    from app.importer.service import (
        ImportService,
        reclassify_assets,
        reclassify_transactions,
        reconcile_market_symbols,
    )
    from app.main import _ordered_csvs

    directory = Path(settings.auto_import_dir)
    if not directory.is_dir():
        print(f"{directory} is not a directory — nothing to re-import")
        return

    with session_scope() as db:
        portfolio = get_default_portfolio(db)
        batch_ids = list(
            db.scalars(select(ImportBatch.id).where(ImportBatch.source_kind == "CRYPTO")).all()
        )
        if batch_ids:
            removed = db.execute(
                delete(Transaction).where(Transaction.import_batch_id.in_(batch_ids))
            ).rowcount
            db.execute(delete(ImportBatch).where(ImportBatch.id.in_(batch_ids)))
            db.commit()
            print(f"removed {removed} movements from {len(batch_ids)} crypto imports")

        service = ImportService(db, portfolio)
        for path, crypto_format in _ordered_csvs(
            sorted(p for p in directory.rglob("*.csv") if p.is_file())
        ):
            if not crypto_format:
                continue
            result = service.import_crypto_csv(path.read_bytes(), path.name)
            print(
                f"{crypto_format:22} {result.rows_imported:>6} new  "
                f"{result.rows_duplicate:>6} duplicate  {path.name[:40]}"
            )

        reclassify_transactions(db, portfolio.id)
        reclassify_assets(db)
        reconcile_market_symbols(db)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="USDT", help="asset to explain (default: USDT)")
    parser.add_argument(
        "--reimport",
        action="store_true",
        help="drop every crypto import and read the exports again",
    )
    args = parser.parse_args()

    if args.reimport:
        reimport()
        print()
    report(args.ticker.upper())
    return 0


if __name__ == "__main__":
    sys.exit(main())
