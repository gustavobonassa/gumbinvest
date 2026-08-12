/**
 * Chart components.
 *
 * Conventions enforced here (see `src/lib/colors.ts` for the palette rules):
 *  - one y-axis per chart, never a dual axis;
 *  - recessive grid/axes, 2px lines, 4px rounded bar ends anchored to the
 *    baseline, 2px surface gap between adjacent/stacked bars;
 *  - crosshair + tooltip on every plotted form;
 *  - a legend whenever there are two or more series, plus direct labels on the
 *    dominant slices, so identity is never carried by colour alone;
 *  - every chart can fall back to a table (`ChartFrame` `table` slot).
 */
import clsx from "clsx";
import { useEffect, useId, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ReferenceArea,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Table2, TrendingUp } from "lucide-react";
import type { ReactNode } from "react";

import { OTHER_COLOR, OTHER_KIND, SERIES, TOKENS, kindColor, kindRank, sequentialFill } from "@/lib/colors";
import { MASK, kindLabel, money, percent, periodLabel, shortDate } from "@/lib/format";
import { getValuesHidden as valuesHidden } from "@/lib/privacy";
import { Card, EmptyState, ErrorState, SectionTitle } from "@/components/ui";

const MONTH_LABELS = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"];

/**
 * Honour the OS "reduce motion" setting: Recharts' entry animations are
 * decorative, and users who ask for less motion (or automated renderers) must
 * still get a fully drawn chart on the first frame.
 */
const PREFERS_REDUCED_MOTION =
  typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
    : false;
const ANIMATE = !PREFERS_REDUCED_MOTION;
/** Spread onto every animated Recharts primitive. */
const MOTION = { isAnimationActive: ANIMATE } as const;

/**
 * Axis and grid chrome, read at render time rather than frozen at import: the
 * theme can change under a mounted chart, and a module-level literal would keep
 * painting the theme that was on when this file first loaded.
 */
const axisProps = () =>
  ({
    stroke: TOKENS.axis,
    tickLine: false,
    axisLine: false,
    tick: { fill: TOKENS.axis, fontSize: 11 },
  }) as const;

const grid = () => <CartesianGrid stroke={TOKENS.grid} strokeDasharray="3 3" vertical={false} />;

/** Band a bar/column tooltip highlights under the cursor. */
const cursorFill = () => ({ fill: TOKENS.cursor });

/** Axis ticks format their own numbers, so they mask their own numbers too —
    the plot keeps its shape, the scale beside it stops naming amounts. */
const compact = (value: number) =>
  valuesHidden()
    ? MASK
    : new Intl.NumberFormat("pt-BR", { notation: "compact", maximumFractionDigits: 1 }).format(value);

