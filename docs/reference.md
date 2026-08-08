# Reference

Market-data providers, environment variables, the API surface, tests and backups.

*Part of the [GumbInvest](../README.md) documentation.*


---

## Market data

Providers implement one small interface
([`app/market/base.py`](../backend/app/market/base.py)) and are selected with
`MARKET_DATA_PROVIDER`:

| Provider | Key needed | Notes |
|---|---|---|
| `yahoo` *(default)* | no | Yahoo Finance chart API; quotes **and** daily history, `.SA` suffix added automatically |
| `brapi` | yes (free) | brapi.dev, richest B3 metadata; set `BRAPI_TOKEN` |
| `yfinance` | no | the `yfinance` package; kept as an alternative |
| `none` | — | disables live pricing; positions are valued at average cost |

Quotes refresh every `PRICE_REFRESH_MINUTES` and are cached for
`QUOTE_CACHE_TTL` seconds. A failed symbol never blocks the rest of the refresh.
Instruments no quote API covers are handled separately: **fixed income accrues
from the CDI/Selic/IPCA series** (see
[calculations](calculations.md#fixed-income-cdb-lci-lca)), Tesouro Direto is priced from the
Treasury's own file, and futures, options and subscription rights are valued at
cost — the dashboard says so explicitly, and a manual price can be set per asset.

**Crypto** needs nothing special: a coin is a dollar-denominated asset with a
`BTC-USD`-shaped market symbol, so the same providers quote it alongside every US
holding.



---

## Environment variables

All of these live in `.env` (copied from `.env.example`); every one has a working default.

| Variable | Default | What it does |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `gumbinvest` | Database credentials |
| `POSTGRES_PORT` | `5433` | Host port for Postgres |
| `BACKEND_PORT` / `FRONTEND_PORT` | `8000` / `3000` | Published ports |
| `APP_NAME` | `GumbInvest` | Shown in the API metadata |
| `BASE_CURRENCY` | `BRL` | Currency for every monetary figure |
| `TIMEZONE` | `America/Sao_Paulo` | Scheduler timezone |
| `LOG_LEVEL` | `INFO` | Backend log level |
| `CORS_ORIGINS` | `localhost:3000,5173` | Only needed when not using the bundled nginx |
| `RUN_MIGRATIONS` | `true` | Apply `alembic upgrade head` on start |
| `MARKET_DATA_PROVIDER` | `yahoo` | `yahoo` / `brapi` / `yfinance` / `none` |
| `BRAPI_TOKEN` | *(empty)* | Token for brapi.dev |
| `BRAPI_BASE_URL` | `https://brapi.dev/api` | brapi endpoint |
| `QUOTE_CACHE_TTL` | `900` | Seconds a quote counts as fresh |
| `PRICE_REFRESH_MINUTES` | `30` | Automatic quote refresh interval |
| `SNAPSHOT_TIME` | `23:10` | Daily snapshot job (HH:MM) |
| `BACKUP_TIME` | `03:30` | Daily `pg_dump`; empty disables |
| `BACKUP_DIR` / `BACKUP_KEEP` | `/backups` / `14` | Backup location and rotation |
| `AUTO_IMPORT_DIR` | `/data` | Scanned recursively for exports on startup (any filename) |
| `AUTO_IMPORT_ON_STARTUP` | `true` | Import those files automatically |
| `GDRIVE_CLIENT_ID` / `GDRIVE_CLIENT_SECRET` | *(empty)* | Cloud backup: your own Google OAuth client (usually entered in the UI instead) |
| `DROPBOX_APP_KEY` | *(empty)* | Cloud backup: your own Dropbox app key (usually entered in the UI instead) |

AI provider keys are **not** environment variables: they are entered in
**Configurações → Inteligência artificial**, stored in the local database and
stripped from exports and backups.



---

## API

Interactive documentation at `/api/docs`. The main endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness |
| `POST` | `/api/imports` | Upload a B3 CSV, broker statement PDF or exchange export (multipart `file`) |
| `GET` | `/api/imports`, `/api/imports/{id}` | Import history (paginated) and full log |
| `GET` | `/api/imports/coverage` | Statements held per account, missing months, position drift |
| `GET` | `/api/portfolio/overview` | Headline metrics |
| `GET` | `/api/portfolio/positions` | Positions with market data |
| `GET` | `/api/portfolio/allocation?group_by=asset\|kind\|broker\|currency` | Allocation |
| `GET` | `/api/portfolio/history?range=max&granularity=auto` | Value over time |
| `GET` | `/api/portfolio/income`, `/contributions`, `/monthly-returns` | Series |
| `GET` | `/api/portfolio/dividends` | Income by period, class, type and asset |
| `GET` | `/api/dividends/calendar`, `/api/dividends/upcoming` | Paid and announced income |
| `GET` | `/api/portfolio/warnings` | Data-quality issues |
| `GET` | `/api/assets`, `/api/assets/{ticker}` | Asset list and detail |
| `PATCH` | `/api/assets/{ticker}` | Notes, manual price, metadata |
| `POST` | `/api/assets/{ticker}/reconcile` | State the balance the venue reports; appends the difference |
| `GET` | `/api/transactions`, `/api/transactions/export` | Ledger with filters; CSV export |
| `GET` | `/api/reports/summary`, `/annual`, `/performers`, `/income` | Reports |
| `GET` | `/api/search?q=` | Global search |
| `GET`/`PUT` | `/api/settings` | Preferences and AI provider configuration |
| `POST` | `/api/market/refresh`, `/api/market/backfill` | Quotes and history |
| `GET`/`PUT` | `/api/fixed-income`, `/api/fixed-income/{ticker}` | Papers, terms, accrued value, implied rates |
| `GET`/`POST` | `/api/treasury`, `/api/treasury/sync` | Tesouro Direto positions and price sync |
| `GET`/`POST`/`DELETE` | `/api/corporate-actions` | Detect, declare and undo successions |
| `POST` | `/api/ai/chat` | AI analyst chat (SSE stream) |
| `GET`/`DELETE` | `/api/ai/chats`, `/api/ai/chats/{id}` | Saved conversations |
| `GET`/`POST` | `/api/ai-wallet/…` | Carteira IA wallets, generation and review jobs |
| `GET`/`POST` | `/api/cloud-backup/…` | Cloud backup: connect Google Drive/Dropbox, send now, list, restore |
| `GET`/`POST` | `/api/universe/…` | Asset-universe ingest, screener, portfolio fit |
| `GET` | `/api/investors/…` | Public 13F portfolios |
| `GET` | `/api/watchlist`, `/api/audit` | Extras |



---

## Tests

```bash
cd backend && python -m pytest -q            # SQLite, no infrastructure needed
```

Or against PostgreSQL, exactly as CI would (the database is created on first run):

```bash
docker compose exec -e TEST_DATABASE_URL=postgresql+psycopg2://gumbinvest:gumbinvest@db:5432/gumbinvest_test backend pytest -q
```

Covering: CSV dialect parsing, every movement label in the reference export, the
full calculation rule set (average price, partial sales, splits, bonuses, share
restructurings, fraction auctions, return of capital, custody transfers,
`Atualização`), de-duplication and monthly merges, statement and crypto
reconciliation, the universe ingest and screener, and the HTTP layer end to end.

The statement tests are the interesting ones, because they check the parsers
against the documents themselves rather than against fixtures: every PDF under
`data/` is parsed and each section's total is compared with the total the
broker printed at the bottom of it, then the archive is imported twice — the
second run must add nothing. Those tests skip automatically when `data/` is
absent, so the suite still runs on a clean checkout.



---

## Backups

A `pg_dump` runs daily at `BACKUP_TIME` into `./backups`, keeping `BACKUP_KEEP`
files. A machine that was off at that hour catches up: an hourly check (and, on
the desktop build, a check shortly after the app opens) runs the backup — and
the cloud sync — whenever the newest dump is more than a day old. The cloud
side keeps `BACKUP_KEEP` files per provider too, so neither disk nor cloud
grows without bound.

```bash
# manual backup
docker compose exec worker python -c \
  "from app.workers.tasks import backup_database_task; print(backup_database_task())"

# restore
gunzip -c backups/gumbinvest-YYYYMMDD-HHMMSS.sql.gz | \
  docker compose exec -T db psql -U gumbinvest -d gumbinvest
```

For moving a whole history between installs (Docker ↔ desktop), use the
`.gumbinvest` export instead — **Configurações → Backup e migração**.

### Cloud backup

**Configurações → Backup** can also mirror the `.gumbinvest` export to your own
Google Drive and/or Dropbox, nightly (with the local backup) and on demand. On
another computer, connect the same account and restore from the listed backups
— the restore uses the regular full import, so it only proceeds into an empty
installation. On a non-empty one, the dialog offers a deliberate reset: type
the confirmation phrase and the current data is dumped to the local backup
directory, then wiped and replaced by the cloud backup (a clone, never a
merge). An optional passphrase encrypts the file (AES-256-GCM) before it
leaves the machine; without the passphrase the backup cannot be restored.

Setup is one-time, in the UI:
- **Google Drive** — create a free Google Cloud project, enable the Drive API,
  create an OAuth client of type *TVs and Limited Input devices*, and publish
  the consent screen (*In production* — in *Testing* mode Google expires the
  connection every 7 days). Paste the client ID and secret, then authorize at
  google.com/device with the code shown. Scope is `drive.file`: the app only
  ever sees the files it created, in a `GumbInvest` folder.
- **Dropbox** — create a free app at dropbox.com/developers with *App folder*
  access, enable `files.metadata.read`, `files.content.read` and
  `files.content.write` on the app's **Permissions** tab, paste its app key,
  and authorize with the code Dropbox displays. Backups live under
  `Apps/<your app>/`. (Permissions are baked into the authorization — if you
  change them later, disconnect and reconnect.)

Tokens and the passphrase are stored like AI keys: local database only, never
echoed back, stripped from every export.

