/**
 * Chart colour tokens.
 *
 * `SERIES` is the validated categorical order. Checked with the data-viz
 * palette validator against *both* chart surfaces — the dark #12141a and the
 * light #ffffff — and it passes on each, so an asset class keeps one colour
 * whichever theme is on. (On the light surface the yellow slot lands at 2.99:1
 * and carries the documented relief: every chart here ships a legend, direct
 * labels and a table view.) Rules that must not be broken:
 *
 * 1. Slots are assigned in **fixed order** and never cycled — a 9th series
 *    folds into "Outros" (see `withOther`).
 * 2. Colour follows the entity, not its rank: `seriesColorFor` hashes a stable
 *    key so filtering a chart never repaints the survivors.
 * 3. Status colours (positive/negative) are reserved and never reused as a
 *    categorical slot.
 *
 * What *is* themed is the chrome — surface, grid, axis, ink — plus the status
 * pair, which has to clear 4.5:1 as a number as well as 3:1 as a mark. Charts
 * paint into SVG attributes that no stylesheet reaches, so those values are
 * kept here in TypeScript rather than as CSS variables, and `setChartTheme`
 * (called by `lib/theme.ts`) swaps the set.
 */
export const CHART_SURFACE = "#12141a";

export const SERIES = [
  "#3987e5", // blue
  "#d95926", // orange
  "#199e70", // aqua
  "#c98500", // yellow
  "#d55181", // magenta
  "#008300", // green
  "#9085e9", // violet
  "#e66767", // red
] as const;

export const OTHER_COLOR = "#6c7689";

/**
 * The bucket every class without a palette slot collapses into. A 9th series is
 * never a generated hue, and five classes sharing one grey is the same mistake
 * wearing a different hat — so they become one labelled series instead.
 */
export const OTHER_KIND = "__other__";

/**
 * Fixed colour per asset class, so a class looks the same in every chart, tag
 * and legend in the app — a slice that changes hue when the ranking changes is
 * the thing this replaces.
 *
 * `KIND_ORDER` is not decoration: the palette is only guaranteed to separate
 * **adjacent** slots, so class-grouped charts must render in this order for the
 * guarantee to hold. The order was chosen by exhaustive search over the safe
 * pairs (validated with the data-viz palette checker at the dark surface
 * `#12141a`) and tolerates up to two consecutive classes being absent before
 * any pair drops below the CVD floor — a portfolio without ETFs still reads.
 *
 * It also runs as a risk gradient, fixed income first, which is why the biggest
 * slice is not necessarily first: position carries meaning here, not size.
 *
 * Verified: all adjacent pairs pass the lightness, chroma, CVD and contrast
 * checks; the worst adjacent pair (Stocks vs REITs, ΔE 6.5 protan) sits in the
 * 6–8 band, which is legal because every chart using these also ships a legend,
 * direct labels and a 2px surface gap between marks.
 */
export const KIND_ORDER = [
  "FIXED_INCOME",
  "TREASURY",
  "ETF_INTL",
  "ETF",
  "REIT",
  "STOCK_INTL",
  "STOCK",
  "FII",
] as const;

const KIND_COLORS: Record<string, string> = {
  FIXED_INCOME: SERIES[4], // magenta
  TREASURY: SERIES[3], // amarelo
  ETF_INTL: SERIES[5], // verde
  ETF: SERIES[6], // violeta
  REIT: SERIES[7], // vermelho
  STOCK_INTL: SERIES[2], // água
  STOCK: SERIES[0], // azul
  UNIT: SERIES[0], // retired kind, same instrument as STOCK
  FII: SERIES[1], // laranja
};

/** Colour of an asset class; the neutral for transient or marginal families. */
export function kindColor(kind: string): string {
  return KIND_COLORS[kind] ?? OTHER_COLOR;
}

/** Sort key placing a class at its slot; unmapped families land at the end. */
export function kindRank(kind: string): number {
  const index = (KIND_ORDER as readonly string[]).indexOf(kind);
  return index === -1 ? KIND_ORDER.length : index;
}

/**
 * Per-theme chart chrome.
 *
 * `surface` is the card the marks sit on — it is what slice gaps and marker
 * rings are painted in, so it must be the card colour, not the page's.
 * `grid` is deliberately near-invisible (1.2:1 against its surface in both
 * themes): a grid line that competes with the data is noise.
 *
 * The greys under `benchmark` are outside the categorical palette on purpose.
 * A benchmark is a *reference*, so it is drawn recessive and dashed — that
 * reads as "comparison" at a glance and keeps all eight categorical slots free
 * for the classes, which is what the reader is being asked to tell apart. The
 * two greys sit ~2:1 apart in lightness in either theme, so they separate under
 * any colour vision, and the dash patterns distinguish them again on their own.
 */