/** Shared tooltip shell: crosshair values, never a number on every point. */
function TooltipCard({
  label,
  rows,
}: {
  label: ReactNode;
  rows: { name: string; value: string; color?: string }[];
}) {
  return (
    <div className="animate-scale-in rounded-xl border border-line-strong bg-surface-raised/95 px-3 py-2 shadow-raised backdrop-blur-xl">
      <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-ink-muted">{label}</p>
      <div className="space-y-1">
        {rows.map((row) => (
          <div key={row.name} className="flex items-center justify-between gap-6 text-xs">
            <span className="flex items-center gap-1.5 text-ink-secondary">
              {row.color ? (
                <span className="h-2 w-2 rounded-full" style={{ background: row.color }} aria-hidden />
              ) : null}
              {row.name}
            </span>
            <span className="tnum font-medium text-ink">{row.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Frame with a title, an optional toolbar and a table fallback toggle. */
export function ChartFrame({
  title,
  subtitle,
  action,
  children,
  table,
  footer,
  height = 280,
  className,
  error,
  retry,
  loading,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
  table?: ReactNode;
  /** Note under the plot — what the chart cannot say in the plot itself. */
  footer?: ReactNode;
  height?: number;
  className?: string;
  /** A failed query must not render as an empty chart: in a finance app,
      "Sem posições" after a network error reads as "your money is gone". */
  error?: unknown;
  retry?: () => void;
  /** True while this chart's own query is in flight. Charts render an empty
      state when given no data, and "no data yet" must not flash as "no data
      exists" — each frame waits on its own query, not on a shared flag. */
  loading?: boolean;
}) {
  const [showTable, setShowTable] = useState(false);
  return (
    <Card className={clsx("p-5", className)}>
      <SectionTitle
        title={title}
        subtitle={subtitle}
        action={
          // Wraps: pages stack several Segmented controls here, and on a phone
          // they must fall onto new rows instead of running off the card.
          <div className="flex flex-wrap items-center justify-end gap-2">
            {action}
            {table ? (
              <button
                type="button"
                onClick={() => setShowTable((current) => !current)}
                className="btn-ghost px-2.5 py-2"
                aria-pressed={showTable}
                aria-label={showTable ? "Ver gráfico" : "Ver tabela"}
                title={showTable ? "Ver gráfico" : "Ver tabela"}
              >
                {showTable ? <TrendingUp size={15} /> : <Table2 size={15} />}
              </button>
            ) : null}
          </div>
        }
      />
      {error ? (
        <div style={{ height }} className="grid place-items-center">
          <ErrorState error={error} retry={retry} />
        </div>
      ) : loading ? (
        <div style={{ height }} className="skeleton w-full" />
      ) : showTable && table ? (
        // The floor keeps money columns from crushing to one digit per line on
        // a phone — below it the table scrolls sideways inside this box.
        <div className="max-h-[320px] overflow-auto [&_table]:min-w-[420px]">{table}</div>
      ) : (
        <div style={{ height }} className="animate-fade-in">
          {children}
        </div>
      )}
      {footer ? (
        <div className="mt-3 border-t border-line pt-3 text-xs text-ink-muted">{footer}</div>
      ) : null}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Portfolio value over time — market value vs. invested capital
// ---------------------------------------------------------------------------
export interface HistorySeriesPoint {
  date: string;
  market_value: number;
  cost_basis: number;
  dividends: number;
}

export function PortfolioHistoryChart({ data, height = 300 }: { data: HistorySeriesPoint[]; height?: number }) {
  const gradientId = useId().replace(/:/g, "");
  // Press and drag measures a stretch of the series: the anchor is where the
  // button went down, the cursor is where it is now. Both are dates, so the
  // measurement survives the chart being re-rendered at a different width.
  const [anchor, setAnchor] = useState<string | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  // The entry animation is a one-off for the first paint. Turning it back on
  // after a drag makes Recharts replay it — the flash seen on release — so
  // once the chart has been interacted with it stays off for good.
  const [animated, setAnimated] = useState(ANIMATE);

  // The release that ends a drag can happen anywhere — off the plot, off the
  // window — and the highlight has to clear with it either way.
  useEffect(() => {
    if (anchor === null) return undefined;
    setAnimated(false);
    const end = () => {
      setAnchor(null);
      setCursor(null);
    };
    window.addEventListener("mouseup", end);
    window.addEventListener("blur", end);
    return () => {
      window.removeEventListener("mouseup", end);
      window.removeEventListener("blur", end);
    };
  }, [anchor]);

  const selection = useMemo(() => {
    if (!anchor || !cursor || anchor === cursor) return null;
    const [from, to] = anchor <= cursor ? [anchor, cursor] : [cursor, anchor];
    const start = data.find((point) => point.date === from);
    const finish = data.find((point) => point.date === to);
    if (!start || !finish) return null;
    const change = Number(finish.market_value) - Number(start.market_value);
    const days = Math.round(
      (new Date(`${to}T00:00:00`).getTime() - new Date(`${from}T00:00:00`).getTime()) / 86_400_000,
    );
    return {
      from,
      to,
      days,
      change,
      // A percentage needs something to be a percentage *of*: a window that
      // starts at zero (before the first contribution) has no base, so the
      // move is reported in money alone.
      percent: Number(start.market_value) ? (change / Number(start.market_value)) * 100 : null,
      startValue: Number(start.market_value),
      endValue: Number(finish.market_value),
    };
  }, [anchor, cursor, data]);

  if (!data.length) {
    return <EmptyState title="Sem histórico" description="Importe transações para ver a evolução da carteira." />;
  }

  return (
    <div className={clsx("relative", anchor && "select-none")}>
      {selection ? (
        <div className="pointer-events-none absolute inset-x-0 top-0 z-10 flex justify-center">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-line-strong bg-surface-raised/95 px-3 py-2 text-sm shadow-raised backdrop-blur">
            <span className="tnum text-xs text-ink-muted">
              {shortDate(selection.from)} → {shortDate(selection.to)} · {selection.days} dias
            </span>
            <span className="tnum font-medium text-ink">
              {money(selection.startValue, { compact: true })} → {money(selection.endValue, { compact: true })}
            </span>
            <span
              className={clsx(
                "tnum font-semibold",
                selection.change >= 0 ? "text-positive" : "text-negative",
              )}
            >
              {selection.percent === null
                ? `${selection.change >= 0 ? "+" : ""}${money(selection.change)}`
                : `${percent(selection.percent, 2, true)} · ${selection.change >= 0 ? "+" : ""}${money(
                    selection.change,
                  )}`}
            </span>
          </div>
        </div>
      ) : null}
      <ResponsiveContainer debounce={150} width="100%" height={height}>
      <AreaChart
        data={data}
        margin={{ top: 8, right: 8, bottom: 0, left: 8 }}
        onMouseDown={(state: { activeLabel?: string | number }) => {
          const label = state?.activeLabel;
          if (label === undefined) return;
          setAnchor(String(label));
          setCursor(String(label));
        }}
        onMouseMove={(state: { activeLabel?: string | number }) => {
          if (anchor === null) return;
          const label = state?.activeLabel;
          if (label !== undefined) setCursor(String(label));
        }}
        onMouseUp={() => {
          setAnchor(null);
          setCursor(null);
        }}
      >
        <defs>
          <linearGradient id={`fill-${gradientId}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={SERIES[0]} stopOpacity={0.35} />
            <stop offset="100%" stopColor={SERIES[0]} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        {grid()}
        <XAxis dataKey="date" {...axisProps()} tickFormatter={(value) => shortDate(value).slice(3)} minTickGap={40} />
        <YAxis {...axisProps()} width={58} tickFormatter={compact} />
        <Tooltip
          cursor={{ stroke: TOKENS.axis, strokeDasharray: "4 4" }}
          // While a range is being dragged the readout above answers the
          // question; a per-day tooltip on top of it is two answers at once.
          content={({ active, payload, label }) =>
            active && payload?.length && anchor === null ? (
              <TooltipCard
                label={shortDate(String(label))}
                rows={[
                  { name: "Valor de mercado", value: money(payload[0]?.payload.market_value), color: SERIES[0] },
                  { name: "Capital investido", value: money(payload[0]?.payload.cost_basis), color: SERIES[1] },
                  {
                    name: "Resultado",
                    value: money(payload[0]?.payload.market_value - payload[0]?.payload.cost_basis),
                  },
                ]}
              />
            ) : null
          }
        />

        {/* Always mounted, only sometimes visible. Adding or removing a child
            makes Recharts rebuild the chart, which reads as the whole plot
            flickering on every mouse move — so the band is hidden by opacity
            and pinned to a single date, never unmounted. */}
        <ReferenceArea
          x1={selection ? selection.from : data[0]?.date}
          x2={selection ? selection.to : data[0]?.date}
          fill={selection && selection.change < 0 ? TOKENS.negative : TOKENS.positive}
          fillOpacity={selection ? 0.12 : 0}
          stroke={selection && selection.change < 0 ? TOKENS.negative : TOKENS.positive}
          strokeOpacity={selection ? 0.4 : 0}
          isAnimationActive={false}
        />
        <Legend
          verticalAlign="top"
          align="right"
          height={28}
          iconType="plainline"
          formatter={(value) => <span className="text-xs text-ink-secondary">{value}</span>}
        />
        <Area
          type="monotone"
          dataKey="market_value"
          name="Valor de mercado"
          stroke={SERIES[0]}
          strokeWidth={2}
          fill={`url(#fill-${gradientId})`}
          isAnimationActive={animated}
          activeDot={{ r: 4, strokeWidth: 2, stroke: TOKENS.surface }}
        />
        <Area
          type="monotone"
          dataKey="cost_basis"
          name="Capital investido"
          stroke={SERIES[1]}
          strokeWidth={2}
          strokeDasharray="5 4"
          fill="none"
          isAnimationActive={animated}
          activeDot={{ r: 4, strokeWidth: 2, stroke: TOKENS.surface }}
        />
      </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Company-level annual charts (asset page, Visão geral)
// ---------------------------------------------------------------------------

/** Revenue vs. net income per fiscal year — grouped, never stacked: profit is
    a slice of revenue, and stacking would draw it as an addition. */
export function YearlyFinancialsBars({
  data,
  currency,
  height = 260,
}: {
  data: { year: number; revenue?: number; earnings?: number }[];
  currency?: string;
  height?: number;
}) {
  if (!data.length) return <EmptyState title="Sem resultados anuais" />;
  const fmt = (value?: number) =>
    value === undefined ? "-" : money(value, { currency, compact: true, decimals: 0 });
  return (
    <ResponsiveContainer debounce={150} width="100%" height={height}>
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }} barCategoryGap="28%" barGap={2}>
        {grid()}
        <XAxis dataKey="year" {...axisProps()} />
        <YAxis {...axisProps()} width={58} tickFormatter={compact} />
        <ReferenceLine y={0} stroke={TOKENS.grid} strokeWidth={1} />
        <Tooltip
          cursor={cursorFill()}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipCard
                label={String(label)}
                rows={[
                  { name: "Receita", value: fmt(payload[0]?.payload.revenue), color: SERIES[1] },
                  { name: "Lucro líquido", value: fmt(payload[0]?.payload.earnings), color: SERIES[0] },
                ]}
              />
            ) : null
          }
        />
        <Legend
          verticalAlign="top"
          align="right"
          height={28}
          formatter={(value) => <span className="text-xs text-ink-secondary">{value}</span>}
        />
        <Bar dataKey="revenue" name="Receita" fill={SERIES[1]} radius={[4, 4, 0, 0]} maxBarSize={34} {...MOTION} />
        <Bar dataKey="earnings" name="Lucro líquido" fill={SERIES[0]} radius={[4, 4, 0, 0]} maxBarSize={34} {...MOTION} />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Declared payout per share per year; the year's yield rides above each bar
    as a label rather than on a second axis (one axis, always). */
export function DividendPerShareBars({
  data,
  currency,
  height = 260,
}: {
  data: { year: number; total_rate: number; payments: number; yield_pct?: number | null }[];
  currency?: string;
  height?: number;
}) {
  if (!data.length) return <EmptyState title="Sem histórico de proventos declarados" />;
  return (
    <ResponsiveContainer debounce={150} width="100%" height={height}>
      <BarChart data={data} margin={{ top: 20, right: 8, bottom: 0, left: 8 }} barCategoryGap="28%">
        {grid()}
        <XAxis dataKey="year" {...axisProps()} />
        <YAxis {...axisProps()} width={58} tickFormatter={compact} />
        <Tooltip
          cursor={cursorFill()}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipCard
                label={String(label)}
                rows={[
                  {
                    name: "Por cota/ação",
                    value: money(payload[0]?.payload.total_rate, { currency, decimals: 4 }),
                    color: SERIES[2],
                  },
                  ...(payload[0]?.payload.yield_pct != null
                    ? [{ name: "Yield no ano", value: percent(payload[0].payload.yield_pct, 2) }]
                    : []),
                  { name: "Pagamentos", value: String(payload[0]?.payload.payments) },
                ]}
              />
            ) : null
          }
        />
        <Bar dataKey="total_rate" name="Por cota/ação" fill={SERIES[2]} radius={[4, 4, 0, 0]} maxBarSize={38} {...MOTION}>
          <LabelList
            dataKey="yield_pct"
            position="top"
            formatter={(value: number | null) => (value == null ? "" : percent(value, 1))}
            fill={TOKENS.axis}
            fontSize={10}
          />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// Compound interest projection — future value vs. capital paid in
// ---------------------------------------------------------------------------
export interface ProjectionPoint {
  /** "YYYY-MM"; projections run on synthetic future months. */
  period: string;
  /** Projected net worth at the end of the month. */
  total: number;
  /** Start value plus every contribution made so far. */
  invested: number;
  /** total − invested: what compounding alone produced. */
  interest: number;
}

export function ProjectionChart({ data, height = 320 }: { data: ProjectionPoint[]; height?: number }) {
  const gradientId = useId().replace(/:/g, "");
  if (!data.length) return <EmptyState title="Sem projeção" description="Preencha os campos para simular." />;
  return (
    <ResponsiveContainer debounce={150} width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
        <defs>
          <linearGradient id={`fill-${gradientId}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={SERIES[0]} stopOpacity={0.35} />
            <stop offset="100%" stopColor={SERIES[0]} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        {grid()}
        <XAxis
          dataKey="period"
          {...axisProps()}
          tickFormatter={(value) => String(value).slice(0, 4)}
          minTickGap={40}
        />
        <YAxis {...axisProps()} width={58} tickFormatter={compact} />
        <Tooltip
          cursor={{ stroke: TOKENS.axis, strokeDasharray: "4 4" }}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipCard
                label={periodLabel(String(label))}
                rows={[
                  { name: "Patrimônio projetado", value: money(payload[0]?.payload.total), color: SERIES[0] },
                  { name: "Total investido", value: money(payload[0]?.payload.invested), color: SERIES[1] },
                  { name: "Juros acumulados", value: money(payload[0]?.payload.interest) },
                ]}
              />
            ) : null
          }
        />
        <Legend
          verticalAlign="top"
          align="right"
          height={28}
          iconType="plainline"
          formatter={(value) => <span className="text-xs text-ink-secondary">{value}</span>}
        />
        <Area
          type="monotone"
          dataKey="total"
          name="Patrimônio projetado"
          stroke={SERIES[0]}
          strokeWidth={2}
          fill={`url(#fill-${gradientId})`}
          {...MOTION}
          activeDot={{ r: 4, strokeWidth: 2, stroke: TOKENS.surface }}
        />
        <Area
          type="monotone"
          dataKey="invested"
          name="Total investido"
          stroke={SERIES[1]}
          strokeWidth={2}
          strokeDasharray="5 4"
          fill="none"
          {...MOTION}
          activeDot={{ r: 4, strokeWidth: 2, stroke: TOKENS.surface }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

/** Yearly composition of the projected balance: what was paid in versus what
    compounding added — stacked, because the two genuinely sum to the total. */
export function ProjectionBreakdownBars({
  data,
  height = 300,
}: {
  data: { period: string; invested: number; interest: number }[];
  height?: number;
}) {
  if (!data.length) return <EmptyState title="Sem projeção" description="Preencha os campos para simular." />;
  return (
    <ResponsiveContainer debounce={150} width="100%" height={height}>
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }} barCategoryGap="22%">
        {grid()}
        <XAxis dataKey="period" {...axisProps()} minTickGap={12} />
        <YAxis {...axisProps()} width={58} tickFormatter={compact} />
        <Tooltip
          cursor={cursorFill()}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipCard
                label={String(label)}
                rows={[
                  { name: "Total investido", value: money(payload[0]?.payload.invested), color: SERIES[1] },
                  { name: "Juros acumulados", value: money(payload[0]?.payload.interest), color: SERIES[0] },
                  {
                    name: "Patrimônio",
                    value: money(payload[0]?.payload.invested + payload[0]?.payload.interest),
                  },
                ]}
              />
            ) : null
          }
        />
        <Legend
          verticalAlign="top"
          align="right"
          height={28}
          formatter={(value) => <span className="text-xs text-ink-secondary">{value}</span>}
        />
        <Bar dataKey="invested" stackId="projection" name="Total investido" fill={SERIES[1]} maxBarSize={38} {...MOTION} />
        <Bar
          dataKey="interest"
          stackId="projection"
          name="Juros acumulados"
          fill={SERIES[0]}
          radius={[4, 4, 0, 0]}
          maxBarSize={38}
          {...MOTION}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// Return over time — the portfolio against itself, its classes and the market
// ---------------------------------------------------------------------------
export interface ReturnSeries {
  key: string;
  label: string;
  color: string;
  /** Set on reference series (an index), so "mine" and "the market" differ by
   *  more than hue — the benchmarks share the neutral greys. */
  dash?: string;
  /** The portfolio's own line, drawn a touch heavier and on top. */
  emphasis?: boolean;
}

export interface ReturnPoint {
  date: string;
  [key: string]: string | number | undefined;
}

/**
 * Cumulative return, in percent, rebased so every line starts at zero.
 *
 * Lines rather than areas: these are independent series that share an axis and
 * cross each other, and a stack would imply they add up — which percentages
 * never do. Everything is toggleable from the legend, because the comparison a
 * reader wants ("me versus CDI") is usually two of the lines, not all of them.
 */
export function ReturnLinesChart({
  data,
  series,
  hidden,
  onToggle,
  height = 340,
}: {
  data: ReturnPoint[];
  series: ReturnSeries[];
  hidden?: Set<string>;
  onToggle?: (key: string) => void;
  height?: number;
}) {
  if (!data.length || !series.length) {
    return <EmptyState title="Sem histórico" description="Importe transações para ver a rentabilidade." />;
  }
  const off = hidden ?? new Set<string>();
  const visible = series.filter((item) => !off.has(item.key));

  return (
    <ResponsiveContainer debounce={150} width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
        {grid()}
        <XAxis dataKey="date" {...axisProps()} tickFormatter={(value) => shortDate(value).slice(3)} minTickGap={40} />
        <YAxis {...axisProps()} width={56} tickFormatter={(value) => `${Math.round(value)}%`} />
        <ReferenceLine y={0} stroke={TOKENS.axis} strokeWidth={1} />
        <Tooltip
          cursor={{ stroke: TOKENS.axis, strokeDasharray: "4 4" }}
          content={({ active, payload, label }) => {
            if (!active || !payload?.length) return null;
            const point = payload[0].payload as ReturnPoint;
            // Ordered by value at the hovered date, so the reader sees who is
            // ahead without comparing line ends by eye.
            const rows = visible
              .filter((item) => point[item.key] !== undefined && point[item.key] !== null)
              .sort((a, b) => Number(point[b.key]) - Number(point[a.key]))
              .map((item) => ({
                name: item.label,
                value: percent(Number(point[item.key]), 2, true),
                color: item.color,
              }));
            return rows.length ? <TooltipCard label={shortDate(String(label))} rows={rows} /> : null;
          }}
        />
        <Legend
          verticalAlign="top"
          align="right"
          height={28}
          iconType="plainline"
          onClick={(entry) => {
            const key = (entry as { dataKey?: unknown } | undefined)?.dataKey;
            if (onToggle && key != null) onToggle(String(key));
          }}
          formatter={(value) => {
            const item = series.find((candidate) => candidate.key === String(value));
            return (
              <span
                className={clsx(
                  "text-xs",
                  onToggle && "cursor-pointer select-none",
                  off.has(String(value)) ? "text-ink-muted line-through" : "text-ink-secondary",
                )}
              >
                {item?.label ?? String(value)}
              </span>
            );
          }}
        />
        {series.map((item) => (
          <Line
            key={item.key}
            type="monotone"
            dataKey={item.key}
            name={item.key}
            hide={off.has(item.key)}
            stroke={item.color}
            strokeWidth={item.emphasis ? 2.5 : 2}
            strokeDasharray={item.dash}
            dot={false}
            connectNulls
            {...MOTION}
            activeDot={{ r: 4, strokeWidth: 2, stroke: TOKENS.surface }}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// Allocation donut
// ---------------------------------------------------------------------------
export interface AllocationDatum {
  key: string;
  label: string;
  value: number;
  percent: number;
  /**
   * What the legend prints. Defaults to `key`, which is the ticker when slicing
   * by asset; grouping by class or broker passes a readable name instead, since
   * the key there is an internal enum value.
   */
  legend?: string;
  /** Fixed colour. Set when the entity owns one — an asset class always does. */
  color?: string;
  /** What an aggregate slice ("Outros") swallowed — shown in its tooltip. */
  members?: { label: string; value: number; percent: number }[];
}

/**
 * Slices for a class-grouped chart: every class gets its own fixed colour, the
 * order is the palette's (see `KIND_ORDER`), and the families with no colour of
 * their own collapse into a single neutral bucket rather than several
 * indistinguishable grey slivers.
 */
export function kindSlices(
  rows: { kind: string; value: number; percent: number }[],
): AllocationDatum[] {
  const mapped: AllocationDatum[] = [];
  let otherValue = 0;
  let otherPercent = 0;
  let otherCount = 0;
  const otherMembers: { label: string; value: number; percent: number }[] = [];

  for (const row of rows) {
    if (kindColor(row.kind) === OTHER_COLOR) {
      otherValue += Number(row.value);
      otherPercent += Number(row.percent);
      otherCount += 1;
      otherMembers.push({ label: kindLabel(row.kind), value: Number(row.value), percent: Number(row.percent) });
      continue;
    }
    mapped.push({
      key: row.kind,
      label: kindLabel(row.kind),
      legend: kindLabel(row.kind),
      value: Number(row.value),
      percent: Number(row.percent),
      color: kindColor(row.kind),
    });
  }

  mapped.sort((a, b) => kindRank(a.key) - kindRank(b.key));
  if (otherCount) {
    mapped.push({
      key: OTHER_KIND,
      label: otherCount > 1 ? `Outros (${otherCount})` : "Outros",
      value: otherValue,
      percent: otherPercent,
      color: OTHER_COLOR,
      members: otherMembers.sort((a, b) => b.value - a.value),
    });
  }
  return mapped;
}

export function AllocationDonut({
  data,
  height = 300,
  centerLabel,
  centerValue,
  valueMode = "percent",
}: {
  data: AllocationDatum[];
  height?: number;
  centerLabel?: string;
  centerValue?: string;
  /** What the legend prints per slice: share of the whole, or money. */
  valueMode?: "percent" | "value";
}) {
  const legendValue = (item: AllocationDatum) =>
    valueMode === "value" ? money(item.value, { compact: true, decimals: 1 }) : percent(item.percent, 1);
  const colored = useMemo(
    () =>
      data.map((item, index) => ({
        ...item,
        color: item.color ?? (item.key === OTHER_KIND ? OTHER_COLOR : SERIES[index % SERIES.length]),
      })),
    [data],
  );
  if (!colored.length) return <EmptyState title="Sem posições" description="Nenhuma posição em aberto." />;

  return (
    <div className="flex h-full flex-col gap-4 lg:flex-row lg:items-center">
      <div className="relative min-w-0 flex-1" style={{ height }}>
        <ResponsiveContainer debounce={150} width="100%" height="100%">
          <PieChart>
            <Pie
              data={colored}
              dataKey="value"
              nameKey="label"
              innerRadius="62%"
              outerRadius="88%"
              paddingAngle={1.5}
              stroke={TOKENS.surface}
              strokeWidth={2}
              animationDuration={600}
              // Sectors are not individually focusable: the browser draws a
              // focus ring around the *bounding box* of an arc, which reads as
              // a stray square. The table view carries the keyboard path.
              rootTabIndex={-1}
              {...MOTION}
            >
              {colored.map((item) => (
                <Cell key={item.key} fill={item.color} />
              ))}
            </Pie>
            <Tooltip
              content={({ active, payload }) => {
                if (!active || !payload?.length) return null;
                const slice = payload[0]?.payload as AllocationDatum & { color?: string };
                const rows = [
                  { name: "Valor", value: money(slice.value), color: slice.color },
                  { name: "Participação", value: percent(slice.percent) },
                ];
                // An aggregate slice owes the reader its contents.
                for (const member of (slice.members ?? []).slice(0, 8)) {
                  rows.push({
                    name: `· ${member.label}`,
                    value: valueMode === "value" ? money(member.value, { compact: true, decimals: 1 }) : percent(member.percent, 1),
                    color: undefined,
                  });
                }
                if ((slice.members?.length ?? 0) > 8) {
                  rows.push({ name: `…e mais ${(slice.members?.length ?? 0) - 8}`, value: "", color: undefined });
                }
                return <TooltipCard label={slice.label} rows={rows} />;
              }}
            />
          </PieChart>
        </ResponsiveContainer>
        {centerValue ? (
          <div className="pointer-events-none absolute inset-0 z-10 flex flex-col items-center justify-center">
            <span className="text-[11px] uppercase tracking-wide text-ink-muted">{centerLabel}</span>
            <span className="tnum text-xl font-semibold text-ink">{centerValue}</span>
          </div>
        ) : null}
      </div>
      {/* Legend doubles as the direct-label channel: identity never colour-only. */}
      <ul
        className="grid min-h-0 flex-1 auto-rows-min gap-1.5 overflow-auto pr-1 lg:max-w-[46%]"
        style={{ maxHeight: height }}
      >
        {colored.map((item) => (
          <li
            key={item.key}
            className="flex items-center justify-between gap-3 rounded-lg px-2 py-1.5 text-sm hover:bg-surface-hover"
            // The browser-native tooltip answers "o que tem dentro de Outros?"
            // from the legend too, not just from hovering the slice.
            title={item.members?.length ? item.members.map((member) => member.label).join(", ") : undefined}
          >
            <span className="flex min-w-0 items-center gap-2">
              <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: item.color }} aria-hidden />
              <span className="truncate text-ink-secondary">
                {item.key === OTHER_KIND ? item.label : (item.legend ?? item.key)}
              </span>
            </span>
            <span className="tnum shrink-0 font-medium text-ink">{legendValue(item)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Income / contributions bars
// ---------------------------------------------------------------------------
export function IncomeBars({
  data,
  height = 280,
  dataKey = "total",
  label = "Proventos",
  colorIndex = 2,
  currency,
}: {
  data: { period: string; [key: string]: string | number }[];
  height?: number;
  dataKey?: string;
  label?: string;
  colorIndex?: number;
  /** The currency the values are in. Defaults to the portfolio's own; an asset
   *  page passes its own, because a US holding's dividends are dollars and
   *  printing "R$" in front of them overstates them fivefold. */
  currency?: string;
}) {
  if (!data.length) return <EmptyState title="Sem dados no período" />;
  const color = SERIES[colorIndex];
  return (
    <ResponsiveContainer debounce={150} width="100%" height={height}>
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }} barCategoryGap="22%">
        {grid()}
        <XAxis dataKey="period" {...axisProps()} tickFormatter={periodLabel} minTickGap={12} />
        <YAxis {...axisProps()} width={58} tickFormatter={compact} />
        <Tooltip
          cursor={cursorFill()}
          content={({ active, payload, label: period }) =>
            active && payload?.length ? (
              <TooltipCard
                label={periodLabel(String(period))}
                rows={[{ name: label, value: money(Number(payload[0]?.value), { currency }), color }]}
              />
            ) : null
          }
        />
        <Bar
          dataKey={dataKey}
          name={label}
          fill={color}
          radius={[4, 4, 0, 0]}
          maxBarSize={38}
          animationDuration={500}
          {...MOTION}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Contributions: bought vs. sold, one axis, 2px gap between adjacent bars. */
export function ContributionBars({
  data,
  height = 280,
}: {
  data: { period: string; bought: number; sold: number; net: number }[];
  height?: number;
}) {
  if (!data.length) return <EmptyState title="Sem aportes no período" />;
  return (
    <ResponsiveContainer debounce={150} width="100%" height={height}>
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }} barCategoryGap="24%" barGap={2}>
        {grid()}
        <XAxis dataKey="period" {...axisProps()} tickFormatter={periodLabel} minTickGap={12} />
        <YAxis {...axisProps()} width={58} tickFormatter={compact} />
        <Tooltip
          cursor={cursorFill()}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipCard
                label={periodLabel(String(label))}
                rows={[
                  { name: "Compras", value: money(payload[0]?.payload.bought), color: SERIES[0] },
                  { name: "Vendas", value: money(payload[0]?.payload.sold), color: SERIES[1] },
                  { name: "Aporte líquido", value: money(payload[0]?.payload.net) },
                ]}
              />
            ) : null
          }
        />
        <Legend
          verticalAlign="top"
          align="right"
          height={28}
          iconType="square"
          formatter={(value) => <span className="text-xs text-ink-secondary">{value}</span>}
        />
        <Bar dataKey="bought" name="Compras" fill={SERIES[0]} radius={[4, 4, 0, 0]} maxBarSize={26} {...MOTION} />
        <Bar dataKey="sold" name="Vendas" fill={SERIES[1]} radius={[4, 4, 0, 0]} maxBarSize={26} {...MOTION} />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Monthly returns: polarity, so status colours (not categorical) apply. */
export function ReturnsBars({
  data,
  height = 280,
}: {
  data: { period: string; return_pct: number; profit: number }[];
  height?: number;
}) {
  if (!data.length) return <EmptyState title="Sem retornos calculados" />;
  return (
    <ResponsiveContainer debounce={150} width="100%" height={height}>
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }} barCategoryGap="22%">
        {grid()}
        <XAxis dataKey="period" {...axisProps()} tickFormatter={periodLabel} minTickGap={12} />
        <YAxis {...axisProps()} width={52} tickFormatter={(value) => `${value}%`} />
        <ReferenceLine y={0} stroke={TOKENS.grid} strokeWidth={1} />
        <Tooltip
          cursor={cursorFill()}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipCard
                label={periodLabel(String(label))}
                rows={[
                  { name: "Retorno", value: percent(payload[0]?.payload.return_pct, 2, true) },
                  { name: "Resultado", value: money(payload[0]?.payload.profit) },
                ]}
              />
            ) : null
          }
        />
        <Bar dataKey="return_pct" name="Retorno" radius={[4, 4, 0, 0]} maxBarSize={30} {...MOTION}>
          {data.map((item) => (
            <Cell
              key={item.period}
              fill={item.return_pct >= 0 ? TOKENS.positive : TOKENS.negative}
            />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Horizontal ranking of results by asset (magnitude + polarity). */
export function ProfitByAssetChart({
  data,
  height = 320,
}: {
  data: { ticker: string; value: number }[];
  height?: number;
}) {
  if (!data.length) return <EmptyState title="Sem resultados" />;
  return (
    <ResponsiveContainer debounce={150} width="100%" height={height}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 8 }} barCategoryGap="18%">
        <CartesianGrid stroke={TOKENS.grid} strokeDasharray="3 3" horizontal={false} />
        <XAxis type="number" {...axisProps()} tickFormatter={compact} />
        <YAxis type="category" dataKey="ticker" {...axisProps()} width={72} />
        <ReferenceLine x={0} stroke={TOKENS.grid} />
        <Tooltip
          cursor={cursorFill()}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipCard label={String(label)} rows={[{ name: "Resultado", value: money(Number(payload[0]?.value)) }]} />
            ) : null
          }
        />
        <Bar dataKey="value" radius={[0, 4, 4, 0]} maxBarSize={22} {...MOTION}>
          {data.map((item) => (
            <Cell key={item.ticker} fill={item.value >= 0 ? TOKENS.positive : TOKENS.negative} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// Dividends

/**
 * Income per period, split by kind of payment.
 *
 * Stacked because the parts sum to a total that is itself meaningful — "what
 * landed in March" — which grouped bars would make the reader add up by eye.
 * Segments are separated by a 2px surface gap and the legend carries identity.
 */
/**
 * Income per period, optionally broken into stacked segments.
 *
 * The breakdown axis is the caller's choice — payment type or asset class —
 * because the two answer different questions and neither is a substitute for
 * the other: "what kind of payment was it" versus "what paid it". The component
 * only needs the keys and a way to label them.
 */
export function IncomeBreakdownBars({
  data,
  keys,
  labelFor,
  colorFor,
  hidden,
  onToggle,
  onSelect,
  height = 300,
}: {
  data: { period: string; total: number; [key: string]: string | number }[];
  /** Stack segments, largest first. Empty means a single total bar. */
  keys: string[];
  labelFor: (key: string) => string;
  /** Colour per stack key. Classes own theirs; ranked keys take palette slots. */
  colorFor?: (key: string, index: number) => string;
  /** Keys switched off from the legend; still listed, just not plotted. */
  hidden?: Set<string>;
  onToggle?: (key: string) => void;
  /** Called with the period of the bar that was clicked. */
  onSelect?: (period: string) => void;
  height?: number;
}) {
  if (!data.length) return <EmptyState title="Sem proventos no período" />;
  const off = hidden ?? new Set<string>();
  const visible = keys.filter((key) => !off.has(key));
  const stacked = keys.length > 0;
  const colour = colorFor ?? ((_key: string, index: number) => SERIES[index % SERIES.length]);
  // With segments switched off the bar no longer reaches the stored total, so
  // the tooltip adds up what is actually drawn.
  const totalOf = (point: Record<string, number>) =>
    stacked ? visible.reduce((sum, key) => sum + (Number(point[key]) || 0), 0) : Number(point.total);

  return (
    <ResponsiveContainer debounce={150} width="100%" height={height}>
      <BarChart
        data={data}
        margin={{ top: 8, right: 8, bottom: 0, left: 8 }}
        barCategoryGap="22%"
        onClick={(state) => {
          const period = (state as { activeLabel?: unknown } | undefined)?.activeLabel;
          if (onSelect && period != null) onSelect(String(period));
        }}
        style={onSelect ? { cursor: "pointer" } : undefined}
      >
        {grid()}
        <XAxis dataKey="period" {...axisProps()} tickFormatter={periodLabel} minTickGap={12} />
        <YAxis {...axisProps()} width={58} tickFormatter={compact} />
        <Tooltip
          cursor={cursorFill()}
          content={({ active, payload, label }) => {
            if (!active || !payload?.length) return null;
            const point = payload[0].payload as Record<string, number>;
            // The tooltip answers whatever the bar is asking. One bar is one
            // number; only a stacked bar owes a breakdown — and then it lists
            // every segment on screen, so the tooltip and the stack agree.
            const parts = stacked
              ? keys
                  .map((key, index) => ({ key, index }))
                  .filter(({ key }) => !off.has(key) && Number(point[key]))
                  .map(({ key, index }) => ({
                    name: labelFor(key),
                    value: money(point[key]),
                    color: colour(key, index),
                  }))
              : [];
            return (
              <TooltipCard
                label={periodLabel(String(label))}
                rows={[
                  ...parts,
                  {
                    name: parts.length ? "Total" : "Proventos",
                    value: money(totalOf(point)),
                    color: parts.length ? undefined : SERIES[2],
                  },
                ]}
              />
            );
          }}
        />
        {stacked ? (
          <Legend
            verticalAlign="top"
            align="right"
            height={28}
            iconType="circle"
            iconSize={8}
            onClick={(entry) => {
              const key = (entry as { dataKey?: unknown } | undefined)?.dataKey;
              if (onToggle && key != null) onToggle(String(key));
            }}
            formatter={(value) => (
              <span
                className={clsx(
                  "text-xs",
                  onToggle && "cursor-pointer select-none",
                  off.has(String(value)) ? "text-ink-muted line-through" : "text-ink-secondary",
                )}
              >
                {labelFor(String(value))}
              </span>
            )}
          />
        ) : null}
        {stacked ? (
          keys.map((key, index) => (
            <Bar
              key={key}
              dataKey={key}
              stackId="income"
              name={key}
              hide={off.has(key)}
              fill={colour(key, index)}
              stroke={TOKENS.surface}
              strokeWidth={2}
              // The top segment is the last *visible* one, not the last defined.
              radius={key === visible[visible.length - 1] ? [4, 4, 0, 0] : undefined}
              maxBarSize={44}
              animationDuration={500}
              {...MOTION}
            />
          ))
        ) : (
          <Bar
            dataKey="total"
            name="Proventos"
            fill={SERIES[2]}
            radius={[4, 4, 0, 0]}
            maxBarSize={44}
            animationDuration={500}
            {...MOTION}
          />
        )}
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Ranked payers: horizontal bars, one hue — magnitude, not identity. */
export function TopPayersBars({
  data,
  height = 340,
}: {
  /** `net` is what the bar shows — see the Proventos page: every figure
   *  there is after withholding. */
  data: { ticker: string; net: number }[];
  height?: number;
}) {
  if (!data.length) return <EmptyState title="Sem proventos" />;
  return (
    <ResponsiveContainer debounce={150} width="100%" height={height}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 56, bottom: 4, left: 8 }} barCategoryGap="20%">
        <CartesianGrid stroke={TOKENS.grid} strokeDasharray="3 3" horizontal={false} />
        <XAxis type="number" {...axisProps()} tickFormatter={compact} />
        <YAxis type="category" dataKey="ticker" {...axisProps()} width={78} />
        <Tooltip
          cursor={cursorFill()}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipCard
                label={String(label)}
                rows={[{ name: "Proventos (líquido)", value: money(Number(payload[0]?.value)), color: SERIES[2] }]}
              />
            ) : null
          }
        />
        <Bar dataKey="net" fill={SERIES[2]} radius={[0, 4, 4, 0]} maxBarSize={20} {...MOTION}>
          {/* Direct labels on a ranked list: the value belongs beside the bar. */}
          <LabelList
            dataKey="net"
            position="right"
            formatter={(value: number) => money(value, { compact: true })}
            style={{ fill: TOKENS.inkSecondary, fontSize: 11 }}
          />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/**
 * Month × year income matrix.
 *
 * A table that happens to be shaded: the numbers are always readable, and the
 * single-hue wash (see `sequentialFill`) makes seasonality and growth visible
 * without asking anyone to compare 90 numbers by eye.
 */
export function IncomeMatrix({ data }: { data: { period: string; total: number }[] }) {
  const { years, cells, max, yearTotals } = useMemo(() => {
    const cells = new Map<string, number>();
    const yearTotals = new Map<number, number>();
    let max = 0;
    for (const point of data) {
      const [year, month] = point.period.split("-").map(Number);
      if (!year || !month) continue;
      const value = Number(point.total) || 0;
      cells.set(`${year}-${month}`, value);
      yearTotals.set(year, (yearTotals.get(year) ?? 0) + value);
      if (value > max) max = value;
    }
    return { years: [...yearTotals.keys()].sort((a, b) => b - a), cells, max, yearTotals };
  }, [data]);

  if (!years.length) return <EmptyState title="Sem proventos" />;

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[760px] border-separate border-spacing-0.5 text-xs">
        <caption className="sr-only">Proventos recebidos por mês e ano</caption>
        <thead>
          <tr className="text-ink-muted">
            <th scope="col" className="px-2 py-1 text-left font-medium">
              Ano
            </th>
            {MONTH_LABELS.map((month) => (
              <th key={month} scope="col" className="px-1 py-1 text-center font-medium">
                {month}
              </th>
            ))}
            <th scope="col" className="px-2 py-1 text-right font-medium">
              Total
            </th>
          </tr>
        </thead>
        <tbody>
          {years.map((year) => (
            <tr key={year}>
              <th scope="row" className="tnum px-2 py-1 text-left font-medium text-ink-secondary">
                {year}
              </th>
              {MONTH_LABELS.map((month, index) => {
                const value = cells.get(`${year}-${index + 1}`) ?? 0;
                return (
                  <td
                    key={month}
                    className="tnum rounded-md px-1 py-1.5 text-center text-ink"
                    style={{ background: sequentialFill(max ? value / max : 0) }}
                    title={`${month}/${year}: ${money(value)}`}
                  >
                    {value ? money(value, { compact: true }) : <span className="text-ink-muted">-</span>}
                  </td>
                );
              })}
              <td className="tnum px-2 py-1 text-right font-medium text-ink">
                {money(yearTotals.get(year) ?? 0, { compact: true })}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Single-series price line (asset detail). No legend: the title names it. */
export function PriceLine({
  data,
  height = 260,
  averagePrice,
  currency,
}: {
  data: { date: string; close: number }[];
  height?: number;
  averagePrice?: number;
  /** See IncomeBars: a foreign asset is quoted in its own currency. */
  currency?: string;
}) {
  // The window's extremes get the only direct labels on the plot: they are the
  // two prices a reader actually hunts for, and labeling every point is noise.
  const extremes = useMemo(() => {
    if (data.length < 2) return null;
    let low = data[0];
    let high = data[0];
    for (const point of data) {
      if (point.close < low.close) low = point;
      if (point.close > high.close) high = point;
    }
    return low === high ? null : { low, high };
  }, [data]);

  if (!data.length) {
    return (
      <EmptyState
        title="Sem histórico de preços"
        description="Ative um provedor de cotações e execute o backfill em Configurações."
      />
    );
  }
  return (
    <ResponsiveContainer debounce={150} width="100%" height={height}>
      <LineChart data={data} margin={{ top: 16, right: 8, bottom: 0, left: 8 }}>
        {grid()}
        <XAxis dataKey="date" {...axisProps()} tickFormatter={(value) => shortDate(value).slice(3)} minTickGap={40} />
        <YAxis {...axisProps()} width={58} domain={["auto", "auto"]} tickFormatter={compact} />
        {extremes ? (
          <>
            <ReferenceDot
              x={extremes.high.date}
              y={extremes.high.close}
              r={3}
              fill={SERIES[0]}
              stroke={TOKENS.surface}
              strokeWidth={2}
              label={{
                value: money(extremes.high.close, { currency, compact: true }),
                fill: TOKENS.axis,
                fontSize: 10,
                position: "top",
              }}
            />
            <ReferenceDot
              x={extremes.low.date}
              y={extremes.low.close}
              r={3}
              fill={SERIES[0]}
              stroke={TOKENS.surface}
              strokeWidth={2}
              label={{
                value: money(extremes.low.close, { currency, compact: true }),
                fill: TOKENS.axis,
                fontSize: 10,
                position: "bottom",
              }}
            />
          </>
        ) : null}
        {averagePrice ? (
          <ReferenceLine
            y={averagePrice}
            stroke={SERIES[3]}
            strokeDasharray="5 4"
            label={{ value: "Preço médio", fill: TOKENS.axis, fontSize: 11, position: "insideTopLeft" }}
          />
        ) : null}
        <Tooltip
          cursor={{ stroke: TOKENS.axis, strokeDasharray: "4 4" }}
          content={({ active, payload, label }) =>
            active && payload?.length ? (
              <TooltipCard
                label={shortDate(String(label))}
                rows={[{ name: "Fechamento", value: money(Number(payload[0]?.value), { currency }), color: SERIES[0] }]}
              />
            ) : null
          }
        />
        <Line
          type="monotone"
          dataKey="close"
          stroke={SERIES[0]}
          strokeWidth={2}
          dot={false}
          activeDot={{ r: 4, strokeWidth: 2, stroke: TOKENS.surface }}
          {...MOTION}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
