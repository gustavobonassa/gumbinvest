# Importing your data

How GumbInvest reads the raw exports — the B3 CSV, Avenue/Nomad statement PDFs and Binance exports — and why re-sending anything is always safe.

*Part of the [GumbInvest](../README.md) documentation.*


---

## How the CSV import works

The expected header — exactly what B3 exports:

```
Entrada/Saída,Data,Movimentação,Produto,Instituição,Quantidade,Preço unitário,Valor da Operação
```

Pipeline: **parse → classify → de-duplicate → persist**.

1. **Parse** ([`app/importer/parser.py`](../backend/app/importer/parser.py)) turns each
   line into a normalised row: pt-BR decimals, dates, a stable ticker and the
   instrument family. Products come in four shapes and all are handled:
   `PETR4 - PETROLEO BRASILEIRO`, `CDB - CDB7246C5YO - BANCO X`, `Futuro - WING21`,
   `Opção de Compra - PETRM59 - PETR`, and bare names such as
   `Tesouro Renda+ Aposentadoria Extra 2065`. Broker spellings are normalised
   (the sample export writes one broker nine different ways).
2. **Classify** ([`app/importer/classifier.py`](../backend/app/importer/classifier.py))
   maps `(movement label, direction)` onto an operation type and a *position effect*.
   Adding a new broker wording is one line in the table.
3. **De-duplicate**: each row gets `sha256(date|direction|movement|ticker|broker|qty|price|amount)`
   plus an **occurrence counter** — its index among identical rows *within that file*.
   Same file again → same keys → everything is a duplicate. A monthly file that
   overlaps the previous one only adds the new rows. Two identical payments on the
   same day get occurrences 0 and 1 and are both kept.
4. **Persist**: bulk insert, plus an `ImportBatch` row holding counts, per-row errors,
   operation breakdown, unmapped labels and the date range.

A bad line never aborts an import — it lands in the import log instead.



---

## Broker statements (Avenue, Nomad)

The B3 export is a CSV; the offshore brokers only give you monthly PDFs. Drop
them on the import page — one at a time or a whole folder — and they go through
the same pipeline. The file type is detected from its contents, so there is no
separate upload box.

Four layouts are read, which is every generation of both brokers:

