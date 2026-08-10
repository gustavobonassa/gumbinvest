"""FastAPI application entrypoint."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.api.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.models import AppSetting
from app.db.session import session_scope
from app.importer.parser import CsvFormatError
from app.importer.service import (
    ImportService,
    reclassify_assets,
    reclassify_transactions,
    reconcile_market_symbols,
    reconcile_ticker_aliases,
)
from app.market.crypto import sync_crypto_fx
from app.market.fixed_income import ensure_terms_for_fixed_income
from app.market.fx import backfill_transaction_fx
from app.services.portfolio_registry import get_default_portfolio
from app.services.secrets import apply_stored_secrets

configure_logging()
logger = get_logger(__name__)


#: Statement formats, best first. When two reports cover the same month the
#: richer one is imported first, so the other only fills in what it is missing:
#: Apex prices a trade including its commission, while Avenue's own report
#: carries dividends that the Apex file leaves out entirely.
_FORMAT_PRIORITY = {"apex-en": 0, "apex-ascend": 0, "drivewealth": 1, "avenue-pt": 2}

#: Same idea for the exchange exports. The transaction history is the whole
#: account — deposits, Earn, staking, futures, the lot — while the spot exports
#: only ever describe trading, and the order history does not even carry its
#: fees. Whichever lands first establishes the cost, so they are imported
#: richest-first and each later file is reconciled against what is already
#: there rather than added on top.
_CRYPTO_PRIORITY = {
    "binance-transactions": 1,
    "binance-spot-trades": 2,
    "binance-spot-orders": 3,
}


def _ordered_statements(paths: list[Path]) -> list[tuple[Path, object]]:
    """Parse each statement once and sort them into a deterministic order.

    Import order matters when two reports describe the same month, because the
    first to arrive establishes the amounts. Sorting by broker, then period,
    then format priority means rebuilding from scratch always produces the same
    portfolio. The parsed statement travels with the path so the import does
    not have to read the file a second time.
    """
    from app.importer.pdf import parse_pdf

    parsed: list[tuple[tuple, Path, object]] = []
    for path in paths:
        try:
            statement = parse_pdf(path.read_bytes())
        except Exception:  # noqa: BLE001 — sorted last, then reported by the import
            parsed.append((("~", "9999-99-99", 9, path.name), path, None))
            continue
        parsed.append(
            (
                (
                    statement.broker,
                    statement.period_start.isoformat() if statement.period_start else "9999-99-99",
                    _FORMAT_PRIORITY.get(statement.format, 8),
                    path.name,
                ),
                path,
                statement,
            )
        )
    parsed.sort(key=lambda item: item[0])
    return [(path, statement) for _, path, statement in parsed]


def _ordered_csvs(paths: list[Path]) -> list[tuple[Path, str | None]]:
    """Sort tabular exports into the order they should be imported in.

    The B3 export first (priority 0), then the exchange exports richest-first,
    for the reason in ``_CRYPTO_PRIORITY``. Each file's format travels with its
    path so the import does not have to sniff it a second time.
    """
    from app.importer.crypto import sniff_format
    from app.importer.parser import is_xlsx

    ordered: list[tuple[tuple, Path, str | None]] = []
    for path in paths:
        try:
            payload = path.read_bytes()
            # No exchange ships a spreadsheet, and sniffing one means decoding
            # zip bytes as text.
            fmt = None if is_xlsx(payload) else sniff_format(payload)
        except Exception:  # noqa: BLE001 — an unreadable file is reported by the import
            fmt = None
        ordered.append(((_CRYPTO_PRIORITY.get(fmt or "", 0), path.name), path, fmt))
    ordered.sort(key=lambda item: item[0])
    return [(path, fmt) for _, path, fmt in ordered]


#: Files already imported, as ``{path: [size, mtime_ns]}`` in the settings
#: table. Parsing a hundred statement PDFs just to have de-duplication call
#: every row a duplicate used to cost about a minute of every startup; an
#: unchanged file can be skipped before it is ever opened. A changed or new
#: file still goes through the importer, whose dedup keeps re-reads safe.
_SEEN_KEY = "auto_import_seen"


def _file_signature(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def _load_seen(db) -> dict:
    row = db.get(AppSetting, _SEEN_KEY)
    value = row.value if row is not None else None
    seen = value.get("value") if isinstance(value, dict) else None
    return dict(seen) if isinstance(seen, dict) else {}


def auto_import_initial_files() -> None:
    """Import every CSV and statement PDF under ``AUTO_IMPORT_DIR`` on startup.

    Re-running is safe: de-duplication means an already-imported file adds
    nothing. This is what makes ``docker compose up`` a one-command setup with
    the whole history — B3 and offshore — already loaded. Files whose size and
    mtime match the last successful import are skipped without being parsed.
    """
    directory = Path(settings.auto_import_dir)
    if not settings.auto_import_on_startup or not directory.is_dir():
        return

    with session_scope() as db:
        portfolio = get_default_portfolio(db)
        service = ImportService(db, portfolio)
        seen = _load_seen(db)
        skipped = 0

        def fresh(paths: list[Path]) -> list[Path]:
            nonlocal skipped
            kept = []
            for path in paths:
                if seen.get(str(path)) == _file_signature(path):
                    skipped += 1
                else:
                    kept.append(path)
            return kept

        # Recursive: the B3 export sits at the root of the volume while broker
        # and exchange exports are filed per source (``/data/binance``).
        for path, crypto_format in _ordered_csvs(
            fresh(
                sorted(
                    p
                    for pattern in ("*.csv", "*.xlsx")
                    for p in directory.rglob(pattern)
                    # Excel writes a lock file beside an open workbook; it is not an
                    # export and openpyxl cannot read it.
                    if p.is_file() and not p.name.startswith("~$")
                )
            )
        ):
            try:
                payload = path.read_bytes()
                result = (
                    service.import_crypto_csv(payload, path.name)
                    if crypto_format
                    else service.import_csv(payload, path.name)
                )
                logger.info(
                    "auto-import %s: %s new, %s duplicates",
                    path.name,
                    result.rows_imported,
                    result.rows_duplicate,
                )
                seen[str(path)] = _file_signature(path)
            except Exception:  # noqa: BLE001 — startup must never crash on a bad file
                logger.exception("auto-import failed for %s", path.name)

        statements = _ordered_statements(fresh([p for p in directory.rglob("*.pdf") if p.is_file()]))
        for path, statement in statements:
            try:
                result = service.import_pdf(path.read_bytes(), path.name, statement)
                logger.info(
                    "auto-import %s: %s new, %s duplicates",
                    path.name,
                    result.rows_imported,
                    result.rows_duplicate,
                )
                seen[str(path)] = _file_signature(path)
            except Exception:  # noqa: BLE001 — one unreadable statement must not stop the rest
                logger.exception("auto-import failed for %s", path.name)

        if skipped:
            logger.info("auto-import: %s unchanged files skipped without parsing", skipped)
        # Entries for files that were deleted or moved say nothing anymore.
        seen = {key: value for key, value in seen.items() if Path(key).exists()}
        db.merge(AppSetting(key=_SEEN_KEY, value={"value": seen}))


def bootstrap_fx() -> None:
    """Download the PTAX series once, so offshore holdings convert to reais.

    Per pair, not "once for the table": every supported currency the sidebar
    and the importer can need (dollar *and* euro) is fetched on the first run,
    and a pair whose download failed is picked up again on the next start —
    plus by the half-hourly heal job in between.
    """
    try:
        with session_scope() as db:
            from app.market.fx import missing_pairs, sync_fx

            for base, quote in missing_pairs(db):
                logger.info("downloading %s/%s PTAX rates from Banco Central (first run)", base, quote)
                sync_fx(db, base, quote)
    except Exception:  # noqa: BLE001 — startup must not depend on an external API
        logger.exception("could not bootstrap exchange rates")


def bootstrap_indices() -> None:
    """Fetch the CDI/Selic/IPCA history once, so CDBs are priced from day one."""
    try:
        with session_scope() as db:
            from app.market.indices import index_status, sync_all_indices

            if not index_status(db):
                logger.info("downloading index series from Banco Central (first run)")
                sync_all_indices(db)
    except Exception:  # noqa: BLE001 — startup must not depend on an external API
        logger.exception("could not bootstrap index series")


def bootstrap_benchmarks() -> None:
    """Fetch the Ibovespa series once, so the return chart has something to
    compare against on the very first run. CDI arrives with the index series."""
    try:
        with session_scope() as db:
            from app.market.benchmarks import missing, sync_benchmarks

            absent = missing(db)
            if absent:
                logger.info("downloading benchmark series %s (first run)", ", ".join(absent))
                sync_benchmarks(db)
    except Exception:  # noqa: BLE001 — startup must not depend on an external API
        logger.exception("could not bootstrap benchmark series")


def bootstrap_treasury() -> None:
    """Price Tesouro Direto holdings on first run, then leave it to the beat.

    The feed is a single ~14 MB file, so it is downloaded once here and then
    refreshed daily rather than on every request.
    """
    try:
        with session_scope() as db:
            from app.db.models import TreasuryPrice
            from app.market.treasury import sync_treasury_prices, treasury_assets

            if not treasury_assets(db):
                return
            if db.query(TreasuryPrice.id).first() is not None:
                return
            logger.info("downloading Tesouro Direto prices (first run)")
            sync_treasury_prices(db)
    except Exception:  # noqa: BLE001 — startup must not depend on an external API
        logger.exception("could not bootstrap Tesouro Direto prices")


def startup_reconciliation() -> None:
    """The heavy half of startup: downloads, auto-import and ledger repair.

    Everything here used to run inline in the lifespan, which meant the server
    refused connections — and the desktop app sat on its loading screen — until
    a minute of re-parsing and re-scanning finished. None of it is needed to
    *serve*: the UI tolerates data appearing and correcting itself moments
    after it opens. The internal order is unchanged and still matters — see
    each step's comment.
    """
    try:
        # Rates first: the importer stamps each foreign movement with the rate
        # of its trade date, and a movement imported before the series exists
        # would carry no rate at all.
        if settings.bootstrap_market_data:
            bootstrap_fx()
        # Renames first: an import that runs before them creates the new ticker
        # as a second asset, leaving two rows to merge instead of one to rename.
        with session_scope() as db:
            reconcile_ticker_aliases(db)
        auto_import_initial_files()
        # `op_type`/`effect` are derived from the raw movement label, so a
        # classifier improvement must reach rows that were imported earlier.
        with session_scope() as db:
            reclassify_transactions(db)
            # Again after the import, in case a statement introduced an old
            # spelling that has since been renamed.
            reconcile_ticker_aliases(db)
            # `Asset.kind` is inferred on first sight, so it needs the same treatment.
            reclassify_assets(db)
            # A company that renamed itself must still be quotable under its new
            # ticker, without moving the history off the old one.
            reconcile_market_symbols(db)
            # Fixed income is valued by accrual; give new papers a 100 % CDI default.
            ensure_terms_for_fixed_income(db)
            # A few exchange trades are priced in a coin rather than in money
            # (``NEARBTC``); publishing that coin's own closes as a rate is what
            # lets the line below convert them like any other foreign movement.
            sync_crypto_fx(db)
            # Movements imported before their rate was known get it filled in now.
            backfill_transaction_fx(db)
        if settings.bootstrap_market_data:
            bootstrap_indices()
            bootstrap_benchmarks()
            bootstrap_treasury()
        logger.info("startup reconciliation finished")
    except Exception:  # noqa: BLE001 — a failed repair pass must not kill the server
        logger.exception("startup reconciliation failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    with session_scope() as db:
        get_default_portfolio(db)
        # API keys saved through the UI override the env from the first
        # request on, not only after the next save.
        apply_stored_secrets(db)
    if settings.startup_background:
        # Serve first, reconcile behind the first paint. Daemon: an abrupt
        # shutdown mid-pass is the same contract as the scheduler jobs — every
        # step is idempotent and simply runs again next start.
        threading.Thread(
            target=startup_reconciliation, name="startup-reconcile", daemon=True
        ).start()
    else:
        # Tests (and anyone debugging startup) get the old deterministic
        # behavior: everything done before the first request is answered.
        startup_reconciliation()
    yield


app = FastAPI(
    title=f"{settings.app_name} API",
    version="1.0.0",
    description=(
        "Self-hosted investment portfolio manager. Imports B3 'Movimentação' CSV exports, "
        "computes average price, realised/unrealised results and dividends, and serves the "
        "analytics consumed by the dashboard."
    ),
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.exception_handler(CsvFormatError)
async def csv_format_error_handler(request: Request, exc: CsvFormatError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


if settings.desktop_mode:
    # The desktop build has no nginx: FastAPI serves the built SPA itself, so
    # everything is same-origin — no CORS, and the phone URL just works. The
    # dist directory is bundled by PyInstaller (or sits in the repo in dev).
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    from app.desktop.paths import frontend_dist

    _dist = frontend_dist()
    if _dist.is_dir():
        # Vite content-hashes every filename under assets/, so a hashed file
        # never changes meaning — cache it forever. index.html is the one
        # file whose *name* stays constant across releases while its content
        # (which hashed chunks it points at) changes on every build; caching
        # it is what causes "Failed to fetch dynamically imported module"
        # after an update — Electron's Chromium cache serves a stale
        # index.html pointing at chunk files the new build already deleted.
        app.mount(
            "/assets",
            StaticFiles(directory=_dist / "assets"),
            name="spa-assets",
        )

        @app.middleware("http")
        async def _no_cache_assets(request: Request, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/assets/"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return response

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            # Registered after api_router, so real endpoints win; anything
            # still reaching here under /api is a miss, not a page.
            if full_path.startswith("api/") or full_path == "api":
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            candidate = (_dist / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(_dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(
                _dist / "index.html",
                headers={"Cache-Control": "no-store, must-revalidate"},
            )

    else:  # pragma: no cover — a build error, not a runtime state
        logger.error("desktop mode but no frontend build at %s", _dist)
else:

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {"app": settings.app_name, "docs": "/api/docs", "health": "/api/health"}
