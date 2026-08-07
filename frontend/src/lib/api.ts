/** Typed client for the GumbInvest API. */
const BASE = (import.meta.env.VITE_API_BASE ?? "/api").replace(/\/$/, "");
/** For the rare caller that needs raw fetch (SSE streaming) instead of request(). */
export const API_BASE = BASE;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: init?.body instanceof FormData ? undefined : { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (payload?.detail) detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
    } catch {
      /* keep the status text */
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const query = (params: Record<string, unknown | undefined>) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) value.forEach((item) => search.append(key, String(item)));
    else search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
};

// --- types ---------------------------------------------------------------
export interface Overview {
  /** Currency every figure below is expressed in. */
  base_currency: string;
  /** Today's rate for each foreign currency held, e.g. `{ USD: "5.42" }`. */
  fx_rates: Record<string, string>;
  /** Portion of the market value that comes from non-base-currency assets. */
  foreign_value: number;
  market_value: number;
  cost_basis: number;
  invested: number;
  net_contributed: number;
  unrealized_pnl: number;
  unrealized_pct: number;
  realized_pnl: number;
  income_total: number;
  returned_capital: number;
  /** Sales of quantity with no purchase behind it; outside every profit figure. */
  uncosted_proceeds: number;
  /** Tickers still holding quantity whose cost is unknown. */
  uncosted_positions: string[];
  total_profit: number;
  total_profit_pct: number;
  day_change: number;
  day_change_pct: number;
  cash_balance: number;
  positions_count: number;
  assets_tracked: number;
  priced_positions: number;
  unpriced_positions: string[];
  /** Held tickers whose quote fetch failed transiently and is queued for retry. */
  pending_quotes: string[];
  last_quote_at: string | null;
}

/**
 * One entry in the header bell.
 *
 * Deliberately generic: the backend registry (app/services/notifications.py)
 * grows new sources without this type changing, so a price-target alert renders
 * with the same component as a queued quote refresh.
 */
export interface AppNotification {
  id: string;
  kind: string;
  level: "info" | "success" | "warning";
  title: string;
  body: string;
  progress: { done: number; total: number; label: string } | null;
  items: string[];
  at: string | null;
}

export interface PositionRow {
  asset_id: number;
  ticker: string;
  name: string;
  kind: string;
  currency: string;
  quantity: number;
  average_price: number;
  current_price: number;
  has_market_price: boolean;
  price_source: string | null;
  cost_basis: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pct: number;
  realized_pnl: number;
  /**
   * Cash received for quantity that had no purchase behind it — coins
   * deposited from a wallet this history cannot see. Deliberately outside
   * `realized_pnl`: a result is proceeds minus cost, and there is no cost here.
   */
  uncosted_proceeds: number;
  uncosted_quantity: number;
  /** Part of `quantity` locked in staking / Simple Earn — owned, not spendable. */
  staked_quantity: number;
  /** Proventos recebidos, já **líquidos** do imposto retido na fonte. */
  income: number;
  /** O mesmo valor como declarado, antes da retenção — é o que `income_by_type` detalha. */
  income_gross: number;
  /** Imposto retido na fonte sobre essa renda, líquido de restituições posteriores. */
  income_withheld: number;
  income_by_type: Record<string, number>;
  returned_capital: number;
  total_return: number;
  total_return_pct: number;
  day_change: number;
  /** The same session move converted to the portfolio's currency. */
  day_change_base: number;
  day_change_pct: number | null;
  allocation_pct: number;
  /** True when the asset trades in a currency other than the portfolio's. */
  is_foreign: boolean;
  /** Today's rate from `currency` to the portfolio's base currency. */
  fx_rate: number | null;
  /** Base-currency mirrors: cost at each purchase's own rate, value at today's. */
  market_value_base: number;
  cost_basis_base: number;
  unrealized_pnl_base: number;
  realized_pnl_base: number;
  income_base: number;
  total_return_base: number;
  transactions: number;
  first_trade: string | null;
  last_trade: string | null;
  is_open: boolean;
  warnings: string[];
  notes: string[];
}

/**
 * The detail endpoint returns the full ledger under `transactions`, so the
 * movement *count* travels as `transactions_count`.
 */
export interface AssetDetail extends Omit<PositionRow, "transactions"> {
  /** False for a watch-only asset: the market knows it, the wallet never
   *  traded it. Position fields arrive zeroed and the wallet tabs hide. */
  held: boolean;
  sector: string | null;
  market_symbol: string | null;
  price_manual: boolean;
  manual_price: number | null;
  /** Free text written by the user (engine notes travel in `notes`). */
  user_notes: string | null;
  transactions: TransactionRow[];
  transactions_count: number;
  dividends: { date: string; op_type: string; amount: number; unit_price: number; quantity: number }[];
  /** Monthly income in the asset's own currency, net of withholding — the same
   *  basis the Proventos page uses, so both screens agree on a given month. */
  income_months: { period: string; gross: number; tax: number; net: number; payments: number }[];
  /** Tax and fees withheld from income over the asset's whole history. */
  income_tax: number;
}

/** A dividend the company has declared, from the B3 schedule. */
export interface ScheduledDividend {
  payment_date: string | null;
  approved_on: string | null;
  record_date: string | null;
  label: string | null;
  /** Reference period the payment relates to ("Julho-2026", "2º Trimestre/2026"). */
  period?: string | null;
  /** Amount per share, in the asset's currency. */
  rate: number;
  /** Declared with the amount known but the payment date not yet fixed. */
  date_pending?: boolean;
}

/**
 * Company data behind an asset. Every field is optional on purpose: providers
 * publish different subsets, and a missing key means "not published" rather
 * than zero.
 */
