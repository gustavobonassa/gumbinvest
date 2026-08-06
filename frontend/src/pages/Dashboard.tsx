/** Portfolio overview: headline metrics, evolution, allocation and income. */
import { useQuery } from "@tanstack/react-query";
import { Activity, AlertTriangle, ArrowRight, Coins, TrendingUp, Wallet } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import {
  AllocationDonut,
  ChartFrame,
  kindSlices,
  ContributionBars,
  IncomeBars,
  PortfolioHistoryChart,
  ProfitByAssetChart,
  ReturnsBars,
} from "@/components/charts";
import { Badge, Card, EmptyState, ErrorState, SectionTitle, Segmented, Skeleton, StatTile } from "@/components/ui";
import { api } from "@/lib/api";
import { withOther } from "@/lib/colors";
import { kindLabel, money, opLabel, percent, periodLabel, shortDate } from "@/lib/format";

type Range = "6m" | "1y" | "2y" | "5y" | "max";
type Grouping = "asset" | "kind" | "broker";

export default function Dashboard() {
  const [range, setRange] = useState<Range>("max");
  const [grouping, setGrouping] = useState<Grouping>("kind");
  const [allocationMode, setAllocationMode] = useState<"percent" | "value">("percent");

  // Focus refetching is off globally; the interval keeps the headline numbers
  // from freezing at whatever the page loaded with.
  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview, refetchInterval: 5 * 60_000 });
  const history = useQuery({ queryKey: ["history", range], queryFn: () => api.history(range) });
  const allocation = useQuery({ queryKey: ["allocation", grouping], queryFn: () => api.allocation(grouping) });
  const income = useQuery({ queryKey: ["income", "month"], queryFn: () => api.income("month") });
  const contributions = useQuery({ queryKey: ["contributions"], queryFn: () => api.contributions("month") });
  const returns = useQuery({ queryKey: ["monthly-returns"], queryFn: api.monthlyReturns });
  const positions = useQuery({ queryKey: ["positions"], queryFn: () => api.positions(false) });
  const movers = useQuery({ queryKey: ["performers", "day"], queryFn: () => api.performers("day", 4) });
  const recentIncome = useQuery({ queryKey: ["dividend-calendar", "recent"], queryFn: () => api.dividendCalendar(8) });

  if (overview.isError) return <ErrorState error={overview.error} retry={() => overview.refetch()} />;

  const data = overview.data;
  const hasData = (data?.assets_tracked ?? 0) > 0;

  // Classes own their colour and their slot; assets and brokers are ranked, so
  // they take palette slots in order and roll the tail into "Outros".
  // Memoized on the query data: range/grouping toggles re-render everything,
  // and fresh array identities would force every Recharts tree to reconcile.
  const allocationData = useMemo(
    () =>
      grouping === "kind"
        ? kindSlices((allocation.data ?? []).map((slice) => ({ ...slice, kind: slice.key })))
        : withOther(
            (allocation.data ?? []).map((slice) => ({ ...slice, legend: slice.key })),
            8,
            (value, count, tail) => ({
              key: "__other__",
              label: `Outros (${count})`,
              legend: `Outros (${count})`,
              value,
              percent:
                ((value / Math.max((allocation.data ?? []).reduce((sum, item) => sum + item.value, 0), 1)) as number) *
                100,
              // The folded slices, so the tooltip can answer "outros o quê?".
              members: tail.map((item) => ({
                label: item.key,
                value: Number(item.value),
                percent: Number(item.percent),
              })),
            }),
          ),
    [allocation.data, grouping],
  );

  const profitRanking = useMemo(() => {
    const profitByAsset = [...(positions.data ?? [])]
      .map((position) => ({ ticker: position.ticker, value: position.unrealized_pnl }))
      .sort((a, b) => b.value - a.value);
    return [...profitByAsset.slice(0, 6), ...profitByAsset.slice(-4)].filter(
      (item, index, list) => list.findIndex((other) => other.ticker === item.ticker) === index,
    );
  }, [positions.data]);

  const incomeLast12 = useMemo(() => (income.data ?? []).slice(-24), [income.data]);
  const income12mTotal = useMemo(
    () => (income.data ?? []).slice(-12).reduce((sum, item) => sum + Number(item.total), 0),
    [income.data],
  );

  if (!hasData && !overview.isLoading) {
    return (
      <EmptyState
        icon={Wallet}
        title="Nenhuma transação importada"
        description="Importe o CSV de movimentação da B3 para começar a acompanhar sua carteira."
        action={
          <Link to="/importar" className="btn-primary">
            Importar CSV
          </Link>
        }
      />
    );
  }

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">Visão geral</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Sua carteira</h1>
      </header>

      {/* Headline numbers — a stat tile is the right form for a single value. */}
      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Patrimônio atual"
          value={money(data?.market_value ?? 0)}
          icon={Wallet}
          tone="accent"
          loading={overview.isLoading}
          delta={data?.day_change_pct}
          hint="hoje"
        />
        <StatTile
          label="Resultado total"
          value={money(data?.total_profit ?? 0)}
          icon={TrendingUp}
          tone={(data?.total_profit ?? 0) >= 0 ? "positive" : "negative"}
          delta={data?.total_profit_pct}
          hint="não realizado + realizado + proventos"
          loading={overview.isLoading}
        />
        <StatTile
          label="Proventos recebidos"
          value={money(data?.income_total ?? 0)}
          icon={Coins}
          tone="positive"
          loading={overview.isLoading}
          hint={`${money(income12mTotal, { compact: true })} nos últimos 12m`}
        />
        <StatTile
          label="Variação do dia"
          value={money(data?.day_change ?? 0)}
          icon={Activity}
          tone={(data?.day_change ?? 0) >= 0 ? "positive" : "negative"}
          delta={data?.day_change_pct}
          loading={overview.isLoading}
        />
      </section>

      {data?.unpriced_positions?.length ? (
        <Card className="flex flex-wrap items-center gap-3 border-warning/25 bg-warning/5 px-5 py-3.5 text-sm" hover={false}>
          <AlertTriangle size={16} className="text-warning" aria-hidden />
          <span className="text-ink-secondary">
            {data.unpriced_positions.length} ativo(s) sem cotação — avaliados pelo preço médio:
          </span>
          <span className="flex flex-wrap gap-1.5">
            {data.unpriced_positions.slice(0, 8).map((ticker) => (
              <Badge key={ticker} tone="warning">
                {ticker}
              </Badge>
            ))}
            {data.unpriced_positions.length > 8 ? <Badge tone="warning">+{data.unpriced_positions.length - 8}</Badge> : null}
          </span>
          <Link to="/configuracoes?aba=dados" className="ml-auto text-xs text-accent hover:underline">
            Resolver eventos corporativos
          </Link>
        </Card>
      ) : null}

      {/* Evolution */}
      <ChartFrame
        title="Evolução do patrimônio"
        subtitle="Valor de mercado comparado ao capital investido"
        height={320}
        loading={history.isLoading}
        error={history.isError ? history.error : undefined}
        retry={() => history.refetch()}
        action={
          <Segmented
            size="sm"
            value={range}
            onChange={setRange}
            options={[
              { value: "6m", label: "6M" },
              { value: "1y", label: "1A" },
              { value: "2y", label: "2A" },
              { value: "5y", label: "5A" },
              { value: "max", label: "Máx" },
            ]}
          />
        }
        table={
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-surface text-left text-xs uppercase tracking-wide text-ink-muted">
              <tr>
                <th className="px-2 py-2">Data</th>
                <th className="px-2 py-2 text-right">Patrimônio</th>
                <th className="px-2 py-2 text-right">Investido</th>
                <th className="px-2 py-2 text-right">Resultado</th>
              </tr>
            </thead>
            <tbody>
              {(history.data ?? []).slice(-60).reverse().map((point) => (
                <tr key={point.date} className="table-row">
                  <td className="px-2 py-1.5">{shortDate(point.date)}</td>
                  <td className="tnum px-2 py-1.5 text-right">{money(point.market_value)}</td>
                  <td className="tnum px-2 py-1.5 text-right">{money(point.cost_basis)}</td>
                  <td className="tnum px-2 py-1.5 text-right">{money(point.profit)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        }
      >
        <PortfolioHistoryChart data={history.data ?? []} height={320} />
      </ChartFrame>

      {/* Allocation + income */}
      <div className="grid gap-4 xl:grid-cols-2">
        <ChartFrame
          title="Alocação da carteira"
          subtitle={
            grouping === "kind"
              ? "Participação por classe de ativo"
              : grouping === "broker"
                ? "Participação por corretora"
                : "Participação por posição"
          }
          height={300}
          loading={allocation.isLoading}
          error={allocation.isError ? allocation.error : undefined}
          retry={() => allocation.refetch()}
          action={
            <div className="flex flex-wrap items-center gap-2">
              <Segmented
                size="sm"
                value={allocationMode}
                onChange={setAllocationMode}
                options={[
                  { value: "percent", label: "%" },
                  { value: "value", label: "R$" },
                ]}
              />
              <Segmented
                size="sm"
                value={grouping}
                onChange={setGrouping}
                options={[
                  { value: "kind", label: "Classe" },
                  { value: "asset", label: "Ativo" },
                  { value: "broker", label: "Corretora" },
                ]}
              />
            </div>
          }
          table={
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-surface text-left text-xs uppercase tracking-wide text-ink-muted">
                <tr>
                  <th className="px-2 py-2">Item</th>
                  <th className="px-2 py-2 text-right">Valor</th>
                  <th className="px-2 py-2 text-right">%</th>
                </tr>
              </thead>
              <tbody>
                {allocationData.map((slice) => (
                  <tr key={slice.key} className="table-row">
                    <td className="px-2 py-1.5">{slice.label}</td>
                    <td className="tnum px-2 py-1.5 text-right">{money(slice.value)}</td>
                    <td className="tnum px-2 py-1.5 text-right">{percent(slice.percent, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          }
        >
          <AllocationDonut
            data={allocationData}
            height={300}
            centerLabel="Total"
            centerValue={money(data?.market_value ?? 0, { compact: true })}
            valueMode={allocationMode}
          />
        </ChartFrame>

        <ChartFrame
          title="Proventos por mês"
          subtitle="Dividendos, JCP, rendimentos e juros"
          height={300}
          loading={income.isLoading}
          error={income.isError ? income.error : undefined}
          retry={() => income.refetch()}
          action={
            <Link to="/proventos" className="btn-ghost px-2 py-1.5 text-xs">
              Ver proventos <ArrowRight size={14} />
            </Link>
          }
          table={
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-surface text-left text-xs uppercase tracking-wide text-ink-muted">
                <tr>
                  <th className="px-2 py-2">Período</th>
                  <th className="px-2 py-2 text-right">Total</th>
                  <th className="px-2 py-2 text-right">Acumulado</th>
                </tr>
              </thead>
              <tbody>
                {[...(income.data ?? [])].reverse().map((point) => (
                  <tr key={point.period} className="table-row">
                    <td className="px-2 py-1.5">{periodLabel(point.period)}</td>
                    <td className="tnum px-2 py-1.5 text-right">{money(point.total)}</td>
                    <td className="tnum px-2 py-1.5 text-right">{money(point.cumulative)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          }
        >
          <IncomeBars data={incomeLast12} height={300} />
        </ChartFrame>
      </div>

      {/* Contributions + returns */}
      <div className="grid gap-4 xl:grid-cols-2">
        <ChartFrame
          title="Aportes mensais"
          subtitle="Compras e vendas liquidadas no mês"
          height={280}
          loading={contributions.isLoading}
          error={contributions.isError ? contributions.error : undefined}
          retry={() => contributions.refetch()}
        >
          <ContributionBars data={(contributions.data ?? []).slice(-24)} height={280} />
        </ChartFrame>
        <ChartFrame
          title="Retorno mensal"
          subtitle="Variação ajustada pelos aportes do período"
          height={280}
          loading={returns.isLoading}
          error={returns.isError ? returns.error : undefined}
          retry={() => returns.refetch()}
        >
          <ReturnsBars data={(returns.data ?? []).slice(-24)} height={280} />
        </ChartFrame>
      </div>

      {/* Ranking */}
      <div className="grid gap-4 xl:grid-cols-2">
        <ChartFrame
          title="Resultado por ativo"
          subtitle="Lucro/prejuízo não realizado das posições abertas"
          height={340}
          loading={positions.isLoading}
          error={positions.isError ? positions.error : undefined}
          retry={() => positions.refetch()}
        >
          <ProfitByAssetChart data={profitRanking} height={340} />
        </ChartFrame>

        <Card className="p-5">
          <SectionTitle
            title="Maiores posições"
            subtitle="Ordenadas por valor de mercado"
            action={
              <Link to="/ativos" className="text-sm text-accent hover:underline">
                Ver todos
              </Link>
            }
          />
          {positions.isLoading ? (
            <div className="space-y-2">
              {Array.from({ length: 8 }).map((_, index) => (
                <Skeleton key={index} className="h-9 w-full" />
              ))}
            </div>
          ) : (
          // Fixed layout: the ticker column absorbs the leftover width and
          // truncates, so a long fixed-income ticker can never push the
          // numeric columns off a phone screen.
          <div className="-mx-2">
            <table className="w-full table-fixed text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-ink-muted">
                <tr>
                  <th className="px-2 pb-2">Ativo</th>
                  <th className="w-[88px] px-2 pb-2 text-right sm:w-24">Valor</th>
                  <th className="w-[88px] px-2 pb-2 text-right sm:w-24">Result.</th>
                  <th className="w-12 px-2 pb-2 text-right sm:w-14">%</th>
                </tr>
              </thead>
              <tbody>
                {(positions.data ?? []).slice(0, 8).map((position) => (
                  <tr key={position.ticker} className="table-row">
                    <td className="px-2 py-2">
                      <Link
                        to={`/ativos/${position.ticker}`}
                        className="block truncate font-medium text-ink hover:text-accent"
                        title={position.ticker}
                      >
                        {position.ticker}
                      </Link>
                      <p className="truncate text-xs text-ink-muted">{kindLabel(position.kind)}</p>
                    </td>
                    <td className="tnum px-2 py-2 text-right">{money(position.market_value_base, { compact: true })}</td>
                    <td
                      className={`tnum px-2 py-2 text-right ${
                        position.unrealized_pnl_base >= 0 ? "text-positive" : "text-negative"
                      }`}
                    >
                      {money(position.unrealized_pnl_base, { compact: true })}
                    </td>
                    <td className="tnum px-2 py-2 text-right text-ink-secondary">
                      {percent(position.allocation_pct, 1)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          )}
        </Card>
      </div>

      {/* Day movers + latest income — both were already served by the API and
          only consumed deep inside Relatórios/Proventos. */}
      <div className="grid gap-4 xl:grid-cols-2">
        <Card className="p-5">
          <SectionTitle title="Movimentação do dia" subtitle="Maiores altas e quedas de hoje" />
          {movers.isLoading ? (
            <div className="space-y-2">
              {Array.from({ length: 4 }).map((_, index) => (
                <Skeleton key={index} className="h-7 w-full" />
              ))}
            </div>
          ) : movers.data && [...movers.data.best, ...movers.data.worst].length === 0 ? (
            <p className="py-6 text-center text-sm text-ink-muted">Sem variação registrada hoje.</p>
          ) : (
            <div className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
              {[movers.data?.best ?? [], movers.data?.worst ?? []].map((rows, column) => (
                // min-w-0: a grid item defaults to min-width auto, so one long
                // fixed-income ticker would widen the card past the viewport
                // instead of truncating.
                <div key={column === 0 ? "best" : "worst"} className="min-w-0">
                  {rows.map((row) => (
                    <p key={row.ticker} className="flex items-baseline justify-between gap-3 py-1.5">
                      <Link to={`/ativos/${row.ticker}`} className="min-w-0 truncate font-medium text-ink hover:text-accent">
                        {row.ticker}
                      </Link>
                      <span className="flex shrink-0 items-baseline gap-2">
                        <span className={`tnum text-xs ${row.window_change >= 0 ? "text-positive" : "text-negative"}`}>
                          {money(row.window_change, { compact: true })}
                        </span>
                        <span className={`tnum ${row.window_change >= 0 ? "text-positive" : "text-negative"}`}>
                          {row.window_pct === null ? "—" : percent(row.window_pct, 2, true)}
                        </span>
                      </span>
                    </p>
                  ))}
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card className="p-5">
          <SectionTitle
            title="Últimos proventos"
            subtitle="Pagamentos mais recentes creditados"
            action={
              <Link to="/proventos" className="text-sm text-accent hover:underline">
                Ver proventos
              </Link>
            }
          />
          {recentIncome.isLoading ? (
            <div className="space-y-2">
              {Array.from({ length: 6 }).map((_, index) => (
                <Skeleton key={index} className="h-7 w-full" />
              ))}
            </div>
          ) : recentIncome.data && recentIncome.data.length === 0 ? (
            <p className="py-6 text-center text-sm text-ink-muted">Nenhum provento registrado.</p>
          ) : (
            <div>
              {(recentIncome.data ?? []).map((payment) => (
                <p
                  key={`${payment.ticker}-${payment.date}-${payment.op_type}-${payment.amount}`}
                  className="flex items-baseline justify-between gap-3 py-1.5"
                >
                  <span className="min-w-0 truncate">
                    <Link to={`/ativos/${payment.ticker}`} className="font-medium text-ink hover:text-accent">
                      {payment.ticker}
                    </Link>
                    <span className="ml-2 text-xs text-ink-muted">
                      {opLabel(payment.op_type)} · {shortDate(payment.date)}
                    </span>
                  </span>
                  <span className="tnum shrink-0 text-positive">{money(payment.net)}</span>
                </p>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