| Broker | Report | Period covered |
|---|---|---|
| Avenue | `stmt<Month> …Statement_*.pdf` (DriveWealth) | 2020-11 → 2021-06 |
| Avenue | `Doc_*_STATEMENT_*.pdf` (Apex Clearing) | 2021-05 → today |
| Avenue | `Stmt_YYYYMMDD.pdf` (Avenue's own) | 2025-01 → today |
| Nomad | `…monthly_statement_*.pdf` (DriveWealth) | 2025-04 → 2025-11 |
| Nomad | `…monthly_statement_*.pdf` (Apex Ascend) | 2025-11 → today |

### Should I import both Avenue reports?

**Yes — import everything you have.** Neither report is a superset of the other:

- the Apex statement is the *only* source for 2021-07 → 2024-12, and it prices a
  trade including the US$ 2.50 commission;
- Avenue's own statement covers 2025 onwards and lists dividends the Apex file
  omits entirely.

They also disagree about the same events — the same dividend is dated a day
apart, and the same purchase differs by the commission — so importing both
naively would double-count everything. The importer recognises an event reported
by two different statements of the same broker and keeps it once, listing every
such match in the import log. The same mechanism handles the two halves of a
month where an account migrated between custodians: those are *not* duplicates
and both are kept.

The upshot: send everything, in any order, as many times as you like.

### Finding the months you never downloaded

The import page shows a **coverage map** — one square per month per account,
green when a statement is held and red when none is. Underneath it reports two
subtler problems:

- **balance breaks** — a statement whose opening balance does not continue the
  previous one's closing balance, which is what a missing month looks like even
  when the calendar appears complete;
- **position drift** — assets whose computed quantity disagrees with the figure
  the latest statement itself prints. This is the strongest check: it catches a
  file that is present but was only partly read, or a corporate action nobody
  recorded.

### What is not imported

Deposits, withdrawals, journals and money-market sweeps are read (so that each
section reconciles against the statement's own printed totals) and then dropped:
they belong to no asset. Their counts appear in the import log, along with any
row the statement gave no security for.



---

## Crypto (Binance)

Export from **Wallet → Transaction History** and drop the CSV on the import
page, or leave it in `data/binance/` for the auto-import. Three files are read,
but only the first one really matters:

| File | What it holds | Use it for |
|---|---|---|
| `Binance-Transaction-History-*.csv` (`…Histórico-de-Transações…`) | one row per **balance change** — trades, deposits, withdrawals, Convert, Earn, staking, airdrops, futures | **export this one** |
| `Binance-Spot-Trade-History-*.csv` | one row per **fill**, trading only | a partial history, if the above is unavailable |
| `Binance-Spot-Order-History-*.csv` | one row per **order**, trading only, no fees | last resort |

### Why the transaction history and not the spot exports

The spot exports describe trading and nothing else, which is not most of what
happens on an exchange. Reconstructing an account from spot trades alone leaves
coins with negative balances — you cannot sell what the history never bought —
and misses holdings acquired through Convert, Earn, P2P or deposits entirely.
Read from the full ledger, every coin balances and nothing is flagged. The test
suite asserts exactly that: the reconstructed positions are compared with the
per-coin totals of the CSV itself, computed independently of the importer.

### The `Earn` account is a mirror

Binance books a Simple Earn reward twice — once in the `Earn` account
(`… - Rewards Income`) and again in `Spot` (`… Interest` / `… Rewards`) minutes
later, with no offsetting debit on either side. Counting both doubles every
reward, so the `Earn` rows are dropped and `Spot` is authoritative. That is what
reproduces the balances the exchange actually shows.

### Staked coins are still yours

Subscribing to Simple Earn or staking removes coins from the balance the
exchange reports — and nothing else. They are still on the exchange, still
owned, still earning. So they stay in the position, with their cost, and are
reported separately only so the app can say how much of a holding is locked.

A **withdrawal** is the opposite case and does leave: once coins are off the
exchange nothing here knows what became of them. Their cost waits in case they
come back (see below), but the quantity is gone from the portfolio.

| | Quantity | Cost |
|---|---|---|
| Into Earn / staking | stays in the position | stays with it |
| Withdrawn to a wallet | leaves the position | parked, reclaimed if it returns |

### A trade is a swap, not a purchase

Every other importer in this app deals with a market where cash is the
counterparty, and cash is not tracked. An exchange is different: `ETHUSDT` BUY
spends Tether to get Ether, `USDTBRL` BUY spends reais to get Tether. So one row
becomes up to three movements:

- the **instrument leg** — the coin the trade is about;
- the **funding leg** — what paid for it, when that is itself a holding. Buying
  Ether with Tether has to take those Tethers out of the portfolio. Leaving them
  in would show a balance that was already spent *and* read the swap as fresh
  capital arriving, inflating "how much did I put in" by every trade you ever made;
- the **fee leg** — only when the fee was charged in a *third* coin (Binance
  discounts fees paid in BNB). A fee taken out of what you bought is not a leg:
  it simply never arrived, so the quantity acquired is smaller.

### Two classes, not one

| Class | What lands in it |
|---|---|
| **Cripto** | coins and tokens — BTC, ETH, SOL, … |
| **Stablecoins** | USDT, USDC, BUSD and friends |

Kept apart on purpose: a balance of dollar-pegged tokens is dollar cash parked on
the exchange, not crypto exposure, and merging the two makes the allocation chart
claim a risk that is not there.

### Prices and currency

Coins are booked and quoted in **dollars** (`BTC-USD`), exactly like a US share,
and converted to reais by the same PTAX machinery — see
[Investments abroad and currencies](calculations.md#investments-abroad-and-currencies). Dollars
rather than reais because `BTC-BRL` exists for a handful of coins while `BTC-USD`
exists for all of them, so every alt coin gets a real quote instead of sitting at
cost. A trade settled in reais is converted at that day's PTAX and the rate is
stored on the movement, so the reais actually paid come back exactly.

Dollar-pegged tokens *are* dollars: 100 USDT is booked as US$ 100, which is the
whole point of holding them. The handful of trades priced in a coin instead
(`NEARBTC`, `UNIBNB`) keep that coin as their currency; the coin's own daily
close is published as an exchange rate so they still reach the base currency —
see [`app/market/crypto.py`](../backend/app/market/crypto.py).

### Withdrawing to a wallet is not a sale

An exchange is often used as a bridge: buy a coin, withdraw it minutes later,
deposit it again months on, sell. Treating the withdrawal as the coins leaving
for good writes off the purchase, so they return free and the eventual sale
invents a gain the size of what was paid. Withdrawals therefore **park** their
cost exactly as staking does, and a later deposit reclaims it.

### Coins that arrive from outside are not profit

Whatever is deposited beyond what was ever withdrawn was bought somewhere this
file cannot see. Nothing is invented for it — a made-up cost is worse than a
missing one — but neither are its proceeds counted as a gain, because **a result
is revenue minus cost and there is no cost to subtract**. Selling those units
puts the money in `uncosted_proceeds`, reported alongside the realised result
rather than inside it, and the position carries a warning naming both the
quantity and the amount. Stablecoin deposits are the exception: a dollar-pegged
token is booked at face value.

### When a crypto position looks wrong

Positions are derived, so there is always an answer in the movements behind
them:

```bash
docker compose exec backend python scripts/crypto_doctor.py            # USDT
docker compose exec backend python scripts/crypto_doctor.py --ticker BTC
```

It prints which exports are on file, what the movement labels currently resolve
to, and the position with its notes. `--reimport` drops every crypto import and
reads the exports again — the repair for a database that accumulated rows from
more than one version of the importer. The B3 history and the broker statements
are left untouched.

### When the export cannot reach the real balance

Some of a position is underivable — interest that compounds *inside* a staking
product is paid into the balance without being itemised as a movement. The
asset page takes the real figure ("Saldo informado pela corretora") and appends
the difference as one more movement. Positions stay derived — nothing is
overwritten, the audit trail says where the correction came from, and stating
the same balance twice is a no-op.



---

## Known data caveats

These come from the source exports themselves, not from the importer. The app
surfaces them rather than papering over them:

- **Corporate actions are never linked** in the B3 export — see
  [Corporate actions](calculations.md#corporate-actions).
- **Sales without a matching purchase.** If your export starts after a position
  was opened, the disposal has no cost to work against; the proceeds are booked
  as realised and the asset is flagged in **Configurações → Qualidade dos dados**.
- **Delisted tickers have no quotes.** Those positions are valued at average
  cost and listed on the dashboard banner; a manual price can be set per asset.
- **A Tesouro position can look worse than it is.** It is marked at the
  Treasury's buyback price, and on very long papers the buy/sell spread alone
  is several percent. The **Renda fixa** page shows both prices — and neither
  matters if the paper is held to maturity.
- **The two Avenue reports disagree with each other** (dates and commissions).
  Whichever is imported first for a month sets the amounts; the second only
  fills in what the first was missing.
- **Nomad reports dividends net of withholding.** The gross and the tax are
  reconstructed from the description and only booked separately when the
  arithmetic reproduces the printed net to the cent.
- **The Binance spot exports are trades only** — export the transaction
  history instead; see [Crypto (Binance)](#crypto-binance).
- **Coins deposited from outside have no cost on file.** Their proceeds are
  reported as `uncosted_proceeds`, not invented as gains.