export interface Fundamentals {
  symbol: string;
  sector?: string;
  industry?: string;
  website?: string;
  summary?: string;
  /** Currency the financial statements are reported in. */
  currency?: string;
  market_cap?: number;
  pe_trailing?: number;
  pe_forward?: number;
  price_to_book?: number;
  book_value?: number;
  eps_trailing?: number;
  beta?: number;
  fifty_two_week_low?: number;
  fifty_two_week_high?: number;
  revenue?: number;
  gross_profit?: number;
  ebitda?: number;
  net_income?: number;
  profit_margin?: number;
  operating_margin?: number;
  return_on_equity?: number;
  revenue_growth?: number;
  earnings_growth?: number;
  debt_to_equity?: number;
  free_cashflow?: number;
  dividend_yield?: number;
  dividend_rate?: number;
  payout_ratio?: number;
  ex_dividend_date?: string;
  next_dividend_date?: string;
  earnings_dates?: string[];
  target_mean_price?: number;
  recommendation?: string;
  analyst_count?: number;
  announced_dividends?: ScheduledDividend[];
  recent_dividends?: ScheduledDividend[];
  /** ~4 years of annual results from Yahoo's earnings module. */
  yearly_financials?: { year: number; revenue?: number; earnings?: number }[];
  /** The company's declared payout per share folded per year (B3 registry). */
  dividends_by_year?: { year: number; total_rate: number; payments: number }[];
  fetched_at?: string;
}

export interface FundamentalsResponse {
  ticker: string;
  /** False for families with no company behind them (CDB, Tesouro, cripto). */
  supported: boolean;
  data: Fundamentals | null;
  fetched_at?: string;
  /** True when the upstream refused and a previous copy is being served. */
  stale?: boolean;
}

/** One investor's latest disclosed 13F portfolio (US-listed longs only). */
export interface InvestorWallet {
  slug: string;
  manager: string;
  fund: string;
  description: string;
  cik: string;
  /** Last day of the disclosed quarter (ISO date). */
  quarter: string;
  filed_at: string;
  previous_quarter: string | null;
  total_value: number;
  positions: number;
  holdings: {
    issuer: string;
    ticker: string | null;
    cusip: string;
    value: number;
    shares: number;
    pct: number;
    change: "new" | "increased" | "reduced" | "unchanged" | null;
  }[];
  exits: { issuer: string; ticker: string | null }[];
  caveats: string;
}

/** One entry of the "what happened" dropdown on the manual-entry form. */
export interface ManualOperation {
  code: string;
  label: string;
  /** Which fields the form must ask for: quantity × price, units, or cash. */
  needs: "trade" | "quantity" | "amount";
  hint: string | null;
  /** What the engine will do with it — resolved server-side by the classifier. */
  op_type: string;
  effect: string;
}

export interface TransactionRow {
  id: number;
  date: string;
  ticker: string;
  name: string;
  kind: string;
  op_type: string;
  effect: string;
  movement: string;
  direction: string;
  quantity: number;
  unit_price: number;
  fees: number;
  taxes: number;
  gross_amount: number;
  net_amount: number;
  broker: string | null;
  notes: string | null;
  /** Hand-entered rather than imported — the only kind that may be deleted. */
  is_manual: boolean;
}

export interface HistoryPoint {
  date: string;
  market_value: number;
  cost_basis: number;
  invested: number;
  dividends: number;
  realized: number;
  profit: number;
}

/**
 * One point of the return curve.
 *
 * `profit` is everything the portfolio has made by that date — unrealised,
 * realised and proventos — so the three components always add up to it.
 * `return_pct` is the same history as a *return*: chained period by period and
 * adjusted for deposits and withdrawals, rebased to the first day of the
 * requested range, which is what makes it comparable with `benchmarks`.
 */
export interface ProfitPoint {
  date: string;
  profit: number;
  unrealized: number;
  realized: number;
  income: number;
  cost_basis: number;
  market_value: number;
  /**
   * Cumulative return since the start of the range, in percent, **weighted by
   * how much was invested when** (Modified Dietz). This is the headline: a bad
   * year holding R$ 20 mil should not cost as much as a bad year holding
   * R$ 800 mil.
   */
  return_pct: number;
  /**
   * The same history run size-blind — every day weighted equally. The figure an
   * index is a fair comparison for, quoted under the chart rather than plotted.
   */
  twr_pct: number;
  /**
   * Share of the portfolio the return speaks for. A CDB has no daily close on
   * file, so it sits outside the percentage — reported rather than quietly
   * diluting it toward zero.
   */
  priced_share: number;
  /** Result attributed to each `AssetKind`; empty unless `group_by=kind`. */
  kinds: Record<string, number>;
  /** The same classes as cumulative return; empty unless `group_by=kind`. */
  kinds_pct: Record<string, number>;
  /** Per class, size-blind. Same relationship as `twr_pct` to `return_pct`. */
  kinds_twr_pct: Record<string, number>;
  /** Cumulative return of each benchmark over the same window (`IBOV`, `CDI`). */
  benchmarks: Record<string, number>;
}

export type ProfitRange = "6m" | "1y" | "2y" | "5y" | "max";

/** Periods the performance ranking can be measured over. */
export type PerformanceWindow = "day" | "1m" | "3m" | "6m" | "1y" | "total";

/**
 * A ranking with the measured period attached. Rows are `PositionRow`s plus the
 * result *for that window* — one pair of fields whatever was asked for, so the
 * UI never branches on the period to know which number to print.
 */
export interface PerformanceReport {
  window: PerformanceWindow;
  /** Quote timestamp for `day`, the window's last date otherwise. */
  as_of: string | null;
  best: PerformanceRow[];
  worst: PerformanceRow[];
}

export interface PerformanceRow extends PositionRow {
  window_change: number;
  /** `null` when no capital stood behind the move (quantity that arrived free). */
  window_pct: number | null;
}

export interface AllocationSlice {
  key: string;
  label: string;
  value: number;
  percent: number;
}

export interface IncomePoint {
  period: string;
  total: number;
  cumulative: number;
  [key: string]: string | number;
}

export interface ContributionPoint {
  period: string;
  bought: number;
  sold: number;
  net: number;
  cumulative: number;
}

export interface MonthlyReturn {
  period: string;
  market_value: number;
  flow: number;
  income: number;
  profit: number;
  return_pct: number;
}

