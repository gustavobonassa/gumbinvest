/**
 * Rentabilidade — where the portfolio's result came from and how it got there.
 *
 * Everything on this page is a *result*: the accumulated curve, the monthly
 * returns, the per-class contribution and the per-asset ranking. Allocation and
 * income live on their own pages; mixing "how much do I have" into a page about
 * "how much have I made" is what made the old Relatórios screen unreadable.
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { Trophy } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import {
  ChartFrame,
  ReturnLinesChart,
  ReturnsBars,
  type ReturnPoint,
  type ReturnSeries,
} from "@/components/charts";
import { Card, ErrorState, KindTag, SectionTitle, Segmented, Skeleton } from "@/components/ui";
import { api, type PerformanceRow, type PerformanceWindow, type ProfitRange } from "@/lib/api";
import { BENCHMARK_STYLE, OTHER_COLOR, SERIES, TOKENS, kindColor, kindRank } from "@/lib/colors";
import { kindLabel, money, percent, periodLabel } from "@/lib/format";

const RANGE_LABELS: Record<ProfitRange, string> = {
  "6m": "6M",
  "1y": "1A",
  "2y": "2A",
  "5y": "5A",
  max: "Tudo",
};
const RANGES: ProfitRange[] = ["6m", "1y", "2y", "max"];
/** Months each range covers. `null` = everything (used to slice local series). */
const RANGE_MONTHS: Record<ProfitRange, number | null> = {
  "6m": 6,
  "1y": 12,
  "2y": 24,
  "5y": 60,
  max: null,
};

type Mode = "total" | "kind";
const MODE_LABELS: Record<Mode, string> = { total: "Total", kind: "Classe" };
const MODE_HINTS: Record<Mode, string> = {
  total: "Quanto o seu dinheiro rendeu, contra o mercado",
  kind: "O mesmo retorno, por classe de ativo",
};

/** The portfolio's own line. Not a class, so it needs a key of its own. */
const PORTFOLIO = "__portfolio__";

const BENCHMARK_LABELS: Record<string, string> = { IBOV: "Ibovespa", CDI: "CDI" };

const WINDOWS: PerformanceWindow[] = ["day", "1m", "6m", "1y", "total"];
const WINDOW_LABELS: Record<PerformanceWindow, string> = {
  day: "Dia",
  "1m": "1M",
  "3m": "3M",
  "6m": "6M",
  "1y": "1A",
  total: "Total",
};
const WINDOW_HINTS: Record<PerformanceWindow, string> = {
  day: "Último pregão: na segunda-feira e no fim de semana, o de sexta",
  "1m": "Resultado dos últimos 30 dias",
  "3m": "Resultado dos últimos 3 meses",
  "6m": "Resultado dos últimos 6 meses",
  "1y": "Resultado dos últimos 12 meses",
  total: "Resultado desde a primeira compra",
};

/** One line of a ranking: position, ticker, class, result and return. */
function PerformerRow({ asset, index, highlight }: { asset: PerformanceRow; index: number; highlight: boolean }) {
  const positive = asset.window_change >= 0;
  return (
    <li className="flex items-center justify-between gap-3 rounded-xl px-2 py-2 hover:bg-surface-hover">
      <span className="flex min-w-0 items-center gap-3">
        <span
          className={clsx(
            "grid h-7 w-7 shrink-0 place-items-center rounded-lg text-xs font-semibold",
            highlight ? "bg-positive/15 text-positive" : "bg-surface-hover text-ink-muted",
          )}
        >
          {highlight ? <Trophy size={14} /> : index + 1}
        </span>
        <span className="min-w-0">
          {/* Treasury tickers run to forty characters; the row must not grow to
              fit one, or the ranking loses its shape. */}
          <Link
            to={`/ativos/${asset.ticker}`}
            title={asset.name || asset.ticker}
            className="block truncate font-medium hover:text-accent"
          >
            {asset.ticker}
          </Link>
          <span className="mt-0.5 block">
            <KindTag kind={asset.kind} />
          </span>
        </span>
      </span>
      <span className="shrink-0 text-right">
        <span className={clsx("tnum block font-medium", positive ? "text-positive" : "text-negative")}>
          {money(asset.window_change)}
        </span>
        <span className="tnum block text-xs text-ink-muted">
          {asset.window_pct === null ? "-" : percent(asset.window_pct, 1, true)}
        </span>
      </span>
    </li>
  );
}

