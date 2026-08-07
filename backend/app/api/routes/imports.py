"""Upload endpoints, import history and broker statement coverage."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.api.deps import CurrentPortfolio, DbSession
from app.core.dates import local_today
from app.db.models import ImportBatch
from app.importer.coverage import statement_coverage
from app.importer.crypto import CryptoFormatError, sniff_format
from app.importer.crypto.registry import available_formats as available_crypto_formats
from app.importer.parser import CsvFormatError, is_xlsx
from app.importer.pdf import PdfFormatError
from app.importer.pdf.registry import available_formats
from app.importer.service import ImportService, reclassify_transactions
from app.services.full_backup import (
    FullBackupError,
    export_snapshot,
    import_snapshot,
    is_full_backup,
)

router = APIRouter(prefix="/imports", tags=["imports"])

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
#: PDFs are parsed page by page in memory, so they get a tighter cap.
MAX_PDF_BYTES = 32 * 1024 * 1024


@router.post("", response_model=None, summary="Upload a B3 CSV or a broker statement PDF")
async def upload(db: DbSession, portfolio: CurrentPortfolio, file: UploadFile = File(...)) -> dict:
    """One endpoint for every source; the file itself decides which importer runs.

    The distinction is entirely mechanical — a PDF starts with ``%PDF-``, and a
    CSV is told apart from an exchange export by its header row — so it is made
    here rather than by asking the user to pick the right upload box.
    """
    payload = await file.read()
    filename = file.filename or "upload"
    is_pdf = payload[:5] == b"%PDF-" or filename.lower().endswith(".pdf")
    # A spreadsheet is binary, so it must never be offered to the exchange
    # sniffer — that decodes the bytes as text and would be reading zip noise.
    is_spreadsheet = is_xlsx(payload)
    limit = MAX_PDF_BYTES if is_pdf else MAX_UPLOAD_BYTES

    if not payload:
        raise HTTPException(status_code=400, detail="empty file")
    if len(payload) > limit:
        raise HTTPException(
            status_code=413, detail=f"file larger than {limit // (1024 * 1024)} MB"
        )

    # An encrypted cloud backup needs its passphrase, which this endpoint has
    # no way to ask for — point at the flow that does before the CSV sniffer
    # produces a confusing error about it.
    from app.services.cloud_backup import is_encrypted

    if is_encrypted(payload):
        raise HTTPException(
            status_code=422,
            detail=(
                "este arquivo está criptografado, restaure-o pela aba Backup em "
                "Configurações, onde a senha pode ser informada"
            ),
        )

    # A .gumbinvest file is a whole-database clone from another instance —
    # it replaces everything, so it never goes through the row importers.
    if is_full_backup(payload, filename):
        try:
            return import_snapshot(db, payload, filename)
        except FullBackupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    service = ImportService(db, portfolio)
    try:
        if is_pdf:
            result = service.import_pdf(payload, filename)
        elif not is_spreadsheet and sniff_format(payload):
            result = service.import_crypto_csv(payload, filename)
        else:
            result = service.import_csv(payload, filename)
    except (CsvFormatError, PdfFormatError, CryptoFormatError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return asdict(result)


@router.get("/export", response_model=None, summary="Download the whole database as a .gumbinvest file")
def export_full(db: DbSession) -> StreamingResponse:
    """Everything, one file — made to move a history into another instance.

    Typically: export here (Docker), drop the file on the Importar page of a
    fresh desktop install. The import side refuses non-empty targets.
    """
    payload = export_snapshot(db)
    filename = f"gumbinvest-{local_today().isoformat()}.gumbinvest"
    return StreamingResponse(
        iter([payload]),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/coverage", response_model=None, summary="Statement coverage and gaps per account")
def coverage(db: DbSession, portfolio: CurrentPortfolio) -> dict:
    """Which broker statements are held, and what looks missing.

    Reports three independent signals — months with no statement, balances that
    do not carry from one month into the next, and positions that disagree with
    the broker's own reported holdings. See :mod:`app.importer.coverage`.
    """
    return {
        "accounts": statement_coverage(db, portfolio.id),
        "formats": [*available_formats(), *available_crypto_formats()],
    }


@router.post(
    "/reclassify",
    response_model=None,
    summary="Re-derive operation types and effects for stored transactions",
)
def reclassify(db: DbSession, portfolio: CurrentPortfolio) -> dict:
    """Apply the current classifier rules to movements imported earlier."""
    return reclassify_transactions(db, portfolio.id)


@router.get("", response_model=None, summary="Import history (paginated)")
def list_imports(
    db: DbSession,
    portfolio: CurrentPortfolio,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=5, ge=1, le=100),
) -> dict:
    where = ImportBatch.portfolio_id == portfolio.id
    total = db.scalar(select(func.count()).select_from(ImportBatch).where(where)) or 0
    batches = db.scalars(
        select(ImportBatch)
        .where(where)
        .order_by(ImportBatch.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max((total + page_size - 1) // page_size, 1),
        "items": [
            {
                "id": b.id,
                "filename": b.filename,
                "status": b.status,
                "rows_total": b.rows_total,
                "rows_imported": b.rows_imported,
                "rows_duplicate": b.rows_duplicate,
                "rows_failed": b.rows_failed,
                "created_at": b.created_at,
                "finished_at": b.finished_at,
                "summary": b.summary,
            }
            for b in batches
        ],
    }


@router.get("/{batch_id}", response_model=None, summary="Full log for one import")
def import_detail(batch_id: int, db: DbSession) -> dict:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="import not found")
    return {
        "id": batch.id,
        "filename": batch.filename,
        "file_hash": batch.file_hash,
        "status": batch.status,
        "rows_total": batch.rows_total,
        "rows_imported": batch.rows_imported,
        "rows_duplicate": batch.rows_duplicate,
        "rows_failed": batch.rows_failed,
        "issues": batch.issues,
        "summary": batch.summary,
        "created_at": batch.created_at,
        "finished_at": batch.finished_at,
    }