export interface ImportBatch {
  id: number;
  filename: string;
  status: string;
  rows_total: number;
  rows_imported: number;
  rows_duplicate: number;
  rows_failed: number;
  created_at: string;
  finished_at: string | null;
  summary: {
    operations?: Record<string, number>;
    unknown_movements?: { movement: string; count: number }[];
    warnings?: { message: string; count: number }[];
    date_range?: { start: string | null; end: string | null };
    /** Present for broker statements only. */
    statement?: {
      format: string;
      broker: string;
      account: string;
      currency: string;
      period: { start: string | null; end: string | null };
      opening_balance: string | null;
      closing_balance: string | null;
      holdings: { symbol: string; quantity: string; cusip: string }[];
    };
    /** Present for crypto exchange exports only. */
    exchange?: {
      name: string;
      format: string;
      trades: number;
      pairs: { pair: string; count: number }[];
      /** Movements the export priced in something other than dollars. */
      native_currency: { currency: string; count: number }[];
      /** Non-trade ledger movements by operation — only a full history has these. */
      events?: Record<string, number>;
      note: string;
    };
    /** Union of what each importer can leave out; each key is optional. */
    skipped?: {
      cash_movements?: number;
      cross_source_duplicates?: number;
      cross_source_detail?: { movement: string; count: number }[];
      unattributed?: number;
      unattributed_amount?: string;
      unattributed_detail?: { movement: string; count: number }[];
      provisional_tickers?: number;
      /** Exchange orders that never executed. */
      cancelled_orders?: number;
      /** Fees charged in a currency the trade was not priced in. */
      unconverted_fees?: number;
    };
  };
  issues?: { line: number | null; error: string }[];
}

/** One month of one broker's statement series. */
export interface CoverageMonth {
  month: string;
  files: string[];
  formats: string[];
  opening_balance: string | null;
  closing_balance: string | null;
  transactions: number;
}

export interface CoverageAccount {
  broker: string;
  account_ref: string;
  currency: string;
  first_month: string | null;
  last_month: string | null;
  statements: number;
  is_complete: boolean;
  months: CoverageMonth[];
  /** Months with no statement at all, between the first and the last held. */
  missing_months: string[];
  /** Months whose opening balance does not continue the previous close. */
  balance_breaks: {
    month: string;
    previous_month: string;
    previous_closing: string;
    opening: string;
    difference: string;
  }[];
  /** Assets whose replayed quantity disagrees with the broker's own figure. */
  position_drift: { ticker: string; reported: string; computed: string; difference: string }[];
}

export interface FixedIncomeTerms {
  index_code: "CDI" | "SELIC" | "IPCA" | "PRE";
  percent_of_index: number;
  spread_annual: number;
  fixed_rate_annual: number;
  maturity_date: string | null;
  pays_periodic_interest: boolean;
  notes: string | null;
}

export interface FixedIncomeItem {
  ticker: string;
  name: string;
  kind: string;
  is_open: boolean;
  terms: FixedIncomeTerms;
  accrual: {
    principal: number;
    value: number;
    interest: number;
    factor: number;
    yield_percent: number;
    business_days: number;
    index_code: string;
    through: string;
    stale: boolean;
  } | null;
  /** Rate solved from what the paper actually paid (redeemed papers only). */
  implied: {
    percent_of_index: number;
    index_code: string;
    invested: number;
    proceeds: number;
    total_return_percent: number;
    start: string;
    end: string;
  } | null;
}

export interface DividendReport {
  granularity: "month" | "quarter" | "year";
  /**
   * One entry per period. The two breakdowns are nested and independent: the
   * chart picks an axis without another request, and the namespaces cannot
   * collide as kinds and operations are added.
   */
  series: {
    period: string;
    total: number;
    cumulative: number;
    /** Keyed by `OperationType`: dividend, JCP, yield, interest. */
    types: Record<string, number>;
    /** Keyed by `AssetKind`: ações, stocks, FIIs, REITs, renda fixa… */
    kinds: Record<string, number>;
    /** The same two axes with withholding taken off. */
    types_net: Record<string, number>;
    kinds_net: Record<string, number>;
    /** Tax withheld at source in the period, net of later refunds. */
    tax: number;
    /** What actually reached the account: `total - tax`. */
    net: number;
  }[];
  by_kind: { kind: string; total: number; tax: number; net: number; share: number }[];
  by_type: { op_type: string; total: number; tax: number; net: number; share: number }[];

  by_asset: {
    ticker: string;
    tax: number;
    net: number;
    yield_on_cost_net: number | null;
    name: string;
    kind: string;
    total: number;
    payments: number;
    first: string;
    last: string;
    cost_basis: number;
    /** Income received divided by the cost still invested — null once sold. */
    yield_on_cost: number | null;
    share: number;
  }[];
  totals: {
    /** Gross — what was declared. Every breakdown above sums to it. */
    all_time: number;
    this_year: number;
    last_12m: number;
    /** Withheld at source, net of later refunds. */
    tax: number;
    tax_this_year: number;
    tax_last_12m: number;
    /** What reached the account: gross minus withholding. */
    net: number;
    net_this_year: number;
    net_last_12m: number;
    /** Brokerage paid on trades. Reported, never netted off income. */
    trading_costs: number;
    payments: number;
    assets: number;
    best_month: string | null;
    best_month_amount: number;
    average_month: number;
    monthly_average_12m: number;
    net_monthly_average_12m: number;
    yield_on_cost: number;
    yield_on_cost_net: number;
    cost_basis: number;
  };
}

export interface IncomePayment {
  date: string;
  ticker: string;
  name: string;
  kind: string;
  op_type: string;
  /** Gross payment. */
  amount: number;
  /** Withheld from this payment, matched by asset and date. */
  tax: number;
  /** What reached the account. */
  net: number;
  quantity: number;
  unit_price: number;
}

/** A dividend declared to B3 but not yet paid, sized by the current position. */
export interface UpcomingDividend {
  ticker: string;
  name: string;
  kind: string;
  /** B3's own label: "DIVIDENDO", "JCP", "RENDIMENTO"… */
  label: string | null;
  payment_date: string | null;
  record_date: string | null;
  /** Declared, amount known, payment date still "a definir". */
  date_pending: boolean;
  /** Per share. */
  rate: number;
  quantity: number;
  /** rate × current quantity — an estimate, not a promise. */
  total: number;
}

export interface UpcomingDividends {
  items: UpcomingDividend[];
  /** Held tickers with no cached schedule yet (refresh fills them). */
  missing: string[];
  updated_at: string | null;
}

/** A saved AI-analyst conversation about one asset. */
export interface AiChatSummary {
  id: number;
  /** Null: a conversation about the portfolio as a whole. */
  ticker: string | null;
  title: string;
  message_count: number;
  created_at: string;
  updated_at: string;
}

export interface AiChatDetail extends AiChatSummary {
  messages: { role: "user" | "assistant"; content: string }[];
}

