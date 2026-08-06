# How the numbers are computed

The replay engine, multi-currency conversion, corporate actions, and how fixed income and Tesouro Direto are valued.

*Part of the [GumbInvest](../README.md) documentation.*


---

## How the calculations work

The engine ([`app/portfolio/engine.py`](../backend/app/portfolio/engine.py)) is a pure
replay over the movements: no database, no network, fully unit tested. Every movement
carries a **position effect**, and that is all the engine looks at:

| Effect | Meaning |
|---|---|
| `ACQUIRE` | quantity in, cost in — a purchase |
| `DISPOSE` | quantity out; realises `(price − average) × quantity` |
| `QTY_IN_FREE` | quantity in at zero cost — splits, bonuses, subscription receipts |
| `QTY_OUT_FREE` | quantity out at cost — fractions removed, receipts converted |
| `CASH_IN` / `CASH_OUT` | dividends, JCP, yields, interest, fees, taxes |
| `RETURN_OF_CAPITAL` | reduces the cost basis; only the excess is realised |
| `QTY_SYNC` | B3 `Atualização` — resolved per row, see below |
| `QTY_RESTATE` | share restructuring — the credit *replaces* the position |
| `REALIZE` | cash for quantity that already left (fraction auctions) |
| `LEDGER_ONLY` | internal custody transfer; deliberately no effect |
| `NONE` | audit trail only |

Consequences that matter:

- **A sale never changes the average price.** Cost leaves proportionally; the
  difference is the realised result.
- **A split needs no ratio.** B3 credits the *extra* shares, so quantity grows while
  cost stays put and the average price dilutes exactly right.
- **Amortisations are not income.** They reduce the cost basis.
- **Same-day ordering.** Exports have no intraday sequence, so credits are applied
  before debits — otherwise a same-day buy+sell would hit an empty position.
- **A share restructuring replaces the position.** See below.
- **Fraction auctions do not remove quantity twice.** B3 removes the fraction
  (`Fração em Ativos`) weeks before it pays for it (`Leilão de Fração`), so the
  auction only books proceeds, net of the cost that left with the fraction.
- **Broker transfers cancel.** A credit at one broker paired with a debit at another
  (same day, asset, quantity) is neutralised, which protects the cost basis. Unpaired
  transfers still move quantity.

### Splits, groupings and share restructurings

B3 writes three different things with overlapping words, and the difference is
worth real money:

| Rows on a day | Meaning | Applied as |
|---|---|---|
| `Desdobro` alone | split — the *extra* shares | delta |
| `Bonificação em Ativos` | bonus shares | delta |
| `Desdobro` **and** `Grupamento` | share restructuring (grouping + split) | the credits **are** the resulting position |

The third case is the trap: the old shares are consumed *without an explicit
debit*. Treating those credits as a delta leaves thousands of phantom shares.
The engine detects the pattern (`Grupamento` + `Desdobro`, same asset, same
day) and restates the position to the day's total, preserving cost basis so the
average price rescales by the restructuring ratio.

### The `Atualização` problem

B3 uses one label for two incompatible things: sometimes it *credits* shares (fund
events, ticker conversions), sometimes it merely *restates* a position that already
exists (typically after custody migrates between brokers). Applying it blindly
either double-counts a position or drops real shares.

The engine sums the `Atualização` quantities reported for an asset on a given day
(B3 states them per broker) and compares that total with the position currently
held: an exact match is a restatement and is skipped, anything else is applied as a
free quantity credit. Every decision is recorded in the asset's notes so it is
auditable.



---

## Investments abroad and currencies

Amounts are stored in the currency they happened in, and converted only where a
single number is required:

| Where | Currency |
|---|---|
| Asset page — average price, movements, dividends | the asset's own (US$ for a US holding) |
| Asset list, dashboard, allocation, net worth | R$ |

Offshore holdings also get their own asset classes, so the allocation chart does
not merge Petrobras with Nike:

| Brazil | Abroad |
|---|---|
| Ações | Stocks |
| ETFs | ETFs Exterior |
| FIIs | REITs |
| — | Cripto / Stablecoins |

The class follows the asset's currency, so a B3 ticker can never end up in an
offshore bucket. Crypto is offshore by nature: it is quoted in dollars and
carried to reais by the same two rates as any US holding.

