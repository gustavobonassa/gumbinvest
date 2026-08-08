# Architecture

How GumbInvest is put together and, more importantly, *why*. The README covers
usage; this document covers the reasoning a future maintainer needs.

## Contents

1. [Shape of the system](#1-shape-of-the-system)
2. [Data model](#2-data-model)
3. [The import pipeline](#3-the-import-pipeline)
3b. [Broker statements (PDF)](#3b-broker-statements-pdf)
3c. [Crypto exchanges (CSV)](#3c-crypto-exchanges-csv)
4. [De-duplication](#4-de-duplication)
4b. [Knowing what is missing](#4b-knowing-what-is-missing)
5. [The calculation engine](#5-the-calculation-engine)
6. [Ambiguous B3 events](#6-ambiguous-b3-events)
7. [Analytics layer](#7-analytics-layer)
8. [Market data](#8-market-data)
8d. [Currencies](#8d-currencies)
9. [Background jobs](#9-background-jobs)
10. [Frontend](#10-frontend)
11. [Performance](#11-performance)
12. [Extending the system](#12-extending-the-system)

---

## 1. Shape of the system

Five containers, one job each:

| Container | Role |
|---|---|
| `frontend` | nginx serving the built SPA and proxying `/api` to the backend (same origin, so no CORS in production) |
| `backend` | FastAPI: HTTP, import, analytics |
| `worker` | Celery worker: quotes, history backfill, snapshots, backups |
| `beat` | Celery beat: the schedule |
| `db` / `redis` | PostgreSQL for state, Redis as broker/result backend |

The backend is layered so each piece can be tested without the ones above it:

```
HTTP            app/api/routes/*        thin: parse request, call a service, return
analytics       app/portfolio/service   database-facing queries and aggregations
domain          app/portfolio/engine    pure functions: positions, average price, results
ingestion       app/importer/*          CSV / PDF / exchange export -> normalised movements
integration     app/market/*            quotes and price history behind an interface
persistence     app/db/models           SQLAlchemy, Numeric everywhere
```

Dependencies point downward only. The engine imports nothing but `enums` and the
standard library, which is what makes the financial rules cheap to test.

---

## 2. Data model

| Table | Purpose |
|---|---|
| `portfolios` | Supports multiple portfolios; one default is seeded |
| `assets` | One row per instrument, keyed by ticker (synthetic for Tesouro/CDB) |
| `brokers` | Canonical broker names plus every raw spelling seen |
| `transactions` | Normalised movements — the source of truth |
| `import_batches` | One row per upload: counts, errors, operation breakdown |
| `quotes` | Latest price per asset |
| `price_history` | Daily closes for historical valuation |
| `index_rates` | CDI/Selic/IPCA series from Banco Central |
| `fixed_income_terms` | Contracted yield per private paper (the export omits it) |
| `treasury_prices` | Daily buy/sell price and yield per Tesouro Direto title |
| `asset_successions` | Which asset replaced which (the export never links them) |
| `portfolio_snapshots` | Materialised daily state |
| `app_settings`, `watchlist`, `goals`, `audit_logs` | Preferences and extras |

Decisions:

- **`Numeric`, never `Float`.** `Numeric(20,6)` for money, `Numeric(24,8)` for
  quantities — B3 hands out fractional shares from splits and fraction auctions.
- **Transactions are append-only in practice.** Positions are always derived by
  replaying them, so a fixed classifier rule improves history retroactively.
- **Raw columns are kept** (`raw_movement`, `raw_product`, `raw_institution`,
  `source_line`) so any figure can be traced back to a line in the original file.
- **Indexes** cover the real query patterns: `(portfolio_id, trade_date)`,
  `(asset_id, trade_date)`, `op_type`, `dedup_key`, and a unique
  `(portfolio_id, dedup_key)` that makes duplicate insertion impossible even
  under a race.

---

## 3. The import pipeline

```
bytes → decode → parse rows → classify → de-duplicate → persist → import log
```

**Decode.** Tries `utf-8-sig`, `utf-8`, then `latin-1`; older B3 exports are
latin-1. The delimiter is sniffed (`,` or `;`).

**Parse** (`importer/parser.py`). Header names are matched accent- and
case-insensitively, so `Movimentação`/`MOVIMENTACAO` both work. Numbers handle
`R$`, thousands dots, decimal commas, parenthesised negatives and `-` for N/A.
Products are split into a stable ticker plus a readable name; the instrument
family is inferred from the ticker suffix and the description (suffix `11` is
shared by FIIs, ETFs and Units, so the name disambiguates — index markers win,
because an index fund is still called a "FUNDO").

**Normalise brokers.** The reference export writes four brokers under nine
different strings. Normalising to a canonical name keeps filters usable *and*
makes de-duplication robust when B3 changes a spelling between exports.

**Classify** (`importer/classifier.py`). A table maps
`(normalised label, direction)` to `(OperationType, PositionEffect)`. Unknown
labels fall through to keyword heuristics, and finally to `UNKNOWN`/`NONE` —
imported for the audit trail, reported in the log, never applied to a position.
Guessing quietly would be worse than being visibly ignorant.

One rule deserves a note: a disposal with no amount attached (a maturity row with
`-` as the value) is downgraded from `DISPOSE` to `QTY_OUT_FREE`, because a sale
with no proceeds cannot realise a gain.

---

## 3b. Broker statements (PDF)

The B3 export is a CSV. Every other broker in this portfolio ships PDFs, and
each has changed layout at least once:

| Parser | Files | Period | Notes |
|---|---|---|---|
| `drivewealth` | Avenue `stmt<Month>…`, Nomad `old/` | Avenue 2020-11→2021-06, Nomad 2025-04→2025-11 | One white-labelled statement, two brokers |
| `apex-en` | Avenue `Doc_*` | 2021-05→2026-06 | The only source for 2021-07→2024-12 |
| `avenue-pt` | Avenue `Stmt_*` | 2025-01→2026-06 | Avenue-branded, Portuguese |
| `apex-ascend` | Nomad `new/` | 2025-11→2026-06 | Portuguese until 2026-01, English after |

`importer/pdf/registry.py` sniffs the format from the document text; the two
Apex families are tested first, because an Avenue statement mentions Apex in its
footer disclosures and would otherwise be claimed by the wrong parser.

Each parser produces `StatementRow` objects shaped **exactly like a B3 CSV row**
— a movement label, a direction and a positive amount — so classification,
de-duplication and persistence stay in one place. The broker's own wording is
translated into the canonical labels in `importer/pdf/movements.py`, which the
classifier maps alongside the Portuguese B3 ones.

### Why positions, not just text

Text extraction alone is not enough for these files:

* **The sign lives in the x-coordinate.** Apex and Avenue put amounts in a
  `DEBIT` and a `CREDIT` column whose contents are otherwise identical —
  "Retenção Impostos sobre Dividendos" is a debit, "Estorno Retenção Impostos
  sobre Dividendos" a credit. `importer/pdf/layout.py` keeps every word's
  position and anchors columns on the header row, matching values on their right
  edge because the numbers are right-aligned.
* **Column positions move within one document.** The main ledger and the
  pending-settlement table use different x-positions, so the columns are
  re-read from every header row rather than assumed once.
* **Apex prints its letterhead vertically down the left margin**, one letter per
  line, which lands as a stray leading word on whichever rows line up with it.
  Anything left of the first column is discarded as page furniture.
* **Rows never span a page.** The section heading and column header are
  reprinted, so a block is closed at a page boundary — otherwise the last row of
  a page swallows the next page's letterhead, and the Miami ZIP code sits
  exactly under the credit column.

### Numbers and dates

`importer/pdf/values.py` parses numbers **structurally**, not by locale, because
the locale is not reliable: Apex Ascend's portfolio table prints `1.274,58`
while its activity table two pages later prints `1.315.39`. The separator layout
decides — a trailing group that is not three digits is always a decimal mark —
and a locale hint is consulted only for `1.234`, the one genuinely ambiguous
shape.

Dates are worse, because both readings are valid: the same purchase is
`01/05/26` in the Apex statement and `05/01/2026` in the Avenue one. Each parser
states its order explicitly rather than guessing.

### What is deliberately not imported

* **The pending-settlement table.** Apex lists a month-end trade as pending and
  the next statement lists it again as settled, under a different date. Reading
  both would double-count every trade that straddles a month end, and no
  de-duplication could catch it.
* **Cash movements** — deposits, withdrawals, journals and money-market sweeps.
  They are parsed (so section totals reconcile) and then dropped: they belong to
  no asset, and the portfolio model tracks positions rather than the broker's
  cash account. The counts appear in the import log.
* **Cash rows with no security named.** Avenue's April 2025 statement lists 27
  withholding reversals with the ticker column simply blank. They are reported
  with their total rather than attached to an invented asset. A row that carries
  *quantity* is never dropped — it is imported under a visibly provisional
  ticker, because losing it would silently corrupt the position held.

### Identifying the security

Apex's English statements name no ticker at all — only a description and a
`CUSIP:` line. `importer/pdf/symbols.py` resolves, in order of trust: the row's
own symbol, the CUSIP (via a seed table plus anything learned from statements
that print both, stored on `Asset.cusip`), then the description. Failing all
three it returns `None` and the import service decides what to do.

One alias matters: Avenue calls Medical Properties Trust `MPT` while Apex, Nomad
and the market call it `MPW`; Bank of New York Mellon is `BNY` in Avenue's newer
reports and `BK` everywhere else. Without the alias table each holding would
split into two assets with half the history each.

### Self-checking

Every section of every statement prints a control total, and every statement
prints its own holdings. Both are parsed and compared with what was read:

* a section whose parsed sum disagrees with the printed total becomes an import
  warning — that is how a number read in the wrong locale, a row lost to a page
  break, or an amount put in the wrong column stops being silent;
* the holdings feed the position check in [§4b](#4b-knowing-what-is-missing).

The test suite runs both across the whole archive, so a parser regression fails
loudly rather than producing a quietly wrong portfolio.

---

## 3c. Crypto exchanges (CSV)

`importer/crypto/` reads exchange exports. The parsing is the easy half — a CSV
with the unit glued to each number — and the modelling is the interesting one.

### A trade has two sides

Every other source in this app describes a market where cash is the counterparty
and cash is not tracked. An exchange does not have that shape: `ETHUSDT` BUY
spends Tether to get Ether, `USDTBRL` BUY spends reais to get Tether, `NEARBTC`
BUY spends Bitcoin. So `CryptoTrade` is deliberately symmetric — it names both
sides — and `ImportService._crypto_legs` decides which of them becomes a
movement:

| Leg | When | Effect |
|---|---|---|
| instrument | always | `ACQUIRE` / `DISPOSE` on the base coin |
| funding | quote side is not fiat | the mirror image on the quote coin |
| fee | fee charged in a *third* coin | `FEE` + `QTY_OUT_FREE` |

A ledger export also carries movements that are not trades at all — rewards,
deposits, withdrawals, staking transfers, futures settlements. Those arrive as
`CryptoEvent`s and become a single leg each, labelled with a canonical movement
the classifier already knows, so nothing downstream needs a special case.

The funding leg is the one that matters. Without it a stablecoin balance would
only ever grow, and — worse — every swap would read as fresh capital entering the
portfolio, because `contributions` is built from acquisitions net of disposals.
With it, converting 100 USDT into Ether nets to zero, which is what it is.

A fee charged in the coin *bought* is not a leg at all: those units never
arrived, so the acquired quantity is simply smaller. A fee charged in the *quote*
currency is money and lands on the instrument leg's `fees`, which the engine
already folds into `net_cost`/`net_proceeds`.

### Coins are dollar assets

`Asset.currency` is `USD` and `market_symbol` is `BTC-USD`. Dollars rather than
reais because `BTC-BRL` covers a handful of coins and `BTC-USD` covers all of
them — an alt coin priced in reais would silently fall back to cost. The
consequence is that a coin travels through exactly the same multi-currency path
as a US share ([§8d](#8d-currencies)) with no second conversion mechanism.

To hold that invariant, amounts are normalised into dollars at import:
dollar-pegged tokens are dollars one-for-one, and a reais-quoted trade is divided
by that day's PTAX with the rate stored on the movement — so `base_movements()`
multiplies it straight back to the reais actually paid. A trade priced in a coin
keeps that coin as its currency and `market/crypto.py` publishes the coin's own
daily close into `fx_rates`, which is what it is: how many reais one BTC was
worth that day. `backfill_transaction_fx` then picks it up with no special case
anywhere else.

### Tickers can collide

`Asset.ticker` is globally unique and three-letter symbols are shared freely
between the two worlds — SOL, LINK and UNI are all listed equities somewhere. A
coin whose symbol is already taken by a security gets a `.CRYPTO` suffix, because
the alternative is two histories silently merging into one position.

### Three exports of the same thing

Binance publishes the same activity at three resolutions: the full ledger, one
row per fill, and one row per order. They cannot be matched row for row — an
order splits into several fills, and the ledger reports each side of a trade
separately — but they always agree on the money, so `_crypto_coverage`
reconciles the day's **value** per coin and direction, and only ever against
rows that came from a different `source_format`. Inside one file a repeat is a
real repeat and is kept, which is the same rule the statement importer follows
([§4](#4-de-duplication)). The auto-import orders them richest-first, so the
ledger establishes every cost and the spot exports add nothing.

### The ledger, and why it wins

`binance_ledger.py` reads the **transaction history**: one row per balance
change, across every account. It is the format worth having, because a spot
export is trades and nothing else — deposits, withdrawals, Convert, Earn,
staking and card purchases are absent, so coins acquired that way are disposed
of from a position the history never opened. On the reference account that
leaves six coins negative and five holdings missing; the same account read from
the ledger balances to the last decimal, which is what
`test_the_real_ledger_reproduces_the_exchange_balances` asserts against totals
computed from the CSV independently of the importer.

Three things make it non-trivial:

**The `Earn` account is a mirror.** A Simple Earn reward is booked once in
`Earn` and again in `Spot` minutes later, with no offsetting debit on either
side. Counting both doubles every reward, so `Earn` rows are dropped.

**A trade is spread across rows.** What was bought, what was spent and what was
charged arrive as separate lines sharing a timestamp. Rows are grouped by
(account, second) and folded into one `CryptoTrade` when the group resolves to
exactly one coin in and one coin out; adjacent groups a second or two apart are
merged when that is what resolves them (Convert sometimes straddles a second).
Aggregating a multi-fill group is exact rather than approximate — five `Sold`
rows and five `Revenue` rows are one trade seen piecewise.

Which side is the instrument is decided by distance from cash (fiat 0,
stablecoin 1, coin 2), not by which side arrived. Reading "the arriving side is
what was bought" makes *selling Tether for reais* a purchase of reais — and
since cash is not a holding, the whole row is dropped and the Tether is never
spent, so it accumulates forever.

**Coins that leave the reported balance have not necessarily left.** Two cases
look identical in the ledger and are not:

*Staking / Simple Earn* (`QTY_OUT_STAKED` / `QTY_IN_STAKED`). The coins are
still on the exchange, still owned, still earning. They leave `quantity` during
the replay so the free balance reconciles, and `_restore_staked` folds them back
at the end, recording `staked_quantity` so the UI can say how much is locked.
Leaving them out closes positions on paper: every USDT on the reference account
had been subscribed and never redeemed, so a four-figure dollar balance
reported as no position at all.

*Withdrawals* (`QTY_OUT_PARKED` / `QTY_IN_PARKED`). These genuinely leave — once
coins are off the exchange nothing here knows what became of them — but the
withdrawal is not a *disposal*, so the cost waits in `Position.parked_cost`
instead of being written off. An exchange is routinely used as a bridge (buy,
withdraw minutes later, deposit months on, sell), and writing the cost off on
the way out makes the eventual sale invent a gain the size of the purchase. The
shape is the same as the `pending_fraction_cost` that already holds a fraction's
cost until B3 auctions it.

Conflating the two over-counts: folding withdrawals back in put 309 USDT that
had been sent to a wallet, and 36 DOGE, back into a portfolio they had left.

**And what genuinely has no cost is not a result.** Whatever comes back beyond
what left was acquired somewhere the export cannot see. It accumulates in
`Position.uncosted_quantity`, and when it is sold the money goes to
`uncosted_proceeds` rather than `realized_pnl` — because a result is revenue
minus cost, and with no cost the subtraction is undefined. A disposal draws on
costed and uncosted units in proportion, so a half-and-half position reports
half a result and half bare proceeds.

Booking those proceeds as a gain is not a harmless approximation. Divided by a
cost basis of nothing it produced six-figure percentage returns on the
reference account, and on one coin turned a modest actual trading result into
many times as much "profit" — the entire difference being coins the user had
transferred in from another wallet.

Two things made that number possible, and both are fixed: the numerator counted
proceeds it should not have, and the denominator was whatever cost happened to
remain. `pct()` now refuses a denominator below a cent — a closed position
leaves residues like `2E-16`, which is truthy and catastrophic — and
`total_return_pct` divides by `total_bought_amount`, since the numerator spans
the position's whole life and the residual cost does not.

---

## 4. De-duplication

The requirement is contradictory on its face: never import the same movement
twice, but never lose a movement that legitimately repeats. Both happen in real
exports — the reference file contains one pair of byte-identical rows that is not
an error.

The key is a content hash plus an **occurrence counter**:

```
fingerprint = sha256(date | direction | movement | ticker | broker | qty | price | amount)
dedup_key   = f"{fingerprint}:{occurrence}"
```

`occurrence` is the row's index **among identical rows within the file being
imported** — not "the next free slot in the database". That distinction is the
whole trick:

| Scenario | Result |
|---|---|
| Same file re-uploaded | identical keys → every row is a duplicate → nothing inserted |
| Monthly file overlapping the last one | overlapping rows hit existing keys; only the new tail is inserted |
| Two identical payments on one day | occurrences `0` and `1` → both kept |
| Same movement, broker renamed by B3 | broker is normalised before hashing → still recognised |

Existing keys are loaded once per import into a set, so the check is O(1) per row
and the whole import is a single bulk insert.

### The same event, reported twice

Broker statements break that scheme, because the offshore brokers issue more
than one report per month and the reports **do not agree with each other**:

| | Apex statement | Avenue statement |
|---|---|---|
| The same dividend | dated on the payment day | dated one day later |
| The same purchase | US$ 2.50 higher | (same day, same quantity) |

The purchase differs by exactly US$ 2.50 — Apex includes the commission and Avenue
does not. An exact key sees four movements where there were two, and picking one
report and discarding the other is not an option either: Apex is the only source
before 2025 and reports trade costs in full, while Avenue's own report carries
dividends the Apex file omits entirely.

So rows arriving from a **different source format for the same broker** are also
matched on a fuzzy key (`importer/dedup.py`): same broker, same operation, same
asset, same quantity, within three days. Trades additionally tolerate a differing
amount, because for a trade that difference is the commission and nothing else;
income and tax rows do not, because a statement can legitimately repeat an
identical amount many times — one Avenue report lists the same small
withholding reversal nine times — and only the amount tells two of them apart.

Three properties make this safe:

* **Never within one format.** Two files of the same format only ever meet the
  exact key, so a genuine repeat stays a repeat.
* **Count-aware.** Nine identical rows matched against three already stored
  leave six to import.
* **Operation-sensitive.** The two legs of a custody transfer — shares leaving
  one broker, arriving at another a day later — are `TRANSFER_OUT` and
  `TRANSFER_IN`, so they never collapse into one. They have to both exist for
  the engine to cancel them out.

Every fuzzy match is listed in the import log, so nothing is treated as a
duplicate invisibly.

Import order therefore matters, and is fixed: the richer report for a month is
imported first (Apex before Avenue), so the second only contributes what the
first was missing. `main.py` sorts by broker, period and format priority, which
makes a rebuild from scratch reproducible.

---

## 4b. Knowing what is missing

A month never downloaded looks exactly like a quiet month: no error, no gap in
the numbers, just a position that is quietly wrong from then on. Three
independent checks, weakest to strongest (`importer/coverage.py`):

1. **Calendar gaps** — a month with no statement between the first and last held
   for that account.
2. **Balance breaks** — a statement whose opening balance does not match the
   previous one's closing balance. This catches a missing month even when the
   calendar looks complete, because two consecutive files then fail to join up.
3. **Position drift** — the quantity the engine computes for an asset against
   the quantity the most recent statement says is held. This is the check that
   catches everything else: a movement in a section the parser skipped, a
   corporate action nobody recorded, a statement that imported only partly.

Coverage is grouped by broker **and account number**, because one broker can
issue two unrelated series — Avenue's history runs through an Apex account and,
from 2025, a second series under Avenue's own numbering describing the same
holdings. Balance continuity only means anything inside one series.

Position drift is the exception: it is a property of the whole broker, so it uses
exactly one statement per broker (the most recent, and among equally recent ones
the one listing most positions). Adding up every current statement would count
the same holdings twice.

---

## 5. The calculation engine

`portfolio/engine.py` is deliberately pure: in goes a list of `Movement`, out
comes a `dict[asset_id, Position]`. No session, no clock, no network.

Cost basis follows the Brazilian **preço médio** convention:

| Event | Quantity | Cost basis | Realised |
|---|---|---|---|
| Purchase | `+q` | `+ (amount + fees + taxes)` | — |
| Sale | `−q` | `− average × q` | `proceeds − average × q` |
| Split / bonus / receipt | `+q` | unchanged | — |
| Fraction removed / receipt converted | `−q` | `− proportional` | — |
| Dividend / JCP / yield / interest | — | — | income |
| Amortisation | — | `− amount` (floored at 0) | excess only |

Why the free-quantity rule matters: B3 reports a split as a credit of the *extra*
shares. Adding quantity while holding cost constant makes the average price
dilute by exactly the split ratio — no ratio parsing, no special case, and
reverse splits fall out of the same rule in reverse.

**Ordering.** Movements are sorted by `(date, effect rank, id)`. The export has no
intraday sequence, so credits are applied before debits; otherwise a same-day
buy-and-sell would hit an empty position and book a bogus realised gain.

**Broker transfers.** Moving shares between your own brokers appears as a credit
at one and a debit at the other. Applied literally, cost basis would be destroyed
(out at cost, back in for free). Rows that pair on `(date, asset, quantity)` are
rewritten to `LEDGER_ONLY` — visible in the audit trail, no financial effect.
Unpaired rows keep their original effect, since those are genuine custody moves.

**Degradation, not silence.** Selling more than the recorded position caps the
disposal and records a warning; selling with no recorded position books the whole
proceeds as realised and records a warning. Both surface at
`/api/portfolio/warnings` and in the UI.

---

## 6. Ambiguous B3 events

Two layers handle ambiguity, and the split matters operationally:

* **Per-row decisions** (the classifier) are *persisted* in `op_type`/`effect`.
  Improving a rule therefore requires re-deriving those columns — which the app
  does on every start, because de-duplication means re-importing the file is a
  no-op. See `reclassify_transactions`.
* **Cross-row decisions** (the engine: transfer pairing, `Atualização`,
  restructurings) are computed at replay time and so apply retroactively.
* **Decisions the export cannot support at all** (which asset succeeded which)
  are *user data*, stored in `asset_successions` and read by the replay. See
  § 6b.

### Splits vs. share restructurings

`Desdobro` alone credits the extra shares — a delta. Verified: a 53-share
position plus a 477 credit becomes the 565 B3 prints on the next distribution,
and a two-broker split lands 2 + 8 = 10 exactly.

But when `Grupamento` **and** `Desdobro` appear for the same asset on the same
day, B3 is describing a share restructuring: the credits state the **resulting**
position, and the old shares are consumed with no debit row. Applying them as
deltas leaves phantom shares that later price at the post-restructuring quote —
in the reference data that inflated one holding to 25× its real size, and left a
fully sold position showing thousands of shares.

`resolve_share_restructures` rewrites the day's first credit to
:attr:`PositionEffect.QTY_RESTATE` carrying the day's total and neutralises the
rest, so the position is replaced exactly once. Cost basis is preserved, so the
average price rescales by the restructuring ratio.

Both readings were validated by replaying the whole export and comparing every
computed position against the share counts B3 prints on its own income rows:
every asset with a stated position reconciles.

### Fraction auctions

B3 removes a fraction (`Fração em Ativos`, debit) and auctions it weeks later
(`Leilão de Fração`, credit). Treating the auction as a disposal removes the same
fraction twice and slowly erodes the position. The auction is therefore
`PositionEffect.REALIZE`: no quantity moves, and the proceeds are booked against
the cost that left with the fraction (parked in `pending_fraction_cost`).

### `Atualização`

The export uses this one label for two incompatible meanings:

- a genuine **credit** of shares (fund events, ticker conversions), and
- a **restatement** of a position that already exists, which B3 emits when custody
  migrates between brokers.

Applying every row as a credit inflates positions (one ETF is restated five times
— the position would end up 5× too large). Ignoring every row loses real shares
(a closed FII position would be understated by more than a third).

**The rule.** Sum the `Atualização` quantities for an asset on a given day — B3
reports them per broker, so a position split across two custodians produces two
rows that must be judged together — and compare with the quantity currently held:

- equal → restatement → skip;
- different → credit → apply as free quantity.

Comparing against the *portfolio-wide* quantity rather than a per-broker ledger is
essential: when a broker is absorbed by another (which happened twice in the
reference data) the shares silently change custody, so a per-broker view reads
zero and would mistake a restatement for a credit.

This resolves all 32 occurrences in the reference export correctly, each checked
against the position implied by the surrounding dividend rows, which carry the
share count B3 itself used. Every decision is recorded in the position's `notes`.

### Subscriptions

Rights and receipts (`…1`, `…2`, `…9`…`…16`) flow through several rows: right
received → subscription requested → right exercised (cash out) → receipt received
→ receipt converted. Exercising is booked as an acquisition so the money paid stays
in invested capital, attributed to the subscription line rather than to the
underlying share — the export does not link the two, and inventing the link would
be a guess. Rights are classified as `AssetKind.SUBSCRIPTION` so they are easy to
recognise in the UI, and that kind is excluded from the "missing quote" warning:
a right converts into the underlying or expires, and B3 leaves the converted line
open at zero cost, so quoting it would be meaningless even where a ticker exists.

---

## 6b. Corporate actions the export does not record

Rename, merger, restructuring: B3 credits the successor and **never debits the
predecessor**, and no row links them. The engine cannot infer the link from the
data, so it is user data:

```
app/db/models.py                  asset_successions
app/portfolio/corporate_actions.py detection + engine adapter
app/portfolio/engine.py            Succession, drop_voided_assets, apply_succession
```

Design notes:

- **Only cost moves.** The successor's quantity was already credited by B3's own
  corporate-action row; replaying that row *and* transferring quantity would
  double it. `apply_succession` zeroes the predecessor and adds its basis to the
  successor — nothing else.
- **Applied at the end of the effective day**, so the basis carried over is the
  balance left after that day's own movements.
- **Cash is returned capital, not gain.** A cash-plus-shares merger reduces the
  basis carried over; only cash beyond the remaining basis is realised.
- **A null successor voids the asset.** Intermediate holding vehicles (handed out
  mid-merger, redeemed for cash days later) have every movement rewritten to
  `NONE`; otherwise their zero-cost redemption books an invented realised gain.
- **Detection proposes, it never applies.** `suggest_successions` ranks
  candidates by whether the target is still held, whether the credited quantity
  matches exactly, and whether it landed the same day. The strongest signal is
  *still held*: a mid-merger vehicle also produces an exact quantity match, so
  matching quantity alone would pick the pass-through over the real successor.

---

## 7. Analytics layer

`portfolio/service.py` joins the engine's output with assets and quotes.

- **Valuation.** Live price when available, otherwise average cost — so a missing
  quote shows zero unrealised result instead of a fabricated loss. Which assets are
  unpriced is reported in the overview.
- **History.** `build_timeline` replays movements into cumulative daily state;
  each requested day is valued with the last known close per asset (binary search
  over the stored series), falling back to cost basis where there is no history.
  So the chart exists even with market data disabled, and improves once backfilled.
- **Granularity** adapts to the range: daily up to ~4 months, weekly up to ~2
  years, monthly beyond — a 6-year chart never ships thousands of points.
- **Monthly returns** are cash-flow adjusted: `(value − previous − flow + income)`
  over `(previous + flow)`, so a large contribution does not read as performance.
- **Allocation by broker** is estimated by weighting each position by that broker's
  share of the asset's transactions; B3 does not give a per-broker position
  directly, and the estimate is labelled as such in the UI.

---

## 8. Market data

```python
class MarketDataProvider(ABC):
    def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]: ...
    def get_history(self, symbol: str, start: date | None) -> list[HistoricalPoint]: ...
    def supports_history(self) -> bool: ...
```

Selected by `MARKET_DATA_PROVIDER`; nothing else in the codebase imports a concrete
provider. Implementations must skip symbols they cannot resolve rather than raise,
so one delisted ticker never blocks a portfolio-wide refresh.

Only held, quotable assets are requested — fixed income, Tesouro Direto, futures,
options and subscription rights are excluded because no *equity* quote API covers
them. Fixed income and Tesouro Direto have their own pricing paths (§ 8b, § 8c);
futures, options and subscription rights are valued at cost with a manual-price
override available.

Each refresh also writes that day's close into `price_history`, so the historical
chart keeps improving even without a full backfill.

---

## 8b. Fixed income accrual

Equities get a price from an API; a private CDB never will. But it is not
unknowable — the paper tracks an index that Banco Central publishes, so the value
is *computed* instead of fetched.

```
app/market/indices.py       BCB SGS client + `index_rates` storage
app/market/fixed_income.py  accrual maths, per-paper terms, valuation
```

Three decisions worth recording:

- **Terms are user data, not import data.** The B3 export omits the contracted
  rate entirely, so `fixed_income_terms` holds it (defaulting to 100 % of CDI).
  Guessing a rate silently would produce confident, wrong numbers.
- **Each purchase accrues from its own settlement date**, never from an average
  date, so tranches are valued correctly.
- **The result is exposed as a unit price** (`value / quantity`), so the rest of
  the app keeps working in "quantity × price" terms regardless of whether the
  paper was bought as 30.000 units of R$ 1,00 or 3 units of R$ 1.000,00.

The accrual follows the CETIP/B3 252-business-day convention and is validated
against a real redemption: from a matured CDB's cash flows alone the solver
recovers a contracted rate well above the 100 % of CDI assumed by default.
That inverse calculation is offered in the UI, because a redeemed paper is the
only place the export reveals what a paper actually yielded.

SGS rejects ranges longer than ten years, so `fetch_series` chunks requests; a
short publication lag (CDI lands D+1) is tolerated before a series is called
stale, otherwise every weekend would raise a false alarm.

---

## 8c. Tesouro Direto pricing

A government bond is neither quotable by an equity API nor computable from an
index — its price is set by the yield curve. The Treasury publishes it directly,
so this is a fetch, not a calculation:

```
app/market/treasury.py      Tesouro Transparente client + matching + `treasury_prices`
```

Three decisions worth recording:

- **Marked at the sell side.** The feed carries two prices per day; positions use
  `PU Venda`, what an early redemption pays. On a 2084 paper the buy/sell spread
  is ~5 %, so marking at the buy side would show a profit that cannot be
  realised. Both sides are stored and both are shown, so the spread is visible
  rather than hidden inside a single number.
- **Product matching is explicit, never fuzzy.** Names are folded (accents,
  punctuation, case) and matched against the feed's own title list; a paper the
  feed does not know is reported as `unmatched` instead of being priced by a
  near-miss. Renda+ and Educa+ carry a documented year offset because the year in
  their name is when payments *start*, not when the title matures.
- **The sell side is mirrored into `price_history`**, so the position joins the
  historical value chart through the same path as every equity, and the asset
  page gets a real price curve.

Only held papers are parsed out of the ~14 MB file, and the download runs once at
startup plus daily from beat — never per request.

The `contracted_rate` helper reads each purchase date's yield back from the feed
and reports an amount-weighted average, which is the one number that explains a
Tesouro position's mark-to-market: the price fell because rates rose.

---

## 8d. Currencies

The portfolio is kept in reais, but part of it is bought in dollars. Net worth
has to be one number, so those dollars must be converted — and *which rate* is
used is a modelling decision, not a detail.

**Amounts are stored in the currency they happened in.** A US purchase is saved
in dollars, with `Transaction.fx_rate` recording the rate that applied on its
trade date. Nothing is converted on the way in, so the asset page can show a
holding in the dollars it was actually bought with.

**Two different rates, on purpose.** Cost basis is converted at *each purchase's
own* rate; market value at *today's*. That is the honest split — what it cost
then against what it is worth now — and the difference between the two is the
currency gain, which would vanish if both used the same rate. It also matches
what Brazilian tax reporting expects, which is why rates come from Banco
Central's PTAX series (`market/fx.py`, SGS series 1 for the dollar and 21619 for
the euro) rather than a live market quote.

**Domestic and offshore are separate asset families.** ``STOCK``/``STOCK_INTL``,
``ETF``/``ETF_INTL`` and ``FII``/``REIT`` are the same instruments listed in
different places, and they are kept apart because merging them answers no useful
question: they trade in different currencies under different tax rules, and how
much sits abroad is exactly what the allocation chart is for. The family is
decided by the asset's currency — `classify_us_asset_kind` is only ever reached
for a non-BRL holding — so a B3 ticker can never land in an offshore bucket or
the reverse. `reclassify_assets` re-derives this on every start, which is what
migrates existing portfolios when the rules change.

**The engine never learned about currencies.** `PortfolioService.base_movements()`
multiplies each foreign movement by its stored rate and the same pure replay runs
a second time over the result. Running the engine twice costs one extra pass over
a few thousand rows and keeps it a pure function of its movements, which is what
makes it testable.

**A missing rate produces zero, not one.** If a foreign holding has no rate
available, falling back to `1` would quietly add dollars into a total of reais —
a number that looks entirely plausible and is wrong by a factor of five. Instead
the position is left out of the converted totals and named in
`overview()["unconverted_positions"]`, so the omission is visible. In practice
the series is downloaded at startup before the first import, and
`backfill_transaction_fx` fills in anything imported during an outage.

One consequence worth knowing: quote symbols cannot be derived from the ticker
shape. `BAC.SA` is a Brazilian company with nothing to do with Bank of America,
so `market/service.py` decides the `.SA` suffix from the asset's *currency*.

---

## 9. Background jobs

| Task | Schedule | Purpose |
|---|---|---|
| `refresh_quotes_task` | every `PRICE_REFRESH_MINUTES` | Latest prices |
| `rebuild_snapshots_task` | daily at `SNAPSHOT_TIME` | Materialise daily history |
| `backup_database_task` | daily at `BACKUP_TIME` | `pg_dump` + rotation + cloud sync |
| `backup_catch_up_task` | hourly | Runs the daily backup if its slot was missed (host off) |
| `backfill_history_task` | on demand | Download full daily closes |
| `sync_indices_task` | daily 09:20 | CDI/Selic/IPCA from Banco Central |
| `sync_treasury_task` | daily 11:15 | Tesouro Direto prices from Tesouro Transparente |

Tasks own their session via `session_scope()` and write to `audit_logs`.

In Docker these run under Celery (worker + beat, Redis as broker). The desktop
build has neither: `app/desktop/scheduler.py` replicates the same schedule with
an in-process APScheduler, calling the same underlying functions — possible
because no HTTP route ever *enqueues* a task; Celery only ever ran the clock.
The two schedules are maintained together: a job added to one belongs in the
other.

### 9b. The desktop build

`app/desktop/` packages the backend into a single Windows process for users
without Docker: SQLite in `%LOCALAPPDATA%\GumbInvest` (the engine in
`app/db/session.py` switches pooling and PRAGMAs by dialect; WAL lets the
scheduler write while requests read) and the built SPA served by FastAPI
itself (same-origin, so LAN/phone access needs no CORS or proxy). The window
and tray belong to the Electron shell in `desktop-shell/`: it spawns
`headless.py` (via the PyInstaller `gumbinvest-server` bundle), reads
`port.txt` from the data dir, shows a loading page until `/api/health`
answers, and draws the title bar in the app theme through Electron's Window
Controls Overlay — a custom-chrome attempt with pywebview was reverted
because its drag machinery and JS bridge race the SPA. Nothing in `app.main`
imports `app.desktop` — Docker never loads it; the guarded SPA block in
`main.py` is switched by `DESKTOP_MODE`, which only the desktop entrypoint
sets.

Two consequences worth knowing before touching migrations or money columns:

- Migrations must stay SQLite-compatible (`render_as_batch` is on for SQLite;
  `tests/test_migrations_sqlite.py` runs the whole chain and fails the build
  otherwise), because `alembic upgrade head` on first launch after an update
  is how desktop users' data moves forward.
- SQLite stores `Numeric` through float64: exact to 15 significant digits,
  quantized beyond. `tests/test_sqlite_decimal.py` pins that boundary and
  documents why a TEXT-storing workaround was rejected. PostgreSQL is exact
  at any width.

Moving a history between instances (Docker → desktop, or back) uses the
`.gumbinvest` format — `app/services/full_backup.py`: gzipped JSON of every
table in FK-safe order, primary keys preserved, Decimals as strings, the
source's alembic revision embedded. It is deliberately a *clone with a
refusal*, not a merge: import only proceeds when the target has zero
transactions, because merging two histories would need the dedup machinery to
arbitrate every row. Exported from `GET /api/imports/export`; imported through
the same upload endpoint as every other file (gzip magic tells it apart).

The cloud backup (`app/services/cloud_backup/`) wraps the same export: the
nightly backup job also uploads a `.gumbinvest` (optionally AES-GCM-encrypted
under a user passphrase) to whichever of Google Drive / Dropbox the user
connected in Configurações → Backup, keeps the newest `BACKUP_KEEP` files
there, and can restore one through `import_snapshot` — every refusal above
still applies, with one deliberate exception: a typed-confirmation reset
(`confirm_replace`) that dumps the current database locally, then wipes and
clones. Still never a merge. Two boundaries shape it. First, providers read credentials
DB-first with the env as fallback, never the settings singleton alone:
`apply_stored_secrets()` runs only in the FastAPI lifespan, so the Celery
worker's singleton never sees keys saved through the UI. Second, the nightly
run happens in the worker while the status poll answers from the backend
container, so its outcome lives in a durable `app_settings` row
(`cloud_backup_status`) rather than an in-memory job — only the manual
"Enviar agora", which is HTTP-triggered and therefore shares a process with
its poller, uses `JobRegistry`. OAuth is deliberately redirect-free (Google's
device-code flow, Dropbox's no-redirect PKCE): the desktop build has no fixed
port to register a callback on, and a pasted code works everywhere.

React 18 + TypeScript + Vite, Tailwind for styling, TanStack Query for server
state, Recharts for charts, React Router for navigation. No global state library:
everything on screen is server state, and Query already models that.

**Charts** follow a fixed set of rules encoded in `src/lib/colors.ts` and
`src/components/charts.tsx`:

- a validated categorical palette, assigned **in fixed order and never cycled**; a
  ninth series folds into "Outros" instead of inventing a hue;
- colour follows the entity, not its rank, so filtering never repaints survivors;
- one y-axis per chart — never a dual axis;
- status colours (positive/negative) are reserved for polarity and never reused as
  a categorical slot;
- a legend whenever there are two or more series, plus direct labels, so identity is
  never carried by colour alone;
- every chart offers a table view, and entry animations respect
  `prefers-reduced-motion`;
- magnitude uses a **sequential** wash — one hue, varying intensity
  (`sequentialFill`) — as in the income matrix, never a categorical hue.

The palette was validated against the app's chart surface (`#12141a`) for
lightness band, chroma, colour-vision-deficiency separation and 3:1 contrast.

Two behaviours are set globally in `styles.css` rather than per chart, because
they apply to every plotted form:

- **Tooltips sit at `z-index: 20`.** The allocation donut paints its total in an
  overlay above the plot, and without this the tooltip renders behind that number.
  Recharts owns the wrapper's inline style, so `wrapperStyle` does not survive.
- **Mouse focus draws no ring.** A focus outline on an SVG shape is drawn around
  its *bounding box*, which on a pie arc reads as a stray square; `:focus-visible`
  keeps the ring for keyboard users, who still have the table view as well.

---

## 11. Performance

- Import is one query for existing keys, then one bulk insert — a
  multi-thousand-row export imports in well under a second.
- The engine replays those rows in milliseconds, so positions are computed per
  request instead of being cached and risking staleness.
- Price history is loaded once per request into a per-asset sorted list and looked
  up by binary search.
- Aggregations (income, contributions, annual) are SQL `GROUP BY`, not Python loops.
- The API is stateless; scale by adding backend replicas behind the proxy.

---

## 12. Extending the system

**A new broker wording** → one entry in `_RULES` in `importer/classifier.py`.
Existing transactions pick up the fix on the next import; already-imported rows can
be re-classified by re-uploading the file after clearing the batch.

**A new market data provider** → subclass `MarketDataProvider`, add it to
`_REGISTRY`, set the env var. Nothing else changes.

**A new metric** → add it to `Position` (engine) if it is a replay rule, or to
`PortfolioService` if it is an aggregation, then expose it on a route.

**A new CSV layout** → `parse_csv` validates required columns and raises
`CsvFormatError` with the columns it actually found; add the header aliases to the
normaliser and the shape to `parse_product`.

**A new statement format** → subclass `StatementParser` in `importer/pdf/`,
implement `matches()` (a cheap text sniff) and `parse()`, and register it in
`PARSERS`. Emit the canonical labels from `importer/pdf/movements.py` rather than
inventing new ones, and populate `totals` and `holdings` — those are what make
the parser check itself. Then drop the files into `data/` and run
`pytest tests/test_pdf_parsers.py`, which reconciles every statement against its
own printed figures.

**A broker changes its layout mid-history** → this is the normal case, not the
exception; four of these formats are already two generations of two brokers. If
the change is cosmetic (Nomad translated its statement from Portuguese to
English in February 2026) extend the existing parser's label pairs. If the table
structure changed, write a second parser: they are cheap, and the sniffer keeps
them apart.

**A new currency** → add its PTAX series to `SERIES` in `market/fx.py`. The
portfolio service already converts per-currency; nothing else needs to change.

**A new crypto exchange** → a module in `importer/crypto/` exposing `matches()`
and `parse()` that returns a `ParsedTradeFile`, registered in `PARSERS`. Emit
`CryptoTrade` with both sides named — and `CryptoEvent` for anything that is not
a trade — then let `_crypto_legs` decide what becomes a movement: the swap model,
the dollar normalisation and the fee rules are shared and should not be
re-implemented per exchange. Prefer whichever export is a **ledger** over
whichever is a trade list, for the reason in [§3c](#3c-crypto-exchanges-csv), and
give it the lower `_CRYPTO_PRIORITY`. New coins need nothing: an unknown
symbol keeps its own ticker as a name. Add it to `DOLLAR_PEGGED` in
`importer/crypto/symbols.py` if it is a stablecoin, and to `PROVIDER_SYMBOLS` if
the price provider files it under a different ticker.