function PerformerList({
  title,
  subtitle,
  rows,
  loading,
  crown,
}: {
  title: string;
  subtitle: string;
  rows: PerformanceRow[] | undefined;
  loading: boolean;
  crown: boolean;
}) {
  return (
    <Card className="p-5">
      <SectionTitle title={title} subtitle={subtitle} />
      {loading && !rows ? (
        <Skeleton className="h-56 w-full" />
      ) : !rows?.length ? (
        <p className="py-8 text-center text-sm text-ink-muted">Nada a mostrar neste período.</p>
      ) : (
        <ul className="space-y-2">
          {rows.map((asset, index) => (
            <PerformerRow
              key={asset.ticker}
              asset={asset}
              index={index}
              highlight={crown && index === 0 && asset.window_change > 0}
            />
          ))}
        </ul>
      )}
    </Card>
  );
}

export default function Reports() {
  const [range, setRange] = useState<ProfitRange>("1y");
  const [mode, setMode] = useState<Mode>("total");
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [returnsRange, setReturnsRange] = useState<ProfitRange>("1y");
  const [perfWindow, setPerfWindow] = useState<PerformanceWindow>("total");

  // `keepPreviousData` on every control-driven query: switching a range or a
  // grouping must redraw the one widget that changed, not blank the page.
  const profit = useQuery({
    queryKey: ["profit-history", range, mode],
    queryFn: () => api.profitHistory(range, mode),
    placeholderData: keepPreviousData,
  });
  const performers = useQuery({
    queryKey: ["performers", perfWindow],
    queryFn: () => api.performers(perfWindow, 6),
    placeholderData: keepPreviousData,
  });
  const returns = useQuery({ queryKey: ["monthly-returns"], queryFn: api.monthlyReturns });
  // Same key the Ativos page uses, so the two share one cached fetch.
  const positions = useQuery({ queryKey: ["assets"], queryFn: () => api.assets(true) });
  const reports = useQuery({ queryKey: ["reports"], queryFn: api.reports });

  // The lines and the rows that feed them, built together because they have to
  // agree on every key. Classes come back already bucketed into the eight
  // palette slots plus "Outros" (a return cannot be merged after the fact, so
  // the backend forms that bucket before chaining it) and are ordered by
  // `KIND_ORDER`: only adjacent slots are guaranteed to separate, so the order
  // is not cosmetic. The benchmarks are appended last, in every mode.
  const { series, chartRows } = useMemo(() => {
    const points = profit.data ?? [];
    const lines: ReturnSeries[] =
      mode === "kind"
        ? [...new Set(points.flatMap((point) => Object.keys(point.kinds_pct)))]
            .sort((a, b) => kindRank(a) - kindRank(b))
            .map((kind) => ({ key: kind, label: kindLabel(kind), color: kindColor(kind) }))
        : [{ key: PORTFOLIO, label: "Minha carteira", color: SERIES[0], emphasis: true }];

    const codes = [...new Set(points.flatMap((point) => Object.keys(point.benchmarks)))].sort();
    for (const code of codes) {
      const style = BENCHMARK_STYLE[code];
      lines.push({
        key: code,
        label: BENCHMARK_LABELS[code] ?? code,
        color: style?.color ?? OTHER_COLOR,
        dash: style?.dash,
      });
    }

    const rows: ReturnPoint[] = points.map((point) => {
      const row: ReturnPoint = { date: point.date };
      if (mode === "kind") {
        for (const [kind, value] of Object.entries(point.kinds_pct)) row[kind] = Number(value);
      } else {
        row[PORTFOLIO] = Number(point.return_pct);
      }
      for (const [code, value] of Object.entries(point.benchmarks)) row[code] = Number(value);
      return row;
    });
    return { series: lines, chartRows: rows };
  }, [profit.data, mode]);

  // Switching the axis rebuilds the keys, so old hidden keys mean nothing.
  useEffect(() => setHidden(new Set()), [mode]);
  const toggleKind = (key: string) =>
    setHidden((current) => {
      const next = new Set(current);
      if (!next.delete(key)) next.add(key);
      return next;
    });

  const monthly = useMemo(() => {
    const all = returns.data ?? [];
    const months = RANGE_MONTHS[returnsRange];
    return months === null ? all : all.slice(-months);
  }, [returns.data, returnsRange]);

  /** Consistency, read off the same months the bars show. */
  const consistency = useMemo(() => {
    const rows = monthly.filter((row) => row.return_pct !== 0 || row.profit !== 0);
    if (!rows.length) return null;
    const positive = rows.filter((row) => row.profit > 0);
    const best = rows.reduce((top, row) => (row.return_pct > top.return_pct ? row : top));
    const worst = rows.reduce((low, row) => (row.return_pct < low.return_pct ? row : low));
    return {
      months: rows.length,
      positive: positive.length,
      hitRate: (positive.length / rows.length) * 100,
      best,
      worst,
      averagePct: rows.reduce((sum, row) => sum + row.return_pct, 0) / rows.length,
      totalProfit: rows.reduce((sum, row) => sum + row.profit, 0),
    };
  }, [monthly]);

  /**
   * Result per class, from every position ever held — so the percentage is
   * measured against what was put in over those lives, not against the cost
   * still open. A class that sold its winners has a small remaining cost and a
   * large result, and dividing one by the other reports a return nobody made.
   */
  const byKind = useMemo(() => {
    const buckets = new Map<string, { kind: string; result: number; cost: number; assets: number }>();
    for (const row of positions.data ?? []) {
      const bucket = buckets.get(row.kind) ?? { kind: row.kind, result: 0, cost: 0, assets: 0 };
      bucket.result += Number(row.total_return_base);
      bucket.cost += Number(row.invested_base);
      bucket.assets += 1;
      buckets.set(row.kind, bucket);
    }
    const rows = [...buckets.values()].filter((row) => Math.abs(row.result) > 0.01);
    rows.sort((a, b) => b.result - a.result);
    const peak = Math.max(...rows.map((row) => Math.abs(row.result)), 1);
    return rows.map((row) => ({
      ...row,
      share: (Math.abs(row.result) / peak) * 100,
      pct: row.cost > 1 ? (row.result / row.cost) * 100 : null,
    }));
  }, [positions.data]);

  // Renda fixa has no daily close on file, so it sits outside the percentage.
  // Saying which share of the carteira the line covers is the difference
  // between a caveat and a wrong number.
  const points = profit.data ?? [];
  const priced = Number(points[points.length - 1]?.priced_share ?? 100);
  const coverageNote = priced >= 99 ? "" : ` · cobre ${Math.round(priced)}% da carteira`;

  /**
   * The plotted line is money-weighted: each aporte counts for the time it was
   * actually invested, so a bad year holding almost nothing costs almost
   * nothing. The size-blind figure is the one an index is a fair comparison
   * for, so it is quoted underneath rather than dropped.
   */
  const outcome = useMemo(() => {
    if (points.length < 2) return null;
    const last = points[points.length - 1];
    return { result: Number(last.profit) - Number(points[0].profit), twr: Number(last.twr_pct) };
  }, [points]);

  if (profit.isError) return <ErrorState error={profit.error} retry={() => profit.refetch()} />;

  const annual = reports.data?.annual ?? [];

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">Análises</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Rentabilidade</h1>
        <p className="mt-2 max-w-2xl text-sm text-ink-secondary">
          O resultado da carteira: quanto ela já rendeu, em que meses rendeu e quais classes e ativos
          entregaram esse resultado.
        </p>
      </header>

      <ChartFrame
        title="Rentabilidade acumulada"
        subtitle={`${MODE_HINTS[mode]}${coverageNote}`}
        height={360}
        action={
          <>
            <Segmented
              size="sm"
              value={range}
              onChange={setRange}
              options={RANGES.map((value) => ({ value, label: RANGE_LABELS[value] }))}
            />
            <Segmented
              size="sm"
              value={mode}
              onChange={setMode}
              options={(Object.keys(MODE_LABELS) as Mode[]).map((value) => ({
                value,
                label: MODE_LABELS[value],
              }))}
            />
          </>
        }
        footer={
          outcome ? (
            <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
              <span className="max-w-xl">
                A linha pondera cada aporte pelo tempo em que esteve investido. R$ 400 mil que
                entraram no mês passado valem um mês, não seis anos.
              </span>
              <span className="sm:ml-auto">
                No período:{" "}
                <span className={clsx("tnum font-medium", outcome.result >= 0 ? "text-positive" : "text-negative")}>
                  {money(outcome.result)}
                </span>{" "}
                de resultado · ignorando o tamanho da carteira,{" "}
                <span className="tnum text-ink-secondary">{percent(outcome.twr, 1, true)}</span>
              </span>
            </div>
          ) : null
        }
        table={
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-surface text-left text-xs uppercase tracking-wide text-ink-muted">
              <tr>
                <th className="px-2 py-2">Data</th>
                {series.map((item) => (
                  <th key={item.key} className="px-2 py-2 text-right">
                    {item.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {[...chartRows].reverse().map((point) => (
                <tr key={point.date} className="table-row">
                  <td className="px-2 py-1.5">{point.date}</td>
                  {series.map((item) => (
                    <td key={item.key} className="tnum px-2 py-1.5 text-right text-ink-secondary">
                      {point[item.key] === undefined ? "-" : percent(Number(point[item.key]), 2, true)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        }
      >
        {profit.isLoading && !profit.data ? (
          <Skeleton className="h-full w-full" />
        ) : (
          <ReturnLinesChart
            data={chartRows}
            series={series}
            hidden={hidden}
            onToggle={toggleKind}
            height={360}
          />
        )}
      </ChartFrame>

      <ChartFrame
        title="Retorno mensal"
        subtitle="Variação do patrimônio ajustada pelos aportes de cada mês"
        height={280}
        action={
          <Segmented
            size="sm"
            value={returnsRange}
            onChange={setReturnsRange}
            options={RANGES.map((value) => ({ value, label: RANGE_LABELS[value] }))}
          />
        }
        table={
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-surface text-left text-xs uppercase tracking-wide text-ink-muted">
              <tr>
                <th className="px-2 py-2">Mês</th>
                <th className="px-2 py-2 text-right">Aporte</th>
                <th className="px-2 py-2 text-right">Proventos</th>
                <th className="px-2 py-2 text-right">Resultado</th>
                <th className="px-2 py-2 text-right">Retorno</th>
              </tr>
            </thead>
            <tbody>
              {[...monthly].reverse().map((row) => (
                <tr key={row.period} className="table-row">
                  <td className="px-2 py-1.5">{periodLabel(row.period)}</td>
                  <td className="tnum px-2 py-1.5 text-right text-ink-secondary">{money(row.flow)}</td>
                  <td className="tnum px-2 py-1.5 text-right text-ink-secondary">{money(row.income)}</td>
                  <td
                    className={clsx(
                      "tnum px-2 py-1.5 text-right font-medium",
                      row.profit >= 0 ? "text-positive" : "text-negative",
                    )}
                  >
                    {money(row.profit)}
                  </td>
                  <td className="tnum px-2 py-1.5 text-right">{percent(row.return_pct, 2, true)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        }
      >
        {returns.isLoading ? <Skeleton className="h-full w-full" /> : <ReturnsBars data={monthly} height={280} />}
      </ChartFrame>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card className="p-5">
          <SectionTitle
            title="Resultado por classe"
            subtitle="Quanto cada classe já entregou: mercado, realizado e proventos"
          />
          {positions.isLoading ? (
            <Skeleton className="h-56 w-full" />
          ) : !byKind.length ? (
            <p className="py-8 text-center text-sm text-ink-muted">Sem resultado apurado.</p>
          ) : (
            <ul className="max-h-[420px] space-y-3 overflow-auto pr-1">
              {byKind.map((row) => (
                <li key={row.kind}>
                  <div className="flex items-baseline justify-between gap-3 text-sm">
                    <KindTag kind={row.kind} />
                    <span className="flex items-baseline gap-2">
                      <span
                        className={clsx(
                          "tnum font-medium",
                          row.result >= 0 ? "text-positive" : "text-negative",
                        )}
                      >
                        {money(row.result)}
                      </span>
                      <span className="tnum text-xs text-ink-muted">
                        {row.pct === null ? "-" : percent(row.pct, 1, true)}
                      </span>
                    </span>
                  </div>
                  {/* The bar repeats the number beside it — it is there to make
                      the ranking scannable, not to be read off. */}
                  <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-surface-raised">
                    <div
                      className="h-full rounded-full"
                      style={{
                        width: `${Math.max(row.share, 1)}%`,
                        background: row.result >= 0 ? TOKENS.positive : TOKENS.negative,
                      }}
                    />
                  </div>
                  <p className="mt-1 text-xs text-ink-muted">
                    {row.assets} ativo{row.assets === 1 ? "" : "s"}
                    {row.cost > 1 ? ` · ${money(row.cost, { compact: true })} aplicados` : ""}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card className="p-5">
          <SectionTitle
            title="Consistência mensal"
            subtitle={`Os ${RANGE_LABELS[returnsRange].toLowerCase()} do gráfico de retorno mensal`}
          />
          {/* `consistency` is null both while loading AND for a portfolio with
              no months of history — only the first case is a skeleton, or a
              new portfolio shimmers forever. */}
          {returns.isLoading ? (
            <Skeleton className="h-56 w-full" />
          ) : returns.isError ? (
            <ErrorState error={returns.error} retry={() => returns.refetch()} />
          ) : !consistency ? (
            <p className="py-6 text-center text-sm text-ink-muted">
              Sem meses fechados para medir. Importe transações e aguarde o primeiro mês.
            </p>
          ) : (
            <div className="space-y-4">
              <div>
                <div className="flex items-baseline justify-between gap-3">
                  <span className="text-sm text-ink-secondary">Meses positivos</span>
                  <span className="tnum text-sm font-medium">
                    {consistency.positive} de {consistency.months}
                    <span className="ml-2 text-ink-muted">{percent(consistency.hitRate, 0)}</span>
                  </span>
                </div>
                <div className="mt-2 h-2 overflow-hidden rounded-full bg-surface-raised">
                  <div
                    className="h-full rounded-full"
                    style={{ width: `${consistency.hitRate}%`, background: TOKENS.positive }}
                  />
                </div>
              </div>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
                <div>
                  <dt className="text-ink-muted">Melhor mês</dt>
                  <dd className="tnum mt-0.5 font-medium text-positive">
                    {percent(consistency.best.return_pct, 2, true)}
                  </dd>
                  <dd className="text-xs text-ink-muted">{periodLabel(consistency.best.period)}</dd>
                </div>
                <div>
                  <dt className="text-ink-muted">Pior mês</dt>
                  <dd className="tnum mt-0.5 font-medium text-negative">
                    {percent(consistency.worst.return_pct, 2, true)}
                  </dd>
                  <dd className="text-xs text-ink-muted">{periodLabel(consistency.worst.period)}</dd>
                </div>
                <div>
                  <dt className="text-ink-muted">Retorno médio</dt>
                  <dd className="tnum mt-0.5 font-medium">{percent(consistency.averagePct, 2, true)}</dd>
                  <dd className="text-xs text-ink-muted">por mês</dd>
                </div>
                <div>
                  <dt className="text-ink-muted">Resultado no período</dt>
                  <dd
                    className={clsx(
                      "tnum mt-0.5 font-medium",
                      consistency.totalProfit >= 0 ? "text-positive" : "text-negative",
                    )}
                  >
                    {money(consistency.totalProfit)}
                  </dd>
                  <dd className="text-xs text-ink-muted">soma dos meses</dd>
                </div>
              </dl>
            </div>
          )}
        </Card>
      </div>

      <section className="space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold tracking-tight text-ink">Desempenho por ativo</h2>
            <p className="mt-0.5 text-sm text-ink-muted">{WINDOW_HINTS[perfWindow]}</p>
          </div>
          <Segmented
            size="sm"
            value={perfWindow}
            onChange={setPerfWindow}
            options={WINDOWS.map((value) => ({ value, label: WINDOW_LABELS[value] }))}
          />
        </div>
        <div className="grid gap-4 xl:grid-cols-2">
          <PerformerList
            title="Melhores desempenhos"
            subtitle={perfWindow === "day" ? "Quem mais subiu no último pregão" : "Onde a carteira mais ganhou"}
            rows={performers.data?.best}
            loading={performers.isLoading}
            crown
          />
          <PerformerList
            title="Piores desempenhos"
            subtitle={perfWindow === "day" ? "Quem mais caiu no último pregão" : "Onde a carteira mais perdeu"}
            rows={performers.data?.worst}
            loading={performers.isLoading}
            crown={false}
          />
        </div>
      </section>

      <Card className="p-5">
        <SectionTitle title="Desempenho anual" subtitle="Capital aplicado, vendas e proventos por ano" />
        {reports.isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : (
          <div className="-mx-2 overflow-x-auto">
            <table className="w-full min-w-[620px] text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-ink-muted">
                <tr>
                  <th className="px-3 pb-2">Ano</th>
                  <th className="px-3 pb-2 text-right">Compras</th>
                  <th className="px-3 pb-2 text-right">Vendas</th>
                  <th className="px-3 pb-2 text-right">Proventos</th>
                  <th className="px-3 pb-2 text-right">Movimentos</th>
                  <th className="px-3 pb-2 text-right">Patrimônio ao fim</th>
                </tr>
              </thead>
              <tbody>
                {annual.map((row) => (
                  <tr key={row.year} className="table-row">
                    <td className="px-3 py-2 font-medium">{row.year}</td>
                    <td className="tnum px-3 py-2 text-right">{money(row.bought)}</td>
                    <td className="tnum px-3 py-2 text-right">{money(row.sold)}</td>
                    <td className="tnum px-3 py-2 text-right text-positive">{money(row.income)}</td>
                    <td className="tnum px-3 py-2 text-right text-ink-muted">{row.transactions}</td>
                    <td className="tnum px-3 py-2 text-right">{money(row.market_value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