Conversion uses Banco Central's PTAX rate, and deliberately uses two different
ones: the **cost basis** is converted at each purchase's own trade-date rate,
while the **market value** uses today's. The difference between them is the
currency gain — it would disappear if both used the same rate. This is also what
Brazilian tax reporting expects.



---

## Corporate actions

The export has a systematic blind spot: when a company is renamed, merged or
restructured, **B3 credits the new ticker and never debits the old one**, and no
row anywhere links the two. Replayed literally that leaves the predecessor
holding a phantom position with the entire cost basis, and the successor holding
shares that arrived for free.

**Configurações → Eventos corporativos** closes the loop. It lists every
*stranded* position — open, unpriced, no longer moving — and proposes the asset
that replaced it, ranked by the evidence:

- the successor is still held (a line that was itself closed was a pass-through);
- it was credited with **exactly** the quantity being replaced;
- it was credited on the same day.

Applying one records an `asset_successions` row, which the engine reads on every
replay: the predecessor is closed and its cost basis is carried into the
successor. Only *cost* moves — the quantity was already credited by B3.

Two options matter:

- **Caixa recebido** — cash paid out in the event (a cash-plus-shares merger).
  It reduces the cost carried over rather than being booked as a gain; anything
  beyond the remaining basis is realised.
- **Baixar o ativo** (no successor) — for intermediate vehicles. B3 sometimes
  hands out holding units mid-merger and redeems them days later; left alone,
  that zero-cost redemption invents a realised gain. Every movement of a
  written-off asset is dropped from the replay.

Nothing is applied automatically. The export cannot tell "the successor" from
"an intermediate vehicle credited the same day", and choosing wrong silently
would produce confident, wrong numbers.



---

## Fixed income (CDB, LCI, LCA)

No public API quotes a private CDB, but its value is *computable*: the paper
tracks a published index, and Banco Central publishes the index. The **Renda
fixa** page accrues every application from its own settlement date.

**Index data.** Series pulled from BCB's SGS API (public, no key, chunked to
respect its 10-year request limit) and refreshed daily:

| Code | SGS series | Unit |
|---|---|---|
| `CDI` | 12 | % per business day |
| `SELIC` | 11 | % per business day |
| `IPCA` | 433 | % per month |

**The maths** follows the CETIP/B3 252-business-day convention. For a paper
paying `p` % of CDI:

```
factor = Π ( 1 + TDI_k × p/100 )      TDI_k = daily DI rate / 100
```

with an annual spread compounding on top as `(1 + spread) ** (business_days/252)`,
and prefixed papers using `(1 + rate) ** (business_days/252)` alone. Each purchase
accrues from its own date, so a position built in tranches is valued correctly.

**Rates are configurable because the export omits them.** New papers default to
100 % of CDI. Set the real terms per paper on the **Renda fixa** page.

**Finding the real rate.** When a paper has been redeemed, its cash flows are a
closed experiment — principal in, principal + interest out. The app solves for
the percentage that reproduces them and offers it as a one-click suggestion.



---

## Tesouro Direto

A Tesouro Direto title has no ticker and no broker quote, but the Treasury
publishes every title's price and yield for every business day as open data.
The file (~14 MB, semicolon separated, pt-BR decimals) is downloaded once on
first run and refreshed every morning. Only the papers actually held are parsed
out of it, so the database stores a few hundred rows, not millions.

**Buy side vs sell side.** The Treasury quotes a spread, and the column names
are from the investor's side: `PU Compra` is what the investor pays, `PU Venda`
is what the Treasury pays to buy the paper back. Positions are marked at
**`PU Venda`**, which is what an early redemption actually pays and what the
official Tesouro Direto statement shows. Both prices and both yields are shown
on the **Renda fixa** page so the spread stays visible.

**Product names.** For most titles the year in the name is the maturity year.
The two instalment products are not: Renda+ pays 240 monthly instalments (20
years) and Educa+ pays 60 (5 years), and the name states when payments *start*
— the series is keyed by the last one. "Renda+ 2065" is therefore the series
maturing 15/12/2084, and the matcher applies that offset automatically.

**Contracted yield.** The B3 export states the price paid but never the rate, so
the rate is read back from the feed on each purchase date and reported as an
amount-weighted average. Comparing it to today's rate explains the
mark-to-market directly: a Tesouro position falls precisely when rates rise.

