/** Locale-aware formatting helpers (pt-BR / BRL by default). */

let locale = "pt-BR";
let currency = "BRL";

export function configureFormatting(nextLocale?: string, nextCurrency?: string) {
  if (nextLocale) locale = nextLocale;
  if (nextCurrency) currency = nextCurrency;
}

const num = (value: unknown): number => {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

/**
 * Formats an amount as money.
 *
 * `currency` overrides the portfolio's own — a US holding is shown in the
 * dollars it was actually bought with, so the asset page can label its figures
 * honestly instead of printing dollars with a "R$" in front of them.
 */
export function money(
  value: unknown,
  options: { compact?: boolean; decimals?: number; currency?: string } = {},
): string {
  const amount = num(value);
  const code = options.currency ?? currency;
  if (options.compact && Math.abs(amount) >= 1000) {
    return new Intl.NumberFormat(locale, {
      style: "currency",
      currency: code,
      notation: "compact",
      maximumFractionDigits: 1,
    }).format(amount);
  }
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency: code,
    minimumFractionDigits: options.decimals ?? 2,
    maximumFractionDigits: options.decimals ?? 2,
  }).format(amount);
}

/** The portfolio's own currency, for labelling converted figures. */
export const baseCurrency = () => currency;

export function decimal(value: unknown, decimals = 2): string {
  return new Intl.NumberFormat(locale, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(num(value));
}

/** Quantities: hide decimals when the amount is whole (B3 mixes both). */
export function quantity(value: unknown): string {
  const amount = num(value);
  const decimals = Number.isInteger(amount) ? 0 : Math.abs(amount) < 1 ? 6 : 2;
  return new Intl.NumberFormat(locale, { minimumFractionDigits: 0, maximumFractionDigits: decimals }).format(amount);
}

export function percent(value: unknown, decimals = 2, withSign = false): string {
  const amount = num(value);
  const text = new Intl.NumberFormat(locale, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(amount);
  return `${withSign && amount > 0 ? "+" : ""}${text}%`;
}

export function signedMoney(value: unknown, options?: { compact?: boolean; currency?: string }): string {
  const amount = num(value);
  return `${amount > 0 ? "+" : ""}${money(amount, options)}`;
}

/**
 * How a currency is named when its rate is shown on its own.
 *
 * Only the currencies the portfolio actually converts through need a name; a
 * pair with no entry falls back to "1 XYZ", which reads correctly for anything
 * and is what a coin's rate should say.
 */
const CURRENCY_LABELS: Record<string, string> = {
  USD: "Dólar hoje",
  EUR: "Euro hoje",
  GBP: "Libra hoje",
};

export const currencyLabel = (code: string) => CURRENCY_LABELS[code] ?? `1 ${code}`;

/** "USD 5,4210" — an exchange rate, which is a ratio and not an amount. */
export function fxRate(value: unknown, from: string, to = currency): string {
  return `1 ${from} = ${new Intl.NumberFormat(locale, {
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  }).format(num(value))} ${to}`;
}

export function shortDate(value: string | Date | null | undefined): string {
  if (!value) return "-";
  const date = typeof value === "string" ? new Date(`${value.length <= 10 ? `${value}T00:00:00` : value}`) : value;
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat(locale, { day: "2-digit", month: "2-digit", year: "numeric" }).format(date);
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat(locale, { dateStyle: "short", timeStyle: "short" }).format(date);
}

/** "2024-03" -> "mar/2024"; "2024" stays as is. */
export function periodLabel(period: string): string {
  if (/^\d{4}$/.test(period)) return period;
  const [year, month] = period.split("-");
  if (!month) return period;
  if (month.startsWith("Q")) return `${month} ${year.slice(2)}`;
  const date = new Date(Number(year), Number(month) - 1, 1);
  return new Intl.DateTimeFormat(locale, { month: "short", year: "2-digit" }).format(date).replace(".", "");
}

export function toneOf(value: unknown): "positive" | "negative" | "neutral" {
  const amount = num(value);
  if (amount > 0) return "positive";
  if (amount < 0) return "negative";
  return "neutral";
}

export const OP_LABELS: Record<string, string> = {
  BUY: "Compra",
  SELL: "Venda",
  DIVIDEND: "Dividendo",
  // "JCP" and "Juros" both read as interest in Portuguese but are unrelated:
  // JCP is a company distributing profit (taxed 15 % at source), INTEREST is a
  // fixed income coupon. The longer label keeps them apart on sight.
  JCP: "JCP",
  YIELD: "Rendimento",
  INTEREST: "Juros de renda fixa",
  AMORTIZATION: "Amortização",
  SPLIT: "Desdobramento",
  REVERSE_SPLIT: "Grupamento",
  BONUS: "Bonificação",
  SUBSCRIPTION: "Subscrição",
  MERGER: "Incorporação",
  FRACTION: "Fração",
  TRANSFER_IN: "Transferência (entrada)",
  TRANSFER_OUT: "Transferência (saída)",
  REDEMPTION: "Resgate",
  POSITION_UPDATE: "Atualização de posição",
  // Paid in coin rather than in cash — staking and Earn rewards, airdrops,
  // rebates. Kept apart from "Rendimento", which is money.
  REWARD: "Recompensa",
  DERIVATIVE: "Resultado em derivativos",
  FEE: "Taxa",
  TAX: "Imposto",
  INFO: "Informativo",
  UNKNOWN: "Não mapeado",
};

export const KIND_LABELS: Record<string, string> = {
  // Domestic and offshore families are labelled in different languages on
  // purpose: "Ações" and "Stocks" are the same instrument, and the language is
  // what tells them apart at a glance in a chart legend.
  STOCK: "Ações",
  STOCK_INTL: "Stocks",
  FII: "FIIs",
  REIT: "REITs",
  ETF: "ETFs",
  ETF_INTL: "ETFs Exterior",
  BDR: "BDRs",
  CRYPTO: "Cripto",
  // Split out from CRYPTO on purpose: a balance of dollar-pegged tokens is
  // cash parked on the exchange, and folding it into "Cripto" would make the
  // allocation chart claim an exposure that is not there.
  STABLECOIN: "Stablecoins",
  /** Retired: units are classified as STOCK. Kept so old data still labels. */
  UNIT: "Ações",
  SUBSCRIPTION: "Subscrições",
  FIXED_INCOME: "Renda fixa",
  TREASURY: "Tesouro Direto",
  FUTURE: "Futuros",
  OPTION: "Opções",
  OTHER: "Outros",
};

export const opLabel = (value: string) => OP_LABELS[value] ?? value;
export const kindLabel = (value: string) => KIND_LABELS[value] ?? value;