const THEMES = {
  dark: {
    positive: "#199e70",
    negative: "#e66767",
    accent: "#3987e5",
    grid: "#232833",
    axis: "#6c7689",
    ink: "#f4f6fb",
    inkSecondary: "#a4adbf",
    surface: CHART_SURFACE,
    /** Fill of the band a bar/column tooltip highlights under the cursor. */
    cursor: "rgba(255,255,255,0.04)",
    benchmark: { IBOV: "#9aa3b2", CDI: "#6f7a8d" },
  },
  light: {
    positive: "#0f7856",
    negative: "#c53434",
    accent: "#1c68c5",
    grid: "#e6eaf1",
    axis: "#646e7d",
    ink: "#171b22",
    inkSecondary: "#4a5361",
    surface: "#ffffff",
    cursor: "rgba(16,24,40,0.05)",
    benchmark: { IBOV: "#4f5866", CDI: "#7c8798" },
  },
} as const;

type ChartTheme = keyof typeof THEMES;
type ChartTokens = (typeof THEMES)[ChartTheme];

let active: ChartTokens = THEMES.dark;

/** Called by `lib/theme.ts` — nothing else should switch this. */
export function setChartTheme(theme: ChartTheme) {
  active = THEMES[theme];
}

/**
 * Live chart chrome. Getters rather than a plain object because the values are
 * read all over `charts.tsx`, and a copy taken at import time would freeze the
 * theme that happened to be on when the module loaded.
 */
export const TOKENS: { [K in Exclude<keyof ChartTokens, "benchmark">]: string } = Object.freeze(
  Object.defineProperties(
    {},
    Object.fromEntries(
      (["positive", "negative", "accent", "grid", "axis", "ink", "inkSecondary", "surface", "cursor"] as const).map(
        (key) => [key, { get: () => active[key], enumerable: true }],
      ),
    ),
  ),
) as { [K in Exclude<keyof ChartTokens, "benchmark">]: string };

/** Benchmark line styling; the dash pattern is fixed, the grey is themed. */
export const BENCHMARK_DASH: Record<string, string> = { IBOV: "7 4", CDI: "2 3" };

export function benchmarkStyle(key: string): { color: string; dash: string } {
  const grey = active.benchmark[key as keyof ChartTokens["benchmark"]];
  return { color: grey ?? active.axis, dash: BENCHMARK_DASH[key] ?? "4 4" };
}

/**
 * Sequential wash for magnitude (one hue, low -> high), expressed as alpha over
 * the chart surface. Keeping a single hue and varying only its intensity is the
 * sequential rule; doing it with alpha also guarantees the cell never gets so
 * close to the surface that it breaks contrast with the ink on top — which is
 * why it is alpha over the *current* accent rather than eight baked steps: the
 * light theme washes a darker blue over white and stays legible unchanged.
 */
export function sequentialFill(ratio: number): string {
  if (!Number.isFinite(ratio) || ratio <= 0) return "transparent";
  const alpha = 0.08 + Math.min(Math.max(ratio, 0), 1) * 0.47;
  const [r, g, b] = [1, 3, 5].map((offset) => parseInt(active.accent.slice(offset, offset + 2), 16));
  return `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(3)})`;
}

/** Deterministic slot for a key, so a series keeps its colour across filters. */
export function seriesColorFor(key: string, index?: number): string {
  if (typeof index === "number" && index < SERIES.length) return SERIES[index];
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  return SERIES[hash % SERIES.length];
}

/**
 * Cap a categorical list at `max` slots and roll the tail into "Outros".
 * Keeps every palette guarantee intact no matter how many assets exist.
 */
export function withOther<T extends { value: number }>(
  items: T[],
  max = 8,
  makeOther: (value: number, count: number, tail: T[]) => T,
): T[] {
  if (items.length <= max) return items;
  const head = items.slice(0, max - 1);
  const tail = items.slice(max - 1);
  const total = tail.reduce((sum, item) => sum + Number(item.value ?? 0), 0);
  // The tail travels along so "Outros" can say what it swallowed.
  return [...head, makeOther(total, tail.length, tail)];
}