export interface Succession {
  id: number;
  from_ticker: string;
  from_name: string;
  to_ticker: string | null;
  to_name: string | null;
  effective_date: string;
  cash_amount: number;
  note: string | null;
  source: string;
}

export interface SuccessionCandidate {
  ticker: string;
  name: string;
  date: string;
  movement: string;
  quantity: number;
  exact_quantity_match: boolean;
}

/** A corporate event the AI scan proposed, stored until accepted/declined. */
export interface CorporateAiSuggestion {
  id: number;
  from_ticker: string;
  to_ticker: string | null;
  effective_date: string;
  cash_amount: number;
  event_type: "rename" | "merger" | "delisting" | "spinoff" | "other";
  rationale: string | null;
  source: string | null;
  status: string;
  provider: string;
  model: string;
  created_at: string;
}

export interface SuccessionSuggestion {
  ticker: string;
  name: string;
  kind: string;
  quantity: number;
  cost_basis: number;
  last_trade: string;
  candidates: SuccessionCandidate[];
}

export interface TreasuryItem {
  ticker: string;
  name: string;
  is_open: boolean;
  quantity: number;
  average_price: number;
  cost_basis: number;
  value: number;
  unrealized: number;
  unrealized_pct: number;
  price_date: string | null;
  /** What an early redemption pays today — the basis for `value`. */
  sell_price: number | null;
  /** What the same paper costs to buy today. */
  buy_price: number | null;
  sell_rate: number | null;
  buy_rate: number | null;
  spread_pct: number | null;
  /** Amount-weighted yield the position was bought at, read back from the feed. */
  contracted_rate: number | null;
  stale: boolean;
}

/**
 * A bank balance the user keeps by hand. Modelled server-side as a fixed income
 * asset whose unit is one real, so it counts in the net worth and accrues
 * against the CDI like any other paper — see `app/portfolio/accounts.py`.
 */
export interface CashAccount {
  ticker: string;
  name: string;
  notes: string | null;
  index_code: string;
  percent_of_index: number;
  /** First movement on file; `null` for an account with no entries yet. */
  since: string | null;
  /** Deposits minus withdrawals — the money actually put in. */
  principal: number;
  /** What it is worth today: principal plus everything it has earned. */
  balance: number;
  interest: number;
  yield_percent: number;
  business_days: number;
  /** True when the index series has not caught up with today. */
  stale: boolean;
  entries: CashEntry[];
}

export interface CashEntry {
  id: number;
  date: string;
  kind: "deposit" | "withdrawal";
  amount: number;
}

export interface IndexStatus {
  code: string;
  start: string;
  end: string;
  points: number;
  checked_at: string;
}

export interface AiProviderInfo {
  id: string;
  label: string;
  default_model: string;
  /** Curated suggestions — the model field remains free text. */
  models: string[];
  key_setting: string;
  key_hint: string;
  key_configured: boolean;
}

export interface AppSettings {
  values: Record<string, unknown>;
  /** Write-only API keys: `{key: configured?}` — values never come back. */
  secrets: Record<string, boolean>;
  ai: { active_provider: string; active_model: string; providers: AiProviderInfo[] };
  providers: string[];
  provider_active: string;
  known_movements: string[];
  env: Record<string, unknown>;
}

// --- cloud backup ---------------------------------------------------------
/** State of the manual "Enviar agora" job (poll status while active). */
export interface CloudBackupJob {
  active: boolean;
  id: string | null;
  kind: string | null;
  status: string | null;
  error: string | null;
  result: Record<string, unknown> | null;
  finished_at: string | null;
}

/** Outcome of a provider's most recent upload — nightly or manual. */
export interface CloudProviderLast {
  state: "ok" | "error";
  file: string | null;
  size: number | null;
  at: string | null;
  message: string | null;
  rotated?: number;
}

export interface CloudProviderStatus {
  name: string;
  label: string;
  /** App credentials (client id / app key) present. */
  configured: boolean;
  /** Authorization completed — included in the nightly sync. */
  connected: boolean;
  last: CloudProviderLast | null;
}

export interface CloudBackupStatus {
  providers: CloudProviderStatus[];
  last_run_at: string | null;
  encryption: { passphrase_set: boolean };
  /** When the nightly local dump + cloud sync runs (HH:MM). */
  backup_time: string;
  job: CloudBackupJob;
}

export interface RemoteBackupItem {
  id: string;
  name: string;
  size: number | null;
  modified_at: string | null;
  encrypted: boolean;
}

export interface RemoteBackupsResponse {
  providers: Record<string, { items?: RemoteBackupItem[]; error?: string }>;
}

// --- AI wallet (Carteira IA) ---------------------------------------------
export interface AiWalletSummary {
  id: number;
  name: string;
  provider: string;
  provider_label: string;
  model: string;
  created_at: string;
  value: number;
  invested: number;
  return_pct: number | null;
  categories_active: number;
  pending_suggestions: number;
}

export interface AiWalletPositionRow {
  id: number;
  ticker: string;
  name: string;
  category: string;
  currency: string;
  quantity: number;
  avg_price: number;
  avg_fx: number | null;
  cost_brl: number;
  /** Money reserved for a deferred buy (asset had no quote yet). */
  pending_brl: number;
  market_value_brl: number;
  pnl_brl: number;
  pnl_pct: number;
  weight_pct: number;
  priced: boolean;
  is_fixed_income: boolean;
  fi_label: string | null;
  rationale: string | null;
}

export interface AiWalletCategoryBlock {
  category: string;
  label: string;
  active: boolean;
  pending_suggestions: number;
  budget?: number;
  cash?: number;
  generated_at?: string | null;
  /** The model's own strategy for this category, written at generation. */
  thesis?: string | null;
  value?: number;
  positions?: AiWalletPositionRow[];
}

export interface AiWalletDetail {
  id: number;
  name: string;
  provider: string;
  provider_label: string;
  model: string;
  created_at: string;
  /** Whether the pinned provider/model has server-side web search. */
  web_search: boolean;
  key_configured: boolean;
  totals: { value: number; invested: number; cash: number; return_pct: number | null; unpriced: string[] };
  categories: AiWalletCategoryBlock[];
}

export type AiSuggestionAction = "buy_new" | "increase" | "reduce" | "sell_all" | "rebalance";

