/** Side-by-side asset comparison: indexed price performance + fundamentals.
 *
 * Any B3/US ticker can join the comparison, held or not — opening one that the
 * wallet never traded quietly creates a watch-only asset (same flow as the
 * asset page), so its quote, history and fundamentals exist by the time the
 * queries land. Selection lives in the URL (`?ativos=PETR4,PRIO3`), making a
 * comparison shareable and reload-proof.
 */
import { useQueries, useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { GitCompareArrows, Plus, Search, Trophy, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ChartFrame, ReturnLinesChart, type ReturnPoint, type ReturnSeries } from "@/components/charts";
import { Badge, Card, EmptyState, KindTag, SectionTitle, Segmented, Skeleton } from "@/components/ui";
import { api, type AssetDetail as AssetDetailData, type Fundamentals } from "@/lib/api";
import { SERIES } from "@/lib/colors";
import { decimal, money, percent, shortDate } from "@/lib/format";

const MAX_TICKERS = 4;

type Range = "6m" | "1y" | "5y" | "max";
const RANGE_MONTHS: Record<Range, number | null> = { "6m": 6, "1y": 12, "5y": 60, max: null };

/** Ticker picker with suggestions from the wallet and from the market. */
function TickerPicker({ selected, onAdd }: { selected: string[]; onAdd: (ticker: string) => void }) {
  const [term, setTerm] = useState("");
  const [focused, setFocused] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const local = useQuery({
    queryKey: ["search", term],
    queryFn: () => api.search(term),
    enabled: term.trim().length >= 2,
    staleTime: 20_000,
  });
  // Debounced copy for the query that leaves the machine (same pattern as the
  // global search dialog).
  const [marketTerm, setMarketTerm] = useState("");
  useEffect(() => {
    const handle = setTimeout(() => setMarketTerm(term.trim()), 300);
    return () => clearTimeout(handle);
  }, [term]);
  const market = useQuery({
    queryKey: ["search-market", marketTerm],
    queryFn: () => api.searchMarket(marketTerm),
    enabled: marketTerm.length >= 2,
    staleTime: 5 * 60_000,
  });

  const taken = new Set(selected);
  const suggestions = [
    ...(local.data?.assets ?? []).map((a) => ({ ticker: a.ticker, name: a.name, origin: "carteira" as const })),
    ...(market.data?.items ?? []).map((h) => ({ ticker: h.ticker, name: h.name, origin: "mercado" as const })),
  ]
    .filter((item, index, all) => all.findIndex((other) => other.ticker === item.ticker) === index)
    .filter((item) => !taken.has(item.ticker))
    .slice(0, 8);

  const pick = (ticker: string) => {
    onAdd(ticker.toUpperCase());
    setTerm("");
    inputRef.current?.focus();
  };

  return (
    <div className="relative min-w-[240px] max-w-sm flex-1">
      <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted" />
      <input
        ref={inputRef}
        value={term}
        onChange={(event) => setTerm(event.target.value)}
        onFocus={() => setFocused(true)}
        // Delayed so a click on a suggestion still lands before the list hides.
        onBlur={() => setTimeout(() => setFocused(false), 150)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && term.trim()) {
            event.preventDefault();
            pick(suggestions[0]?.ticker ?? term.trim());
          }
        }}
        placeholder={selected.length ? "Adicionar outro ativo…" : "Buscar ativo (PETR4, HGLG11, AAPL…)"}
        className="input pl-9"
        disabled={selected.length >= MAX_TICKERS}
      />
      {focused && term.trim().length >= 2 && suggestions.length ? (
        <div className="absolute left-0 right-0 top-full z-20 mt-1 overflow-hidden rounded-xl border border-line-strong bg-surface-raised shadow-raised">
          {suggestions.map((item) => (
            <button
              key={item.ticker}
              type="button"
              // onMouseDown fires before the input's blur, keeping the click alive.
              onMouseDown={(event) => {
                event.preventDefault();
                pick(item.ticker);
              }}
              className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm transition-colors hover:bg-surface-hover"
            >
              <span className="min-w-0">
                <span className="font-medium text-ink">{item.ticker}</span>
                <span className="ml-2 truncate text-xs text-ink-muted">{item.name}</span>
              </span>
              {item.origin === "mercado" ? <Badge>mercado</Badge> : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** One comparable fact: how to read it from the data and which way is "best". */
type Metric = {
  label: string;
  hint?: string;
  value: (column: Column) => number | undefined;
  format: (value: number, column: Column) => string;
  /** Direction that deserves the highlight; omitted = no ranking (context numbers). */
  best?: "high" | "low";
};

type Column = {
  ticker: string;
  detail: AssetDetailData | undefined;
  fundamentals: Fundamentals | null | undefined;
  rangeChangePct: number | undefined;
};

const METRICS: Metric[] = [
  {
    label: "Variação no período",
    hint: "fechamentos do intervalo do gráfico",
    value: (c) => c.rangeChangePct,
    format: (v) => percent(v, 1, true),
    best: "high",
  },
  {
    label: "Valor de mercado",
    value: (c) => c.fundamentals?.market_cap,
    format: (v, c) => money(v, { currency: c.fundamentals?.currency ?? c.detail?.currency, compact: true, decimals: 0 }),
  },
  { label: "P/L", value: (c) => c.fundamentals?.pe_trailing, format: (v) => decimal(v, 2), best: "low" },
  { label: "P/VP", value: (c) => c.fundamentals?.price_to_book, format: (v) => decimal(v, 2), best: "low" },
  {
    label: "Dividend yield",
    value: (c) => c.fundamentals?.dividend_yield,
    format: (v) => percent(v, 2),
    best: "high",
  },
  { label: "Payout", value: (c) => c.fundamentals?.payout_ratio, format: (v) => percent(v, 0) },
  { label: "ROE", value: (c) => c.fundamentals?.return_on_equity, format: (v) => percent(v, 1), best: "high" },
  {
    label: "Margem líquida",
    value: (c) => c.fundamentals?.profit_margin,
    format: (v) => percent(v, 1),
    best: "high",
  },
  {
    label: "Crescimento da receita",
    hint: "ano contra ano",
    value: (c) => c.fundamentals?.revenue_growth,
    format: (v) => percent(v, 1, true),
    best: "high",
  },
  {
    label: "Dívida / patrimônio",
    value: (c) => c.fundamentals?.debt_to_equity,
    format: (v) => decimal(v, 1),
    best: "low",
  },
  { label: "Beta", value: (c) => c.fundamentals?.beta, format: (v) => decimal(v, 2) },
  {
    label: "Upside até o preço-alvo",
    hint: "consenso dos analistas sobre a cotação atual",
    value: (c) => {
      const target = c.fundamentals?.target_mean_price;
      const price = Number(c.detail?.current_price);
      return target !== undefined && price > 0 ? (target / price - 1) * 100 : undefined;
    },
    format: (v) => percent(v, 1, true),
    best: "high",
  },
];

export default function Comparador() {
  const [params, setParams] = useSearchParams();
  const tickers = useMemo(
    () =>
      [...new Set((params.get("ativos") ?? "").toUpperCase().split(",").filter(Boolean))].slice(0, MAX_TICKERS),
    [params],
  );
  const setTickers = (next: string[]) =>
    setParams(next.length ? { ativos: next.join(",") } : {}, { replace: true });

  const [range, setRange] = useState<Range>("1y");

  const details = useQueries({
    queries: tickers.map((ticker) => ({
      queryKey: ["asset", ticker],
      queryFn: () => api.asset(ticker),
      retry: 1,
    })),
  });
  const fundamentals = useQueries({
    queries: tickers.map((ticker) => ({
      queryKey: ["fundamentals", ticker],
      queryFn: () => api.assetFundamentals(ticker),
      staleTime: 60 * 60_000,
    })),
  });
  const prices = useQueries({
    queries: tickers.map((ticker) => ({
      queryKey: ["asset-prices", ticker],
      queryFn: () => api.assetPriceHistory(ticker),
    })),
  });

  const cutIso = useMemo(() => {
    const months = RANGE_MONTHS[range];
    if (months === null) return "";
    const cut = new Date();
    cut.setMonth(cut.getMonth() - months);
    return `${cut.getFullYear()}-${String(cut.getMonth() + 1).padStart(2, "0")}-${String(cut.getDate()).padStart(2, "0")}`;
  }, [range]);

  // Every series rebased to 0% at its first close inside the range, merged by
  // date and forward-filled: B3 and US holidays differ, and a hole every
  // mismatched day would shred the lines.
  const { chartData, chartSeries, rangeChange } = useMemo(() => {
    const series: ReturnSeries[] = [];
    const rangeChangeByTicker = new Map<string, number>();
    const byDate = new Map<string, ReturnPoint>();
    const lastSeen = new Map<string, number>();
    const allDates = new Set<string>();
    const rebased = tickers.map((ticker, index) => {
      const visible = (prices[index].data ?? []).filter((point) => point.date >= cutIso);
      const base = visible[0]?.close;
      const points = base
        ? visible.map((point) => ({ date: point.date, pct: (point.close / base - 1) * 100 }))
        : [];
      if (points.length > 1) rangeChangeByTicker.set(ticker, points[points.length - 1].pct);
      for (const point of points) allDates.add(point.date);
      if (points.length) {
        series.push({ key: ticker, label: ticker, color: SERIES[index % SERIES.length] });
      }
      return { ticker, points: new Map(points.map((point) => [point.date, point.pct])) };
    });
    for (const date of [...allDates].sort()) {
      const row: ReturnPoint = { date };
      for (const { ticker, points } of rebased) {
        const value = points.get(date) ?? lastSeen.get(ticker);
        if (value !== undefined) {
          row[ticker] = value;
          lastSeen.set(ticker, value);
        }
      }
      byDate.set(date, row);
    }
    return { chartData: [...byDate.values()], chartSeries: series, rangeChange: rangeChangeByTicker };
  }, [tickers, prices, cutIso]);

  const columns: Column[] = tickers.map((ticker, index) => ({
    ticker,
    detail: details[index].data,
    fundamentals: fundamentals[index].data?.supported ? fundamentals[index].data?.data : null,
    rangeChangePct: rangeChange.get(ticker),
  }));

  const remove = (ticker: string) => setTickers(tickers.filter((item) => item !== ticker));

  // A metric row is worth ranking only when two assets actually answer it.
  const bestValue = (metric: Metric): number | undefined => {
    if (!metric.best) return undefined;
    const values = columns.map((column) => metric.value(column)).filter((v): v is number => v !== undefined);
    if (values.length < 2) return undefined;
    return metric.best === "high" ? Math.max(...values) : Math.min(...values);
  };

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">Mercado</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Comparador de ativos</h1>
        <p className="mt-2 max-w-2xl text-sm text-ink-secondary">
          Compare até {MAX_TICKERS} ativos lado a lado: desempenho, valuation e dividendos. Vale
          para qualquer papel da B3 ou dos EUA, esteja na carteira ou não.
        </p>
      </header>

      {/* z-30: the suggestion dropdown must float over the sibling cards below,
          and each Card is its own stacking context — the picker's card has to
          win as a whole. */}
      <Card className="relative z-30 p-5">
        <div className="flex flex-wrap items-center gap-3">
          <TickerPicker selected={tickers} onAdd={(ticker) => setTickers([...tickers, ticker])} />
          {columns.map((column, index) => (
            <span
              key={column.ticker}
              className="inline-flex items-center gap-2 rounded-full border border-line bg-surface-raised py-1.5 pl-3 pr-1.5 text-sm font-medium"
            >
              <span className="h-2 w-2 rounded-full" style={{ background: SERIES[index % SERIES.length] }} aria-hidden />
              {column.ticker}
              {details[index].isError ? <Badge tone="warning">não encontrado</Badge> : null}
              <button
                type="button"
                onClick={() => remove(column.ticker)}
                className="btn-ghost rounded-full p-1"
                aria-label={`Remover ${column.ticker}`}
              >
                <X size={13} />
              </button>
            </span>
          ))}
          {tickers.length < MAX_TICKERS && tickers.length > 0 ? (
            <span className="inline-flex items-center gap-1 text-xs text-ink-muted">
              <Plus size={13} aria-hidden /> até {MAX_TICKERS} ativos
            </span>
          ) : null}
        </div>
      </Card>

      {!tickers.length ? (
        <EmptyState
          icon={GitCompareArrows}
          title="Escolha os ativos para comparar"
          description="Busque acima por ticker ou nome, por exemplo PETR4 e PRIO3, ou HGLG11 e XPML11. Ativos fora da carteira também valem."
        />
      ) : (
        <>
          <ChartFrame
            title="Desempenho no período"
            subtitle="Variação percentual dos fechamentos, todos partindo de zero"
            height={340}
            action={
              <Segmented
                size="sm"
                value={range}
                onChange={setRange}
                options={[
                  { value: "6m", label: "6M" },
                  { value: "1y", label: "1A" },
                  { value: "5y", label: "5A" },
                  { value: "max", label: "Máx" },
                ]}
              />
            }
            footer={
              chartData.length ? (
                <span>
                  {shortDate(String(chartData[0]?.date))} → {shortDate(String(chartData[chartData.length - 1]?.date))}
                  {" · "}moedas diferentes comparam variação, não preço
                </span>
              ) : undefined
            }
          >
            {prices.some((query) => query.isLoading) ? (
              <Skeleton className="h-full w-full" />
            ) : (
              <ReturnLinesChart data={chartData} series={chartSeries} height={340} />
            )}
          </ChartFrame>

          <Card className="p-5">
            <SectionTitle
              title="Fundamentos lado a lado"
              subtitle="Melhor valor de cada linha em destaque · fonte: Yahoo Finance"
            />
            <div className="-mx-2 overflow-x-auto">
              {/* Fixed column widths: with two assets on a wide screen, fluid
                  columns drift half a page apart and the eye loses the row. */}
              <table className="table-fixed text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wide text-ink-muted">
                    <th className="w-[210px] px-3 py-2">Indicador</th>
                    {columns.map((column, index) => (
                      <th key={column.ticker} className="w-[190px] px-3 py-2">
                        <Link to={`/ativos/${column.ticker}`} className="group inline-block normal-case">
                          <span className="flex items-center gap-1.5 text-sm font-semibold text-ink group-hover:text-accent">
                            <span
                              className="h-2 w-2 rounded-full"
                              style={{ background: SERIES[index % SERIES.length] }}
                              aria-hidden
                            />
                            {column.ticker}
                          </span>
                          <span className="mt-0.5 flex items-center gap-1.5">
                            {column.detail ? <KindTag kind={column.detail.kind} /> : null}
                            {column.detail?.held === false ? (
                              <span className="text-[10px] font-normal text-ink-muted">fora da carteira</span>
                            ) : column.detail ? (
                              <span className="text-[10px] font-normal text-accent">na carteira</span>
                            ) : null}
                          </span>
                        </Link>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  <tr className="table-row border-t border-line/60">
                    <td className="px-3 py-2 text-[13px] text-ink-muted">Cotação atual</td>
                    {columns.map((column) => (
                      <td key={column.ticker} className="tnum px-3 py-2 font-medium">
                        {column.detail
                          ? money(column.detail.current_price, { currency: column.detail.currency })
                          : "…"}
                      </td>
                    ))}
                  </tr>
                  {METRICS.map((metric) => {
                    const best = bestValue(metric);
                    return (
                      <tr key={metric.label} className="table-row border-t border-line/60">
                        <td className="px-3 py-2 text-[13px] text-ink-muted">
                          {metric.label}
                          {metric.hint ? (
                            <span className="mt-0.5 block text-[10px] leading-snug">{metric.hint}</span>
                          ) : null}
                        </td>
                        {columns.map((column) => {
                          const value = metric.value(column);
                          const isBest = best !== undefined && value === best;
                          return (
                            <td
                              key={column.ticker}
                              className={clsx(
                                "tnum px-3 py-2",
                                isBest ? "font-semibold text-ink" : "text-ink-secondary",
                              )}
                            >
                              <span className="inline-flex items-center gap-1.5">
                                {value === undefined ? "-" : metric.format(value, column)}
                                {isBest ? (
                                  <Trophy
                                    size={13}
                                    className="shrink-0 text-warning"
                                    aria-label="Melhor da linha"
                                  />
                                ) : null}
                              </span>
                            </td>
                          );
                        })}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="mt-3 text-xs text-ink-muted">
              “Melhor” segue a leitura usual de cada indicador (P/L e dívida menores, ROE e yield
              maiores); contexto e setor importam mais que qualquer linha isolada.
            </p>
          </Card>
        </>
      )}
    </div>
  );
}
