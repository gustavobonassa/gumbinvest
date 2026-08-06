# AI features, screener and tools

The optional AI layer and the analysis tools that ship alongside the portfolio itself.

*Part of the [GumbInvest](../README.md) documentation.*


---

## AI features

Everything AI is **off until you add a key**, and the key is yours:
**Configurações → Inteligência artificial** takes an API key per provider —
Anthropic (Claude), OpenAI (GPT), Google (Gemini), xAI (Grok) or Groq — and a
model choice per feature. Keys are stored in your local database, never leave
your machine except to call the provider you chose, and are stripped from every
export and backup.

**Analista IA** is the chat in the top bar. On any page it talks about the
whole portfolio; on an asset page it talks about that asset, with the company's
fundamentals and your position assembled server-side as context. The model can
search the web for news and current data, and answers stream in as they are
generated. Conversations are saved to **Conversas IA** and can be reopened
where they happened.

**Carteira IA** builds *virtual* wallets — the model gets R$ 10.000 virtuais
per asset category, screens the local asset universe for candidates, verifies
each pick against live market data, and writes down its reasoning per position.
Wallets are pinned to the provider/model that created them, so different models
can compete against each other (and against CDI and Ibovespa) over time.
Nothing a Carteira IA does touches your real portfolio.

Long AI runs (wallet generation, reviews) execute as background jobs with
progress reporting — start one, navigate away, come back.



---

## Screener and tools

**Universo de ativos** downloads B3's price files and CVM's company filings and
reduces them to one local table of every listed paper — price, liquidity,
dividend yield, P/L, P/VP, debt, revenue and margins, each figure tagged with
the fiscal year it came from. On top of it:

- a **screener** with per-column sorting and threshold filters (DY mínimo, P/L
  máximo, P/VP máximo, índice membership);
- **"Como me comparo"** — where each of *your* holdings sits inside the whole
  market, by percentile, per metric;
- the sync runs in the background with per-stage progress and can be cancelled;
  a blank cell always means "not published", never zero.

**Comparador de ativos** plots any tickers side by side — indexed price
performance plus a fundamentals table. Comparing an asset you never held
quietly creates a watch-only asset, so its quotes and fundamentals exist by the
time the page renders.

**Carteiras públicas** shows the quarterly 13F portfolios of well-known
investors (Buffett, Burry, …) from SEC EDGAR, with the page honest about the
format's limits: quarterly, up to 45 days late, US-listed longs only.

**Calculadora de juros compostos** projects wealth with monthly contributions
— seeded with your current net worth and your real average contribution of the
last twelve months, every field editable for what-if scenarios.

