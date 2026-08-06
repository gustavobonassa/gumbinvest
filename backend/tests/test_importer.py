"""Import behaviour: persistence, de-duplication and monthly merges."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import Asset, Broker, ImportBatch, Transaction
from app.domain.enums import OperationType
from app.importer.service import ImportService, reclassify_transactions
from tests.conftest import SAMPLE_CSV_PATH, requires_sample_csv

HEADER = "Entrada/Saída,Data,Movimentação,Produto,Instituição,Quantidade,Preço unitário,Valor da Operação\n"

INITIAL = HEADER + (
    'Credito,10/01/2024,Transferência - Liquidação,PETR4 - PETROLEO BRASILEIRO S.A.,XP INVESTIMENTOS CCTVM S/A,100," R$ 30,00 "," R$ 3.000,00 "\n'
    'Credito,15/02/2024,Dividendo,PETR4 - PETROLEO BRASILEIRO S.A.,XP INVESTIMENTOS CCTVM S/A,100," R$ 1,00 "," R$ 100,00 "\n'
)

MONTHLY = HEADER + (
    # repeats the first file (must be ignored) ...
    'Credito,10/01/2024,Transferência - Liquidação,PETR4 - PETROLEO BRASILEIRO S.A.,XP INVESTIMENTOS CCTVM S/A,100," R$ 30,00 "," R$ 3.000,00 "\n'
    'Credito,15/02/2024,Dividendo,PETR4 - PETROLEO BRASILEIRO S.A.,XP INVESTIMENTOS CCTVM S/A,100," R$ 1,00 "," R$ 100,00 "\n'
    # ... plus one genuinely new movement
    'Debito,20/03/2024,Transferência - Liquidação,PETR4 - PETROLEO BRASILEIRO S.A.,XP INVESTIMENTOS CCTVM S/A,40," R$ 35,00 "," R$ 1.400,00 "\n'
)

TWIN_ROWS = HEADER + (
    'Credito,01/04/2025,Juros Sobre Capital Próprio,ITSA4 - ITAUSA S.A.,XP INVESTIMENTOS CCTVM S/A,1438," R$ 0,024 "," R$ 28,76 "\n'
    'Credito,01/04/2025,Juros Sobre Capital Próprio,ITSA4 - ITAUSA S.A.,XP INVESTIMENTOS CCTVM S/A,1438," R$ 0,024 "," R$ 28,76 "\n'
)


def _import(db, portfolio, payload: str, name: str = "movimentacao.csv"):
    return ImportService(db, portfolio).import_csv(payload.encode("utf-8"), name)


def test_import_persists_normalised_transactions(db, portfolio):
    result = _import(db, portfolio, INITIAL)
    assert result.rows_total == 2
    assert result.rows_imported == 2
    assert result.rows_failed == 0

    asset = db.scalar(select(Asset).where(Asset.ticker == "PETR4"))
    assert asset is not None and asset.kind == "STOCK"
    assert db.scalar(select(Broker.canonical_name)) == "XP Investimentos"

    buy = db.scalar(select(Transaction).where(Transaction.op_type == OperationType.BUY.value))
    assert buy.quantity == Decimal("100")
    assert buy.unit_price == Decimal("30")
    assert buy.gross_amount == Decimal("3000")
    assert buy.net_amount == Decimal("-3000")  # cash left the account

    dividend = db.scalar(select(Transaction).where(Transaction.op_type == OperationType.DIVIDEND.value))
    assert dividend.net_amount == Decimal("100")


def test_reimporting_the_same_file_changes_nothing(db, portfolio):
    _import(db, portfolio, INITIAL)
    second = _import(db, portfolio, INITIAL)
    assert second.rows_imported == 0
    assert second.rows_duplicate == 2
    assert db.scalar(select(func.count()).select_from(Transaction)) == 2


def test_monthly_file_merges_only_new_rows(db, portfolio):
    _import(db, portfolio, INITIAL)
    second = _import(db, portfolio, MONTHLY, "movimentacao-marco.csv")
    assert second.rows_imported == 1
    assert second.rows_duplicate == 2
    assert db.scalar(select(func.count()).select_from(Transaction)) == 3
    assert db.scalar(select(func.count()).select_from(ImportBatch)) == 2


def test_genuinely_repeated_movements_are_both_kept(db, portfolio):
    """Two identical payments on one day are real — occurrence counters keep both."""
    result = _import(db, portfolio, TWIN_ROWS)
    assert result.rows_imported == 2
    keys = db.scalars(select(Transaction.dedup_key)).all()
    assert len({k.rsplit(":", 1)[0] for k in keys}) == 1
    assert sorted(int(k.rsplit(":", 1)[1]) for k in keys) == [0, 1]

    # ...and re-importing that same file still adds nothing.
    again = _import(db, portfolio, TWIN_ROWS)
    assert again.rows_imported == 0
    assert db.scalar(select(func.count()).select_from(Transaction)) == 2


def test_broker_spelling_variants_do_not_duplicate(db, portfolio):
    variant = INITIAL.replace("XP INVESTIMENTOS CCTVM S/A", "XP INVESTIMENTOS CCTVM S/A.")
    _import(db, portfolio, INITIAL)
    second = _import(db, portfolio, variant)
    assert second.rows_imported == 0
    assert db.scalar(select(func.count()).select_from(Broker)) == 1


def test_unknown_movement_is_recorded_and_reported(db, portfolio):
    payload = HEADER + (
        'Credito,10/01/2024,Evento Espacial,PETR4 - PETROLEO BRASILEIRO S.A.,XP INVESTIMENTOS CCTVM S/A,1," R$ 1,00 "," R$ 1,00 "\n'
    )
    result = _import(db, portfolio, payload)
    assert result.rows_imported == 1
    assert result.summary["unknown_movements"][0]["movement"] == "Evento Espacial"
    transaction = db.scalar(select(Transaction))
    assert transaction.op_type == OperationType.UNKNOWN.value
    assert transaction.effect == "NONE"


def test_import_log_summarises_operations(db, portfolio):
    result = _import(db, portfolio, INITIAL)
    assert result.summary["operations"] == {"BUY": 1, "DIVIDEND": 1}
    assert result.summary["date_range"] == {"start": "2024-01-10", "end": "2024-02-15"}


def test_reclassification_repairs_rows_imported_under_older_rules(db, portfolio):
    """`op_type`/`effect` are derived, so a classifier fix must reach old rows.

    De-duplication means re-uploading the file cannot repair them, which is why
    the app re-derives both columns on start.
    """
    _import(db, portfolio, INITIAL)
    stale = db.scalar(select(Transaction).where(Transaction.op_type == OperationType.BUY.value))
    stale.op_type = "UNKNOWN"
    stale.effect = "NONE"
    stale.net_amount = Decimal(0)
    db.commit()

    result = reclassify_transactions(db, portfolio.id)
    assert result["updated"] == 1

    db.refresh(stale)
    assert stale.op_type == OperationType.BUY.value
    assert stale.effect == "ACQUIRE"
    assert stale.net_amount == Decimal("-3000")

    # Running it again changes nothing.
    assert reclassify_transactions(db, portfolio.id)["updated"] == 0


@requires_sample_csv
def test_real_export_imports_cleanly(db, portfolio):
    """End-to-end run over the full reference export."""
    service = ImportService(db, portfolio)
    result = service.import_csv(SAMPLE_CSV_PATH.read_bytes(), SAMPLE_CSV_PATH.name)

    assert result.rows_failed == 0
    assert result.rows_imported == result.rows_total
    assert not result.summary["unknown_movements"], result.summary["unknown_movements"]

    # Re-import must be a complete no-op.
    again = ImportService(db, portfolio).import_csv(SAMPLE_CSV_PATH.read_bytes(), SAMPLE_CSV_PATH.name)
    assert again.rows_imported == 0
    assert again.rows_duplicate == result.rows_total
