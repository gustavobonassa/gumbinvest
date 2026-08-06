# GumbInvest

**Self-hosted portfolio manager for Brazilian investors.** Import the raw
exports your brokers already give you — the B3 "Movimentação" CSV, Avenue/Nomad
PDF statements, Binance exports — and GumbInvest reconstructs every position
with weighted-average cost (*preço médio*), values it with live quotes, accrues
your fixed income from the official CDI/IPCA series, and serves everything
through a dark, modern dashboard. With an optional AI analyst on top.

Your data never leaves your machine: it runs entirely on your own computer with
Docker, or as a plain Windows desktop app with no infrastructure at all.

⬇️ [**Download for Windows or macOS**](https://github.com/gustavobonassa/gumbinvest/releases/latest)
— one installer, no Docker, no terminal. Prefer running from source? See the
[Quick start](#quick-start).

![Dashboard](docs/screenshots/dashboard.png)

```bash
cp .env.example .env
docker compose up -d
# open http://localhost:3000
```

Drop your exports into `./data/` before the first start — `data/b3/` for the B3
CSV, `data/avenue/` and `data/nomad/` for statement PDFs, `data/binance/` for
exchange exports — and everything is imported automatically. Filenames don't
matter: each file's type is detected from its contents. You can also upload
files later from the **Importar** page.

> All screenshots in this README show a **synthetic demo portfolio** — real
> tickers and live market prices, fictional quantities and dates.

---

## Table of contents

- [Screenshots](#screenshots)
- [Features](#features)
- [Quick start](#quick-start)
  - [Desktop app (Windows, no Docker)](#desktop-app-windows-no-docker)
- [Documentation](#documentation)
- [Architecture](#architecture)
- [Development](#development)
- [Privacy](#privacy)
- [Contributing](#contributing)
- [License](#license)

---

## Screenshots

| | |
|---|---|
| **Proventos** — income by month, calendar, matrix and top payers | **Rentabilidade** — cumulative return vs. CDI and Ibovespa |
| ![Proventos](docs/screenshots/dividends.png) | ![Rentabilidade](docs/screenshots/reports.png) |
| **Ativos** — every position, sortable, by asset class | **Transações** — the full ledger with filters and CSV export |
| ![Ativos](docs/screenshots/assets.png) | ![Transações](docs/screenshots/transactions.png) |

**Analista IA** — an AI chat that sees your portfolio and can search the web,
docked beside the page:

![Analista IA](docs/screenshots/ai-analyst.png)

And the whole interface is responsive — usable from the phone on your couch
(the desktop app even shows a QR code to open it on your phone over the LAN):

| Dashboard | Proventos |
|---|---|
| ![Mobile dashboard](docs/screenshots/mobile-dashboard.png) | ![Mobile proventos](docs/screenshots/mobile-dividends.png) |

---

## Features

**Import**
- Reads the B3 "Movimentação" CSV as-is: pt-BR numbers (`R$ 1.234,56`), `dd/mm/yyyy`
  dates, `-` for "not applicable", UTF-8/BOM/latin-1 encodings, `,` or `;` delimiters.
- Understands 40+ movement types — trades, dividends, JCP, yields, interest,
  amortisations, splits, reverse splits, bonuses, subscriptions, mergers, fractions,
  custody transfers, fixed-income maturities, fees and taxes.
- **Idempotent**: re-uploading the same file adds nothing. Monthly files merge only
  what is new, and genuinely repeated movements (same asset, day, price and quantity)
  are still kept.
- Unknown movements are imported for the audit trail, flagged in the import log,
  and never silently applied to a position.
- Reads the monthly **PDF statements** from Avenue and Nomad — all the layouts
  both brokers have used since 2020 — each one validated against its own
  printed totals, with a **coverage map** of which months you have and which
  positions no longer agree with the broker's own figures.
- Reads the **Binance spot exports** (transaction, trade and order history). A
  trade on an exchange is a swap, so both sides are booked: buying Ether with
  Tether spends the Tether.

**Portfolio**
- Weighted-average cost (*preço médio*), the Brazilian convention.
- **Multi-currency**: US holdings are kept in dollars and shown in dollars on
  their own page, converted to reais wherever a single total is needed — cost at
  each purchase's own PTAX rate, market value at today's.
- Realised and unrealised results, dividend income by type, returned capital,
  cost basis, allocation, day change, cash flow.
- Correct handling of partial sales, multiple purchases, splits/reverse splits,
  bonus shares, subscriptions, mergers, return of capital and broker transfers.
- Data-quality warnings whenever the export itself is inconsistent.

**Dashboard & analysis**
- Portfolio value vs. invested capital over time — with press-and-drag to
  measure any stretch of the series — allocation donut (by asset, class,
  broker or currency), monthly income, contributions and returns, result per
  asset, largest positions, day movers.
- **Rentabilidade**: cumulative return rebased to zero, compared against CDI
  and Ibovespa, by class or for the whole portfolio; monthly consistency,
  best/worst months, performance ranking per asset.
- Asset pages with price history, average-price reference line, fundamentals,
  dividend history per share, announced dividends, and the complete movement
  ledger.
- Transactions with search, filters, sorting, pagination and CSV export.
- Global search (`Ctrl/⌘ + K`) over tickers, company names, movements and dates.

**Proventos**
- A page of its own for income: dividends, JCP, fund yields and fixed-income
  interest, grouped by **month, quarter or year** and stacked by payment type
  or asset class.
- A **dividend calendar**: what landed on each day, plus payments already
  announced on the B3 but not yet credited, estimated against your current
  position.
- Ranked payers, **yield on cost** per asset, and a month × year matrix of
  every payment since the first one, shaded by amount.

**Fixed income & Tesouro Direto**
- CDB/LCI/LCA/RDB positions are **accrued from the index**, not frozen at cost:
  Banco Central's CDI, Selic and IPCA series (free, no key) drive the valuation.
- Per-paper terms: % of CDI/Selic, CDI + spread, prefixed, or IPCA + spread; a
  redeemed paper's cash flows are solved backwards to reveal the rate it
  actually paid.
- Tesouro Direto is marked to market from the Treasury's own daily price file,
  including the yield each purchase was contracted at.
- Cash accounts (conta remunerada) with deposits/withdrawals accrued the same way.

**AI (optional, bring your own key)**
- **Analista IA** — chat about your portfolio or a single asset; the model sees
  your positions and fundamentals and can search the web. Streamed answers,
  conversations saved to their own page.
- **Carteira IA** — virtual wallets managed end-to-end by an AI model with
  R$ 10.000 virtuais per category: it screens candidates, justifies each pick,
  and reviews the wallet over time. Nothing touches your real portfolio.
- Works with **Anthropic (Claude), OpenAI (GPT), Google (Gemini), xAI (Grok)
  and Groq** — pick provider and model per feature in Configurações.

**Screener & tools**
- **Universo de ativos** — screen *every* listed B3 paper, not just what you
  hold, on locally computed fundamentals; see where your holdings sit inside
  the whole market ("como me comparo", by percentile).
- **Comparador** — side-by-side indexed price performance and fundamentals for
  any tickers, held or not; the comparison lives in the URL, so it's shareable.
- **Carteiras públicas** — the 13F portfolios of famous investors, straight
  from SEC EDGAR.
- **Calculadora de juros compostos** — seeded with your actual net worth and
  average monthly contribution, so the first projection is "my portfolio, if I
  keep going".

**Operations**
- Live quotes with pluggable providers; automatic refresh in the background.
- Historical daily closes so the value chart is real market value, not cost.
- Nightly snapshots and automatic backups with rotation, plus a one-file
  `.gumbinvest` export that moves a whole history between installs.
- Watchlist, asset notes, manual prices, audit log.

---

## Quick start

Requirements: Docker and Docker Compose. Nothing else.

```bash
git clone https://github.com/gustavobonassa/gumbinvest.git && cd gumbinvest
cp .env.example .env

# optional: auto-import your history on first boot — one folder per source,
# any filenames (the importer detects each file's type from its contents)
cp /path/to/your-b3-export.csv   ./data/b3/       # B3 "Movimentação" CSV(s)
cp /path/to/avenue-statements/*  ./data/avenue/   # Avenue statement PDFs
cp /path/to/nomad-statements/*   ./data/nomad/    # Nomad statement PDFs
cp /path/to/binance-exports/*    ./data/binance/  # Binance CSV exports

docker compose up -d
docker compose logs -f backend      # watch the import
```

| Service | URL |
|---|---|
| Dashboard | http://localhost:3000 |
| API | http://localhost:8000/api |
| API docs (Swagger) | http://localhost:8000/api/docs |
| Postgres | `localhost:5433` |

First run applies migrations, creates the default portfolio, downloads the
USD/BRL rate history, and imports every CSV *and* statement PDF found under
`AUTO_IMPORT_DIR` (`./data` by default, searched recursively). The per-source
folders are just a convention to keep the archive tidy — files at the root work
too, names are free-form, and re-running never duplicates anything.

Then, from the UI: **Configurações → Baixar histórico de preços** to fetch daily
closes (a few minutes), which turns the evolution chart into true market value.

To stop: `docker compose down`. To wipe everything including the database:
`docker compose down -v`.

### Desktop app (Windows, no Docker)

For users who just want to install and use it, there is a packaged Windows
build: an Electron window (with a dark, app-styled title bar) wrapping the
whole app running locally — SQLite instead of Postgres, an in-process
scheduler instead of Celery, the dashboard served by the API itself. No
Docker, no infrastructure. The window is just a viewer: the same local
server stays reachable from any browser on the machine and from the phone.

**[⬇ Download the latest installer](https://github.com/gustavobonassa/gumbinvest/releases/latest)**
— `GumbInvest Setup <version>.exe` for Windows, `GumbInvest-<version>-arm64.dmg`
for macOS (Apple Silicon). Each release is built automatically on GitHub's
servers from the tagged source
([`.github/workflows/release.yml`](.github/workflows/release.yml)).

The builds are not code-signed, so the OS warns on first run: on Windows,
SmartScreen — "Mais informações" → "Executar assim mesmo"; on macOS,
right-click the app → "Abrir" (or `xattr -dr com.apple.quarantine
/Applications/GumbInvest.app`). The macOS build shares all the code but is
newer and less battle-tested than the Windows one; its data lives in
`~/Library/Application Support/GumbInvest`.

Or build it yourself:

```powershell
# needs Node and Python 3.11+
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# output: desktop-shell\out\GumbInvest Setup <version>.exe
```

What the installed app does:

- Opens its own window with the title bar drawn in the app's dark theme
  (Electron Window Controls Overlay); closing it quits the app. A tray icon
  carries the secondary actions: **Abrir** (bring the window back), **Abrir
  no celular** (QR code with the LAN address — allow GumbInvest on *private
  networks* when the Windows Firewall asks, or the phone can't connect),
  **Abrir pasta de dados**, **Sair**. `http://localhost:8873` also works in
  any browser on the machine, if you prefer.
- Data lives in `%LOCALAPPDATA%\GumbInvest` (database, backups, logs, and an
  `auto-import` folder with the same `b3/`, `avenue/`, `nomad/`, `binance/`
  layout as `./data` in Docker). Uninstalling keeps it; reinstalling finds it
  again.
- The scheduled jobs (quote refresh, FX/index/treasury sync, nightly snapshot
  and backup) run inside the app — same schedule as the Celery beat.
- `DATABASE_URL` still wins if set, so a desktop install can be pointed at an
  existing Postgres.

Desktop and Docker are separate instances with separate data — there is no
live sync between a desktop SQLite and a server Postgres. To move a history
once: **Configurações → Backup e migração → Exportar banco de dados** on the
source, then drag the `.gumbinvest` file onto the **Importar** page of the
target. The file is a complete clone (every table, IDs preserved, gzipped
JSON); importing replaces everything and is only accepted by an instance that
has no transactions yet, so two histories can never be silently merged. Both
installs must be on the same schema version.

During development, run the exact desktop code path without packaging:

```powershell
cd backend
pip install -r requirements-desktop.txt
python -m app.desktop          # the server: SQLite + scheduler + SPA on :8873
# optional, in another terminal — the Electron window around it:
cd ..\desktop-shell && npm install && npm start
```

---

## Documentation

The details live in `docs/`, one guide per subject:

| Guide | What it covers |
|---|---|
| [Importing your data](docs/importing.md) | The B3 CSV pipeline, Avenue/Nomad statement PDFs, Binance exports, de-duplication, known caveats of each source |
| [How the numbers are computed](docs/calculations.md) | The replay engine and its rules (splits, `Atualização`, fractions…), multi-currency, corporate actions, fixed income and Tesouro Direto valuation |
| [AI features, screener and tools](docs/features.md) | Analista IA, Carteira IA, the asset-universe screener, Comparador, public 13F portfolios, the calculator |
| [Reference](docs/reference.md) | Market-data providers, environment variables, API endpoints, tests, backups |
| [Architecture deep-dive](docs/ARCHITECTURE.md) | The *why* behind each design decision — read before changing the importer or engine |

---

## Architecture

```
                    ┌──────────────┐
  browser ────────► │   frontend   │  React + TS + Tailwind + Recharts
                    │    nginx     │  serves the SPA, proxies /api (same origin)
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐        ┌──────────────┐
                    │   backend    │◄──────►│  PostgreSQL  │
                    │   FastAPI    │        └──────────────┘
                    └──────┬───────┘
                           │                ┌──────────────┐
                    ┌──────▼───────┐◄──────►│    Redis     │
                    │ worker+beat  │        └──────────────┘
                    │    Celery    │  quotes · history · snapshots · backups
                    └──────────────┘
```

The desktop build swaps the edges and keeps the core: SQLite for Postgres, an
in-process APScheduler for Celery, the SPA served by FastAPI itself — the same
application code behind a single window (`backend/app/desktop/`,
`desktop-shell/`).

The invariants the whole codebase is built on: money is `Numeric`, never
float; transactions are append-only and positions are always derived by
replaying them; imports are idempotent; the classifier is a table, not logic;
ambiguity is surfaced, never guessed at silently. The reasoning behind each
one is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Development

Run the stack with Docker and point the Vite dev server at it:

```bash
docker compose up -d db redis backend

cd frontend
npm install
npm run dev          # http://localhost:5173, proxies /api to :8000
```

Backend without Docker:

```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate    # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://gumbinvest:gumbinvest@localhost:5433/gumbinvest
alembic upgrade head
uvicorn app.main:app --reload
```

After changing a model:

```bash
docker compose exec backend alembic revision --autogenerate -m "what changed"
docker compose exec backend alembic upgrade head
```

Migrations must stay SQLite-compatible (the desktop build runs on SQLite) —
`tests/test_migrations_sqlite.py` enforces this.

**Releasing:** push a version tag and GitHub Actions builds the Windows
installer and publishes it on the release page automatically:

```bash
git tag v1.0.0
git push origin v1.0.0
```

---

## Tests

```bash
cd backend && python -m pytest -q      # SQLite, no infrastructure needed
cd frontend && npm run typecheck       # tsc is the frontend gate
```

The suite covers the CSV dialect, every movement label, the full calculation
rule set, de-duplication, statement and crypto reconciliation, the universe
ingest and the HTTP layer. Details — including how parsers are validated
against the documents' own printed totals — in
[docs/reference.md](docs/reference.md#tests).

---

## Privacy

- **Everything is local.** Your transactions, statements and balances live in
  your own Postgres/SQLite. There is no telemetry, no account, no cloud.
- **Outbound calls are limited to public market data**: quote providers
  (Yahoo/brapi), Banco Central (PTAX, CDI/Selic/IPCA), Tesouro Transparente,
  B3/CVM public files and SEC EDGAR. None of them receive your portfolio.
- **AI features are opt-in** and only work with your own API key. When you use
  them, the relevant portfolio context is sent to the provider *you* configured
  — and nowhere else. Keys are stored in the local database and stripped from
  every export and backup.
- If you fork or contribute: `data/`, `backups/` and `.env` are gitignored for
  a reason — never commit real financial history, and use synthetic fixtures
  in tests.

---

## Contributing

Issues and pull requests are welcome. Before changing the importer, engine or
de-duplication logic, read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — it
explains the *why* behind each design decision. The short version:

- money is `Decimal`, never float;
- transactions are append-only, positions are always derived;
- imports must stay idempotent;
- the classifier is a table, not logic — new broker wordings are data;
- ambiguity is surfaced, never guessed at silently;
- statement parsers must validate against the documents' own printed totals.

New broker wordings, statement layouts and exchange exports are the most
valuable contributions — and usually the smallest: a classifier row plus a test.

```bash
cd backend && python -m pytest -q     # must stay green
cd frontend && npm run typecheck      # no ESLint configured; tsc is the gate
```

## License

[MIT](LICENSE)
