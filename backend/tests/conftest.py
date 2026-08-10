"""Test fixtures.

Tests run against SQLite by default so ``pytest`` works with no infrastructure.
Set ``TEST_DATABASE_URL`` (the Docker test target does) to exercise the same
suite on PostgreSQL — a handful of tests that rely on Postgres-only SQL are
skipped automatically on SQLite.
"""
from __future__ import annotations

import functools
import os
import tempfile
from pathlib import Path

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

# Point the application at the test database *before* importing anything that
# builds an engine at import time, so `pytest` needs no running PostgreSQL.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL or (
    f"sqlite:///{Path(tempfile.gettempdir()) / 'gumbinvest-tests.sqlite'}"
)
# Tests own their fixtures: never let application startup import a stray CSV.
os.environ["AUTO_IMPORT_ON_STARTUP"] = "false"
# Nor download PTAX/index/benchmark series: every `with TestClient(app)` runs
# the lifespan, and live BCB/Yahoo calls made the suite slow and flaky.
os.environ["BOOTSTRAP_MARKET_DATA"] = "false"
# Startup work runs inline in tests: a background thread from one TestClient
# racing the next test's table wipes would make the suite flaky.
os.environ["STARTUP_BACKGROUND"] = "false"

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.db.models import Base, Portfolio  # noqa: E402
IS_POSTGRES = TEST_DATABASE_URL.startswith("postgresql")

requires_postgres = pytest.mark.skipif(
    not IS_POSTGRES, reason="requires PostgreSQL (set TEST_DATABASE_URL)"
)

def _find_sample_csv() -> Path:
    """Locate the reference export: repo root when local, /data in Docker."""
    candidates = [
        Path(__file__).resolve().parents[2] / "movimentacao.csv",
        Path("/data/movimentacao.csv"),
    ]
    return next((path for path in candidates if path.exists()), candidates[0])


SAMPLE_CSV_PATH = _find_sample_csv()
requires_sample_csv = pytest.mark.skipif(
    not SAMPLE_CSV_PATH.exists(), reason="reference movimentacao.csv not available"
)


def _find_statement_dir() -> Path:
    """Locate the broker statement archive: repo ``data/`` or ``/data`` in Docker."""
    candidates = [
        Path(__file__).resolve().parents[2] / "data",
        Path("/data"),
    ]
    return next((path for path in candidates if path.is_dir()), candidates[0])


STATEMENT_DIR = _find_statement_dir()
#: Every statement PDF available, sorted so failures are reported in a stable
#: order. Empty when the archive is not present, which skips those tests.
STATEMENT_FILES = sorted(STATEMENT_DIR.rglob("*.pdf")) if STATEMENT_DIR.is_dir() else []
requires_statements = pytest.mark.skipif(
    not STATEMENT_FILES, reason="broker statement PDFs not available"
)


#: Every crypto exchange export available (``data/binance``). Like the
#: statements these are read-only reference data, and their absence skips the
#: tests that need real files rather than failing them.
CRYPTO_FILES = sorted(STATEMENT_DIR.glob("binance/*.csv")) if STATEMENT_DIR.is_dir() else []
requires_crypto_exports = pytest.mark.skipif(
    not CRYPTO_FILES, reason="Binance exports not available"
)


def crypto_file(marker: str) -> Path | None:
    """The available export whose filename contains ``marker``.

    Markers are the distinguishing word in Binance's own filenames: ``Trade``,
    ``Order`` and ``Trans`` (``…Transações…`` / ``…Transaction-History…``).
    """
    return next((path for path in CRYPTO_FILES if marker.lower() in path.name.lower()), None)


def ledger_totals(path: Path) -> dict[str, object]:
    """Per-coin balance the exchange's own ledger adds up to.

    Computed straight from the CSV rather than through the importer, so it is an
    independent reference: if the two agree, the reconstruction is right.
    ``Earn`` is excluded because those rows restate what ``Spot`` already
    reports — see :mod:`app.importer.crypto.binance_ledger`.
    """
    import csv
    import io
    from collections import defaultdict
    from decimal import Decimal

    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    if not rows:
        return {}
    columns = list(rows[0].keys())
    account, coin, change = columns[2], columns[4], columns[5]
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        if row[account] != "Earn":
            totals[row[coin]] += Decimal(row[change])
    return dict(totals)


@functools.lru_cache(maxsize=1)
def parsed_statements() -> list[tuple[Path, object]]:
    """Every statement PDF, parsed once for the whole session.

    Reading a hundred PDFs takes a second each, and a dozen tests want the same
    result, so the parse is cached rather than repeated per test. Tests treat
    the statements as read-only.
    """
    from app.importer.pdf import parse_pdf

    return [(path, parse_pdf(path.read_bytes())) for path in STATEMENT_FILES]


def _ensure_database(url: str) -> None:
    """Create the PostgreSQL test database on first run.

    Keeps the documented command (`pytest` with TEST_DATABASE_URL) working
    without a manual `createdb` step.
    """
    if not url.startswith("postgresql"):
        return
    from sqlalchemy.engine import make_url

    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            exists = connection.exec_driver_sql(
                "SELECT 1 FROM pg_database WHERE datname = %s", (target.database,)
            ).scalar()
            if not exists:
                connection.exec_driver_sql(f'CREATE DATABASE "{target.database}"')
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def engine():
    """Engine for the URL the app was pointed at above."""
    _ensure_database(os.environ["DATABASE_URL"])
    engine = create_engine(os.environ["DATABASE_URL"], future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _clear_replay_cache():
    """Tests reuse portfolio ids across wiped databases; never share replays."""
    from app.portfolio.service import clear_replay_cache

    clear_replay_cache()
    yield
    clear_replay_cache()


@pytest.fixture(autouse=True)
def _no_claude_code():
    """Pretend the local Claude Code is absent unless a test says otherwise.

    Availability of the subscription provider is a property of the machine, so
    without this the same test says "configured" on a developer's laptop and
    "not configured" in CI — and every ``providers_public`` call would spawn a
    subprocess. Tests that exercise the provider stub ``status`` themselves.
    """
    from app.services import claude_code

    claude_code._status_cache = (
        float("inf"),  # never expires during the test
        claude_code.CliStatus(
            installed=False, logged_in=False, reason="Claude Code não encontrado nesta máquina."
        ),
    )
    yield
    claude_code._status_cache = None


@pytest.fixture(autouse=True)
def _no_unheld_headline_fetch():
    """Keep /market/status offline: the unheld-Bitcoin fallback calls the
    provider from the request path, and every test that renders the status
    endpoint would otherwise reach the real network. The tests that exercise
    the fallback re-enable it and mock the provider themselves."""
    from app.market import crypto

    crypto.UNHELD_FETCH_ENABLED = False
    crypto._UNHELD_CACHE.clear()
    yield
    crypto.UNHELD_FETCH_ENABLED = True
    crypto._UNHELD_CACHE.clear()


@pytest.fixture
def db(engine) -> Session:
    """A clean database for each test."""
    for table in reversed(Base.metadata.sorted_tables):
        with engine.begin() as connection:
            connection.execute(table.delete())
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def portfolio(db: Session) -> Portfolio:
    item = Portfolio(name="Teste", base_currency="BRL", is_default=True)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item