export interface AiWalletSuggestion {
  id: number;
  category: string;
  batch_id: string;
  action: AiSuggestionAction;
  ticker: string | null;
  name: string;
  amount_brl: number | null;
  to_ticker: string | null;
  to_category: string | null;
  rationale: string | null;
  /** pending | accepted | declined | superseded | failed */
  status: string;
  detail: string | null;
  created_at: string;
}

export interface AiWalletEvent {
  id: number;
  at: string;
  category: string | null;
  action: string;
  provider: string | null;
  model: string | null;
  detail: Record<string, unknown>;
}

export interface AiWalletCompare {
  series: {
    key: string;
    label: string;
    benchmark: boolean;
    wallet_id?: number;
    provider?: string;
    model?: string;
  }[];
  rows: ({ date: string } & Record<string, number | string>)[];
}

export type AiWalletRange = "1m" | "3m" | "6m" | "1y" | "max";

/** State of a background AI job (poll while active) — shared wire shape. */
export interface AiWalletJob {
  active: boolean;
  id: string | null;
  kind: string | null;
  status: string | null;
  error: string | null;
  result: Record<string, unknown> | null;
  finished_at: string | null;
}

// --- aporte inteligente ---------------------------------------------------
export interface SmartInvestCategory {
  kind: string;
  label: string;
  count: number;
  value: number;
}

/** One line of the AI's split. Money figures arrive as strings (Decimal wire). */
export interface SmartInvestAllocation {
  ticker: string;
  name: string;
  kind: string;
  label: string;
  amount: string;
  approx_quantity: string | null;
  current_price: string | null;
  price_currency: string;
  rationale: string;
}

export interface SmartInvestResult {
  amount: string;
  currency: "BRL" | "USD";
  kinds: string[];
  categories: string[];
  strategy: string | null;
  allocations: SmartInvestAllocation[];
  leftover: string;
  skipped: string[];
  used_search: boolean;
  provider: string;
  provider_label: string;
  model: string;
  generated_at: string;
}

/** A persisted analysis — the durable copy of a finished run. */
export type SmartInvestRun = SmartInvestResult & { id: number; created_at: string };


// --- universo de ativos ---------------------------------------------------
export interface UniverseRow {
  ticker: string;
  name: string;
  market: string;
  kind: string;
  currency: string;
  sector: string | null;
  segmento_b3: string | null;
  segmento_do_fundo: string | null;
  status: string;
  /** B3 index codes this paper belongs to: ["IBOV", "IDIV"]. */
  indices: string[];
  preco: number | null;
  preco_em: string | null;
  valor_de_mercado: number | null;
  liquidez_media_diaria: number | null;
  variacao_12m_pct: number | null;
  volatilidade_12m_pct: number | null;
  maxima_52s: number | null;
  minima_52s: number | null;
  dias_negociados_12m: number | null;
  p_l: number | null;
  p_vp: number | null;
  roe_pct: number | null;
  margem_liquida_pct: number | null;
  margem_bruta_pct: number | null;
  crescimento_receita_pct: number | null;
  crescimento_lucro_pct: number | null;
  divida_sobre_patrimonio: number | null;
  dividend_yield_pct: number | null;
  payout_pct: number | null;
  valor_patrimonial_por_acao: number | null;
  patrimonio_do_fundo: number | null;
  /** Trailing revenue, and the size ranking for US rows (which have no cap). */
  receita: number | null;
  lucro_liquido: number | null;
  exercicio_dos_fundamentos: string | null;
  fundamentos_em: string | null;
  observacao: string | null;
  /** Open position in your real portfolio. */
  na_carteira: boolean;
  /** Held by one of the AI wallets — virtual money, not yours. */
  na_carteira_ia: boolean;
  na_watchlist: boolean;
  origem_dos_precos: string | null;
  origem_dos_fundamentos: string | null;
}

export interface UniversePage {
  items: UniverseRow[];
  total: number;
  dropped_for_missing_data: number;
  stalest_fundamentals_at: string | null;
  fields: { key: string; sortable: boolean }[];
}

export interface UniverseCoverage {
  total: number;
  by_market: Record<string, number>;
  by_kind: Record<string, number>;
  with_fundamentals: number;
  prices_updated_at?: string | null;
  fundamentals_updated_at?: string | null;
}

/** One stage of the ingest, as the jobs tab lists it. */
export interface UniverseJob {
  name: string;
  label: string;
  status: "pending" | "running" | "done" | "skipped" | "failed";
  rows: number;
  /** How long it took, once finished. */
  seconds: number | null;
  /** How long it took the last time it succeeded — the basis for the estimate. */
  expected_seconds: number | null;
  started_at: string | null;
  finished_at: string | null;
  processed: number | null;
  total: number | null;
  message: string | null;
}

export interface UniverseRun {
  run_id: string | null;
  state: string;
  message: string | null;
  markets: string[];
  started_at: string | null;
  finished_at: string | null;
  seconds: number | null;
  rows: number;
  stage_rows: Record<string, number>;
  warnings: string[];
}

/** The ingest run block. `active` is already stale-adjusted by the backend. */
export interface UniverseStatus {
  run_id: string | null;
  state: "idle" | "running" | "paused" | "cancelled" | "error" | "done";
  stage: string | null;
  stage_label: string | null;
  stage_index: number;
  stage_count: number;
  message: string | null;
  processed: number;
  total: number;
  started_at: string | null;
  finished_at: string | null;
  active: boolean;
  stale: boolean;
  warnings: string[];
  stages_done: string[];
  stage_rows: Record<string, number>;
  markets: string[];
  settings: {
    enabled: boolean;
    markets: string[];
    history_years: number;
    sec_user_agent?: string;
  };
  stages: { name: string; label: string }[];
  coverage: UniverseCoverage;
  jobs: UniverseJob[];
  history: UniverseRun[];
  /** Seconds left, or null when nothing has been measured yet. */
  eta_seconds: number | null;
  elapsed_seconds: number | null;
}

export interface PortfolioFit {
  enabled: boolean;
  holdings?: {
    ticker: string;
    name: string;
    setor: string | null;
    peso_pct: number | null;
    p_l: number | null;
    p_vp: number | null;
    roe_pct: number | null;
    dividend_yield_pct: number | null;
    percentis_no_setor: Record<string, number | null>;
    exercicio_dos_fundamentos: string | null;
  }[];
  sectors?: { setor: string; meu_peso_pct: number | null; peso_de_mercado_pct: number | null; diferenca_pp: number | null }[];
  gaps?: string[];
  outside?: string[];
  sem_setor?: string[];
  coberto_pct?: number | null;
}

