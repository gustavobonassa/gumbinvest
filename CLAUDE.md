# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

GumbInvest — self-hosted investment portfolio manager for the Brazilian B3 market (plus Avenue/Nomad broker PDF statements, Binance crypto exports, fixed income and Tesouro Direto). FastAPI + SQLAlchemy + PostgreSQL + Celery/Redis backend, React 18 + TypeScript + Vite + Tailwind + Recharts + TanStack Query frontend, all orchestrated with Docker Compose. UI text is in Portuguese (pt-BR).

`README.md` is the thorough user-facing reference (features, env vars, API table). `docs/ARCHITECTURE.md` explains the *why* behind each design decision — read it before changing the importer, engine, or dedup logic.

## Commands

```bash
# Full stack
docker compose up -d                 # dashboard :3000, API :8000/api, Postgres :5433

# Frontend dev against dockerized backend
docker compose up -d db redis backend
cd frontend && npm install && npm run dev    # :5173, proxies /api to :8000
npm run build                        # tsc -b && vite build
npm run typecheck                    # no ESLint/Prettier configured; no frontend tests

# Backend without Docker (Windows venv activation shown; see README for unix)
cd backend
python -m venv .venv && . .venv/Scripts/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://gumbinvest:gumbinvest@localhost:5433/gumbinvest
alembic upgrade head
uvicorn app.main:app --reload

# Desktop mode (no Docker: SQLite + in-process scheduler; Electron shell owns the window/tray)
cd backend && python -m app.desktop        # the desktop server alone (browser at :8873)
cd desktop-shell && npm start              # the Electron window around a running server
powershell -File packaging/build.ps1       # Windows installer (PyInstaller server + electron-builder)

# Tests (from backend/) — run on SQLite, no infrastructure needed
python -m pytest -q
python -m pytest tests/test_engine.py -q                 # one file
python -m pytest tests/test_engine.py::test_name -q      # one test
# Against Postgres, as CI would:
docker compose exec -e TEST_DATABASE_URL=postgresql+psycopg2://gumbinvest:gumbinvest@db:5432/gumbinvest_test backend pytest -q

# Migrations after changing app/db/models.py
docker compose exec backend alembic revision --autogenerate -m "what changed"
docker compose exec backend alembic upgrade head
```

Note: some tests validate parsers against the real statements/exports under `data/` and skip automatically when those files are absent — a fully green run locally may be exercising more tests than a clean checkout would.

## Architecture

Five containers: `frontend` (nginx, serves SPA + proxies `/api` so there's no CORS in prod), `backend` (FastAPI), `worker` + `beat` (Celery: quote refresh, history backfill, nightly snapshots, pg_dump backups), `db`/`redis`.

Backend layers — dependencies point strictly downward:

- `app/api/routes/*` — thin HTTP layer only: parse request, call a service, return.
- `app/portfolio/service.py` — database-facing analytics (allocation, history, reports).
- `app/portfolio/engine.py` — **pure** replay engine: takes a list of movements, returns positions with weighted-average cost (*preço médio*), realised/unrealised results. Imports nothing but `app/domain/enums` and stdlib; all financial rules live here and are covered by fast unit tests.
- `app/importer/*` — CSV/PDF/exchange exports → normalised movements. `parser.py` handles the B3 dialect (pt-BR numbers, `dd/mm/yyyy`, `-` as N/A, multiple encodings/delimiters); `classifier.py` maps movement labels to operations; `service.py` de-duplicates and persists. PDF statement parsers live in `importer/pdf/`, crypto in `importer/crypto/` — both use a registry pattern.
- `app/market/*` — quote/history providers (yahoo / brapi / yfinance / null) behind an interface; provider choice is config (`MARKET_DATA_PROVIDER`), no app code imports a concrete provider.
- `app/db/models.py` — SQLAlchemy models.
- `app/desktop/*` — the desktop server (SQLite, in-process APScheduler mirroring the Celery beat schedule, SPA served by FastAPI). Never imported by `app.main` except behind `DESKTOP_MODE`; Docker never loads it. The window and tray live in `desktop-shell/` (Electron), which spawns this server and finds it via `port.txt`. Migrations must stay SQLite-compatible — `tests/test_migrations_sqlite.py` enforces this.

## Invariants — do not break these

- **Money is `Numeric`/`Decimal`, never float** — in the database, the engine, everywhere.
- **Transactions are append-only; positions are always derived by replaying them.** Never mutate positions directly. A fixed classifier rule retroactively fixes history — that's by design.
- **Imports are idempotent.** De-duplication uses a unique `(portfolio_id, dedup_key)` constraint; re-uploading any file (B3 CSV, statement PDF, Binance export) must add nothing. Genuinely repeated same-day movements are intentionally preserved — understand `importer/dedup.py` before touching keys.
- **The classifier is a table, not logic.** New broker wordings are added as data. Unknown labels are imported for the audit trail, flagged in the import log, and never applied to a position — guessing quietly is worse than being visibly ignorant.
- **Ambiguity is surfaced, not hidden**: import log, asset notes, `/api/portfolio/warnings`.
- **Statement parsers are validated against the documents' own printed totals** — a mis-read column must fail loudly, not silently shift a position.
- Raw source columns (`raw_movement`, `raw_product`, `raw_institution`, `source_line`) are kept so every figure traces back to a line in the original file.

## Data sensitivity

`data/`, `backups/`, `movimentacao.csv` and `.env` contain the owner's real financial history and credentials. Never commit them, never paste their contents into docs/tests/fixtures, and prefer synthetic fixtures for new tests.