export interface UniverseQuery {
  [key: string]: unknown;
  market?: string;
  kind?: string[];
  sector?: string;
  index?: string;
  text?: string;
  order_by?: string;
  descending?: boolean;
  only_active?: boolean;
  limit?: number;
  offset?: number;
  min_dy?: number;
  max_pe?: number;
  max_pb?: number;
  min_roe?: number;
  min_volume?: number;
}

// --- endpoints -----------------------------------------------------------
export const api = {
  overview: () => request<Overview>("/portfolio/overview"),
  notifications: () => request<{ items: AppNotification[]; count: number }>("/notifications"),
  positions: (includeClosed = false) =>
    request<PositionRow[]>(`/portfolio/positions${query({ include_closed: includeClosed })}`),
  allocation: (groupBy: "asset" | "kind" | "broker" | "currency" = "asset") =>
    request<AllocationSlice[]>(`/portfolio/allocation${query({ group_by: groupBy })}`),
  history: (range = "max", granularity = "auto") =>
    request<HistoryPoint[]>(`/portfolio/history${query({ range, granularity })}`),
  profitHistory: (range: ProfitRange = "1y", groupBy: "total" | "kind" = "total") =>
    request<ProfitPoint[]>(`/portfolio/profit-history${query({ range, group_by: groupBy })}`),
  performers: (window: PerformanceWindow = "total", limit = 6) =>
    request<PerformanceReport>(`/reports/performers${query({ window, limit })}`),
  income: (granularity: "month" | "year" = "month") =>
    request<IncomePoint[]>(`/portfolio/income${query({ granularity })}`),
  contributions: (granularity: "month" | "year" = "month") =>
    request<ContributionPoint[]>(`/portfolio/contributions${query({ granularity })}`),
  dividends: (granularity: "month" | "quarter" | "year" = "month") =>
    request<DividendReport>(`/portfolio/dividends${query({ granularity })}`),
  dividendBreakdown: (period: string, granularity: "month" | "quarter" | "year" = "month") =>
    request<{
      period: string;
      granularity: string;
      total: number;
      groups: {
        kind: string;
        total: number;
        assets: { ticker: string; name: string; total: number; payments: number }[];
      }[];
    }>(`/portfolio/dividends/breakdown${query({ period, granularity })}`),
  dividendCalendar: (limit = 40) => request<IncomePayment[]>(`/dividends/calendar${query({ limit })}`),
  upcomingDividends: (refresh = false) =>
    request<UpcomingDividends>(`/dividends/upcoming${query({ refresh: refresh || undefined })}`),
  aiChats: () => request<AiChatSummary[]>("/ai/chats"),
  aiChatDetail: (id: number) => request<AiChatDetail>(`/ai/chats/${id}`),
  deleteAiChat: (id: number) => request<{ deleted: number }>(`/ai/chats/${id}`, { method: "DELETE" }),
  monthlyReturns: () => request<MonthlyReturn[]>("/portfolio/monthly-returns"),
  portfolioWarnings: () => request<{ ticker: string; name: string; message: string }[]>("/portfolio/warnings"),

  assets: (includeClosed = true) => request<PositionRow[]>(`/assets${query({ include_closed: includeClosed })}`),
  asset: (ticker: string) => request<AssetDetail>(`/assets/${encodeURIComponent(ticker)}`),
  assetPriceHistory: (ticker: string) =>
    request<{ date: string; close: number }[]>(`/assets/${encodeURIComponent(ticker)}/price-history`),
  assetFundamentals: (ticker: string, refresh = false) =>
    request<FundamentalsResponse>(
      `/assets/${encodeURIComponent(ticker)}/fundamentals${query({ refresh: refresh || undefined })}`,
    ),
  updateAsset: (ticker: string, payload: Record<string, unknown>) =>
    request(`/assets/${encodeURIComponent(ticker)}`, { method: "PATCH", body: JSON.stringify(payload) }),
  /**
   * State the balance the venue actually reports. Appends the difference as a
   * movement rather than overwriting the position, so it stays derived.
   */
  reconcileAsset: (ticker: string, quantity: number, note?: string) =>
    request<{
      ticker: string;
      previous: number;
      quantity: number;
      difference: number;
      applied: boolean;
      detail?: string;
    }>(`/assets/${encodeURIComponent(ticker)}/reconcile`, {
      method: "POST",
      body: JSON.stringify({ quantity, note }),
    }),

  transactions: (params: Record<string, unknown>) =>
    request<{ total: number; page: number; page_size: number; pages: number; items: TransactionRow[] }>(
      `/transactions${query(params)}`,
    ),
  transactionFilters: () =>
    request<{ op_types: string[]; brokers: string[]; date_range: { start: string; end: string } }>(
      "/transactions/filters",
    ),
  transactionOperations: () => request<ManualOperation[]>("/transactions/operations"),
  createTransaction: (payload: {
    operation: string;
    ticker: string;
    date: string;
    quantity?: number;
    unit_price?: number;
    amount?: number | null;
    fees?: number;
    taxes?: number;
    name?: string;
    /** Only for a ticker nobody has traded: what the market said it is. */
    kind?: string;
    currency?: string;
    broker?: string | null;
    notes?: string | null;
  }) => request<{ id: number; ticker: string; op_type: string }>("/transactions", {
    method: "POST",
    body: JSON.stringify(payload),
  }),
  deleteTransaction: (id: number) => request(`/transactions/${id}`, { method: "DELETE" }),
  exportUrl: (params: Record<string, unknown>) => `${BASE}/transactions/export${query(params)}`,

  /** Whole-database export (.gumbinvest) — for moving a history to another instance. */
  fullExportUrl: () => `${BASE}/imports/export`,

  // Backup na nuvem — conexões, envio manual (job com polling) e restauração.
  cloudBackupStatus: () => request<CloudBackupStatus>("/cloud-backup/status"),
  gdriveDeviceStart: () =>
    request<{ verification_url: string; user_code: string; expires_in: number; interval: number }>(
      "/cloud-backup/gdrive/device/start",
      { method: "POST" },
    ),
  gdriveDevicePoll: () =>
    request<{ status: "pending" | "connected"; interval?: number }>("/cloud-backup/gdrive/device/poll", {
      method: "POST",
    }),
  dropboxAuthorize: () =>
    request<{ authorize_url: string }>("/cloud-backup/dropbox/authorize", { method: "POST" }),
  dropboxComplete: (code: string) =>
    request<{ status: string }>("/cloud-backup/dropbox/complete", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),
  cloudDisconnect: (provider: string) =>
    request<{ status: string }>(`/cloud-backup/${encodeURIComponent(provider)}/disconnect`, {
      method: "POST",
    }),
  cloudBackupSend: () => request<CloudBackupJob>("/cloud-backup/send", { method: "POST" }),
  cloudBackups: () => request<RemoteBackupsResponse>("/cloud-backup/backups"),
  cloudRestore: (payload: { provider: string; backup_id: string; name?: string; passphrase?: string }) =>
    request<{ status: string; rows_imported: number; rows_total: number }>("/cloud-backup/restore", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  /** Live model catalog from the provider (needs its key); curated fallback otherwise. */
  aiModels: (provider: string) =>
    request<{ models: string[]; live: boolean }>(`/ai/models${query({ provider })}`),

  // Carteira IA — generate/suggest run as backend jobs; poll aiWalletJob.
  aiWallets: () => request<AiWalletSummary[]>("/ai-wallets"),
  createAiWallet: (payload: { name: string; provider?: string; model?: string }) =>
    request<AiWalletSummary>("/ai-wallets", { method: "POST", body: JSON.stringify(payload) }),
  aiWalletJob: (id: number, category: string) =>
    request<AiWalletJob>(`/ai-wallets/${id}/categories/${encodeURIComponent(category)}/job`),
  generateAiWalletCategory: (id: number, category: string) =>
    request<AiWalletJob>(`/ai-wallets/${id}/categories/${encodeURIComponent(category)}/generate`, {
      method: "POST",
    }),
  suggestAiWalletCategory: (id: number, category: string) =>
    request<AiWalletJob>(`/ai-wallets/${id}/categories/${encodeURIComponent(category)}/suggest`, {
      method: "POST",
    }),
  deleteAiWallet: (id: number) => request<{ deleted: boolean }>(`/ai-wallets/${id}`, { method: "DELETE" }),
  aiWalletDetail: (id: number) => request<AiWalletDetail>(`/ai-wallets/${id}`),
  aiWalletEvents: (id: number, page = 1, category?: string) =>
    request<{ total: number; page: number; page_size: number; items: AiWalletEvent[] }>(
      `/ai-wallets/${id}/events${query({ page, category })}`,
    ),
  aiWalletSuggestions: (id: number, category?: string) =>
    request<AiWalletSuggestion[]>(`/ai-wallets/${id}/suggestions${query({ category })}`),
  acceptAiSuggestion: (id: number, suggestionId: number) =>
    request<{ applied: Record<string, unknown>; suggestion: AiWalletSuggestion }>(
      `/ai-wallets/${id}/suggestions/${suggestionId}/accept`,
      { method: "POST" },
    ),
  declineAiSuggestion: (id: number, suggestionId: number) =>
    request<{ suggestion: AiWalletSuggestion }>(`/ai-wallets/${id}/suggestions/${suggestionId}/decline`, {
      method: "POST",
    }),
  aiWalletCompare: (range: AiWalletRange = "max") =>
    request<AiWalletCompare>(`/ai-wallets/compare${query({ range })}`),

  // Aporte inteligente — the analysis runs as a backend job; poll smartInvestJob.
  smartInvestOptions: () =>
    request<{ categories: SmartInvestCategory[] }>("/smart-invest/options"),
  smartInvestJob: () => request<AiWalletJob>("/smart-invest"),
  startSmartInvest: (payload: { amount: number; currency: "BRL" | "USD"; kinds: string[] }) =>
    request<AiWalletJob>("/smart-invest", { method: "POST", body: JSON.stringify(payload) }),
  smartInvestHistory: () => request<SmartInvestRun[]>("/smart-invest/history"),
  deleteSmartInvestRun: (id: number) =>
    request<{ deleted: boolean }>(`/smart-invest/history/${id}`, { method: "DELETE" }),

  imports: (page = 1, pageSize = 5) =>
    request<{ total: number; page: number; page_size: number; pages: number; items: ImportBatch[] }>(
      `/imports${query({ page, page_size: pageSize })}`,
    ),
  importDetail: (id: number) => request<ImportBatch>(`/imports/${id}`),
  coverage: () =>
    request<{ accounts: CoverageAccount[]; formats: { format: string; broker: string }[] }>(
      "/imports/coverage",
    ),
  /** Uploads a B3 CSV or a broker statement PDF — the backend tells them apart. */
  upload: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{
      batch_id: number;
      rows_total: number;
      rows_imported: number;
      rows_duplicate: number;
      rows_failed: number;
      summary: ImportBatch["summary"];
    }>("/imports", { method: "POST", body: form });
  },

  reports: () =>
    request<{
      overview: Overview;
      annual: { year: string; bought: number; sold: number; income: number; transactions: number; market_value: number }[];
      income_by_year: IncomePoint[];
      performers: { best: PositionRow[]; worst: PositionRow[] };
      allocation: { by_kind: AllocationSlice[]; by_broker: AllocationSlice[] };
      totals: { income: number; realized: number; unrealized: number };
    }>("/reports/summary"),

  search: (q: string) =>
    request<{
      assets: { ticker: string; name: string; kind: string }[];
      transactions: { id: number; date: string; ticker: string; op_type: string; movement: string; gross_amount: number }[];
    }>(`/search${query({ q })}`),
  /** Tickers the market knows but the portfolio never traded (B3 + US). */
  searchMarket: (q: string) =>
    request<{
      items: { ticker: string; name: string; kind: string; currency: string; exchange: string }[];
    }>(`/search/market${query({ q })}`),

  fixedIncome: () =>
    request<{
      items: FixedIncomeItem[];
      totals: { principal: number; value: number; interest: number };
      indices: IndexStatus[];
      available_indices: string[];
    }>("/fixed-income"),
  updateFixedIncome: (ticker: string, terms: FixedIncomeTerms) =>
    request<FixedIncomeItem>(`/fixed-income/${encodeURIComponent(ticker)}`, {
      method: "PUT",
      body: JSON.stringify(terms),
    }),
  syncIndices: () => request<Record<string, unknown>>("/fixed-income/indices/sync", { method: "POST" }),

  cashAccounts: () =>
    request<{ items: CashAccount[]; totals: { principal: number; balance: number; interest: number } }>(
      "/fixed-income/accounts",
    ),
  createCashAccount: (payload: {
    name: string;
    index_code?: string;
    percent_of_index?: number;
    opening_amount?: number | null;
    opening_date?: string | null;
    notes?: string | null;
  }) => request<CashAccount>("/fixed-income/accounts", { method: "POST", body: JSON.stringify(payload) }),
  updateCashAccount: (ticker: string, payload: Record<string, unknown>) =>
    request<CashAccount>(`/fixed-income/accounts/${encodeURIComponent(ticker)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteCashAccount: (ticker: string) =>
    request(`/fixed-income/accounts/${encodeURIComponent(ticker)}`, { method: "DELETE" }),
  addCashEntry: (ticker: string, payload: { amount: number; date: string; kind: "deposit" | "withdrawal" }) =>
    request<CashAccount>(`/fixed-income/accounts/${encodeURIComponent(ticker)}/entries`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deleteCashEntry: (ticker: string, entryId: number) =>
    request<CashAccount>(`/fixed-income/accounts/${encodeURIComponent(ticker)}/entries/${entryId}`, {
      method: "DELETE",
    }),

  treasury: () =>
    request<{
      items: TreasuryItem[];
      totals: { cost_basis: number; value: number; unrealized: number };
      coverage: { ticker: string; start: string; end: string; points: number }[];
      source: string;
    }>("/treasury"),
  syncTreasury: () => request<Record<string, unknown>>("/treasury/sync", { method: "POST" }),

  corporateActions: () =>
    request<{ items: Succession[]; suggestions: SuccessionSuggestion[] }>("/corporate-actions"),
  createSuccession: (payload: {
    from_ticker: string;
    to_ticker: string | null;
    effective_date: string;
    cash_amount?: number;
    note?: string | null;
    source?: string;
  }) => request<Succession>("/corporate-actions", { method: "POST", body: JSON.stringify(payload) }),
  deleteSuccession: (id: number) => request(`/corporate-actions/${id}`, { method: "DELETE" }),
  // AI event scan: background job (poll status) + stored accept/decline proposals.
  corporateAiScan: () => request<AiWalletJob>("/corporate-actions/ai-scan"),
  startCorporateAiScan: () => request<AiWalletJob>("/corporate-actions/ai-scan", { method: "POST" }),
  corporateAiSuggestions: () => request<CorporateAiSuggestion[]>("/corporate-actions/ai-suggestions"),
  acceptCorporateAiSuggestion: (id: number) =>
    request<{ succession: Succession; suggestion: CorporateAiSuggestion }>(
      `/corporate-actions/ai-suggestions/${id}/accept`,
      { method: "POST" },
    ),
  declineCorporateAiSuggestion: (id: number) =>
    request<{ suggestion: CorporateAiSuggestion }>(`/corporate-actions/ai-suggestions/${id}/decline`, {
      method: "POST",
    }),

  settings: () => request<AppSettings>("/settings"),
  updateSettings: (values: Record<string, unknown>) =>
    request<AppSettings>("/settings", { method: "PUT", body: JSON.stringify({ values }) }),

  marketStatus: () =>
    request<{
      provider: string;
      last_update: string | null;
      quotes: { ticker: string; price: number; change_percent: number | null; source: string; fetched_at: string }[];
      /** Rate series held, one entry per pair. */
      fx: {
        pair: string;
        base: string;
        quote: string;
        start: string;
        end: string;
        points: number;
        /** The most recent published rate — `null` if the series is empty. */
        rate: number | null;
        /**
         * True for a real currency (PTAX). False for a coin's daily close,
         * which is stored the same way only so trades priced in a coin can
         * reach the base currency — not something to show as an exchange rate.
         */
        is_currency: boolean;
      }[];
      /**
       * Headline coin prices, already converted to the portfolio's currency.
       * A price, not a rate — `fx` above is the other thing.
       */
      benchmarks: {
        symbol: string;
        name: string;
        price: number;
        currency: string;
        /** `null` when no exchange rate is on file yet to convert with. */
        price_base: number | null;
        base_currency: string;
        change_percent: number | null;
        fetched_at: string;
      }[];
      /**
       * Index levels — the Ibovespa's close in points, and the move against the
       * previous stored close. Points, never money: no currency belongs in
       * front of these.
       */
      indices: {
        code: string;
        label: string;
        value: number;
        change_percent: number | null;
        /** Session the close belongs to, which may not be today. */
        date: string;
      }[];
    }>("/market/status"),
  refreshQuotes: () => request<Record<string, unknown>>("/market/refresh", { method: "POST" }),
  syncFx: () => request<Record<string, unknown>>("/market/fx/sync", { method: "POST" }),
  backfillHistory: () => request<Record<string, unknown>>("/market/backfill", { method: "POST" }),

  investors: () =>
    request<{ slug: string; manager: string; fund: string; description: string }[]>("/investors"),
  investorWallet: (slug: string) =>
    request<InvestorWallet>(`/investors/${encodeURIComponent(slug)}`),

  universeStatus: () => request<UniverseStatus>("/universe/status"),
  startUniverseIngest: (markets?: string[]) =>
    request<UniverseStatus>("/universe/ingest", {
      method: "POST",
      body: JSON.stringify({ markets: markets ?? null }),
    }),
  cancelUniverseIngest: () =>
    request<UniverseStatus>("/universe/ingest/cancel", { method: "POST" }),
  clearUniverse: () =>
    request<UniverseStatus & { removed: number }>("/universe", { method: "DELETE" }),
  universe: (params: UniverseQuery) => request<UniversePage>(`/universe${query(params)}`),
  universeSectors: () => request<{ sector: string; count: number }[]>("/universe/sectors"),
  portfolioFit: () => request<PortfolioFit>("/universe/fit"),

  watchlist: () =>
    request<{ id: number; ticker: string; note: string | null; target_price: number | null; price: number | null; change_percent: number | null }[]>(
      "/watchlist",
    ),
  addWatchlist: (ticker: string, note?: string) =>
    request("/watchlist", { method: "POST", body: JSON.stringify({ ticker, note }) }),
  removeWatchlist: (id: number) => request(`/watchlist/${id}`, { method: "DELETE" }),
};
