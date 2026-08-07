/** Everything the portfolio has paid: per period, per class, per asset. */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarDays, Coins, PiggyBank, Trophy } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import {
  AllocationDonut,
  ChartFrame,
  kindSlices,
  IncomeBreakdownBars,
  IncomeMatrix,
  TopPayersBars,
} from "@/components/charts";
import DividendCalendar from "@/components/DividendCalendar";
import { useToast } from "@/components/Toast";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  KindTag,
  Modal,
  Pager,
  SectionTitle,
  Segmented,
  Skeleton,
  StatTile,
  Tabs,
} from "@/components/ui";
import { api, type DividendReport, type IncomePayment } from "@/lib/api";
import { SERIES, kindColor, kindRank } from "@/lib/colors";
import { kindLabel, money, opLabel, percent, periodLabel, quantity, shortDate } from "@/lib/format";

type Granularity = "month" | "quarter" | "year";

const GRANULARITY_LABELS: Record<Granularity, string> = {
  month: "Mês",
  quarter: "Trimestre",
  year: "Ano",
};

/**
 * How the period bars are broken up. The two axes are independent questions —
 * "what kind of payment was it" and "what paid it" — so they are offered as
 * alternatives rather than merged into one breakdown.
 */
type Grouping = "none" | "type" | "kind";

const GROUPING_LABELS: Record<Grouping, string> = {
  none: "Total",
  type: "Tipo",
  kind: "Classe",
};

const GROUPING_HINTS: Record<Grouping, string> = {
  none: "Total recebido em cada período",
  type: "Empilhado por tipo de pagamento",
  kind: "Empilhado por classe de ativo",
};

type Range = "6m" | "1y" | "2y" | "all";

const RANGE_LABELS: Record<Range, string> = { "6m": "6M", "1y": "1A", "2y": "2A", all: "Tudo" };

/** Months each range covers, counting the current one. `null` = everything. */
const RANGE_MONTHS: Record<Range, number | null> = { "6m": 6, "1y": 12, "2y": 24, all: null };

/**
 * Last day a period covers — "2026-07", "2026-Q3" and "2026" all resolve.
 *
 * The comparison is against the period's *end* so a bucket that merely overlaps
 * the window is kept: a 6-month range grouped by year must still show the
 * current year, which started long before the window did.
 */
function periodEnd(period: string): Date {
  const [year, rest] = period.split("-");
  const y = Number(year);
  if (!rest) return new Date(y, 11, 31);
  if (rest.startsWith("Q")) return new Date(y, Number(rest.slice(1)) * 3, 0);
  return new Date(y, Number(rest), 0);
}

/** What a single period's bar is made of, grouped by class then by payer. */
function PeriodBreakdown({
  period,
  granularity,
  onClose,
}: {
  period: string | null;
  granularity: Granularity;
  onClose: () => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["dividend-breakdown", period, granularity],
    queryFn: () => api.dividendBreakdown(period as string, granularity),
    enabled: Boolean(period),
  });

  return (
    <Modal
      open={Boolean(period)}
      title={period ? periodLabel(period) : ""}
      subtitle={data ? `${money(data.total)} recebidos` : undefined}
      onClose={onClose}
    >
      {isLoading || !data ? (
        <Skeleton className="h-40 w-full" />
      ) : !data.groups.length ? (
        <p className="text-sm text-ink-muted">Nenhum provento neste período.</p>
      ) : (
        <div className="space-y-5">
          {data.groups.map((group) => (
            <div key={group.kind}>
              <div className="mb-2 flex items-baseline justify-between gap-3">
                <KindTag kind={group.kind} />
                <span className="tnum text-sm font-medium">{money(group.total)}</span>
              </div>
              <ul className="space-y-1">
                {group.assets.map((asset) => (
                  <li key={asset.ticker} className="flex items-baseline justify-between gap-3 text-sm">
                    <Link to={`/ativos/${asset.ticker}`} className="font-medium hover:text-accent">
                      {asset.ticker}
                    </Link>
                    <span className="min-w-0 flex-1 truncate text-xs text-ink-muted" title={asset.name}>
                      {asset.name}
                    </span>
                    <span className="tnum shrink-0 text-ink-secondary">{money(asset.total)}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

const PAGE_SIZES = [15, 25, 0];

type AssetIncome = DividendReport["by_asset"][number];

/**
 * Classes that pay interest, amortisation or nothing at all. They belong to the
 * income totals, but not to a table read as "what my dividend payers pay" —
 * a CDB's coupon has no yield to compare against a REIT's.
 */
const NON_DIVIDEND_KINDS = new Set(["FIXED_INCOME", "TREASURY", "SUBSCRIPTION", "OTHER", "OPTION", "FUTURE"]);

/** Per-asset income: one class at a time, paged within that class. */
function AssetIncomeTable({ rows }: { rows: AssetIncome[] }) {
  const [showAll, setShowAll] = useState(false);
  const [kind, setKind] = useState<string | null>(null);
  const [pageSize, setPageSize] = useState(15);
  const [page, setPage] = useState(1);

  const excluded = useMemo(() => rows.filter((row) => NON_DIVIDEND_KINDS.has(row.kind)), [rows]);
  const eligible = useMemo(
    () => (showAll ? rows : rows.filter((row) => !NON_DIVIDEND_KINDS.has(row.kind))),
    [rows, showAll],
  );

  // Tabs follow the palette order the charts use, so this list and the donut
  // name the classes in the same sequence.
  const tabs = useMemo(() => {
    const totals = new Map<string, { count: number; net: number }>();
    for (const row of eligible) {
      const entry = totals.get(row.kind) ?? { count: 0, net: 0 };
      totals.set(row.kind, { count: entry.count + 1, net: entry.net + Number(row.net) });
    }
    return [...totals.entries()]
      .sort(([a], [b]) => kindRank(a) - kindRank(b))
      .map(([value, entry]) => ({
        value,
        label: kindLabel(value),
        count: entry.count,
        color: kindColor(value),
        net: entry.net,
      }));
  }, [eligible]);

  // A class can vanish when the toggle changes, so the selection is validated
  // against the tabs that exist rather than trusted.
  const active = tabs.some((tab) => tab.value === kind) ? (kind as string) : (tabs[0]?.value ?? "");
  const activeTab = tabs.find((tab) => tab.value === active);

  const classRows = useMemo(
    () => eligible.filter((row) => row.kind === active).sort((a, b) => Number(b.net) - Number(a.net)),
    [eligible, active],
  );

  useEffect(() => setPage(1), [active, showAll, pageSize]);

  const size = pageSize > 0 ? pageSize : Math.max(classRows.length, 1);
  const pages = Math.max(1, Math.ceil(classRows.length / size));
  const current = Math.min(page, pages);
  const pageRows = classRows.slice((current - 1) * size, current * size);
  const excludedNet = excluded.reduce((sum, row) => sum + Number(row.net), 0);

  return (
    <Card className="p-5">
      <SectionTitle
        title="Renda por ativo"
        subtitle={
          activeTab
            ? `${kindLabel(active)}: ${money(activeTab.net)} líquidos de ${activeTab.count} ativo(s)` +
              (showAll || !excluded.length ? "" : ` · ${money(excludedNet)} de renda fixa e outras classes fora da lista`)
            : "Yield on cost compara o que o ativo paga com o capital que ainda está nele"
        }
        action={
          excluded.length ? (
            <Segmented
              size="sm"
              value={showAll ? "all" : "dividends"}
              onChange={(value) => setShowAll(value === "all")}
              options={[
                { value: "dividends", label: "Dividendos" },
                { value: "all", label: "Todas as classes" },
              ]}
            />
          ) : undefined
        }
      />

      <Tabs value={active} onChange={setKind} options={tabs} className="mb-4" />
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-sm">
          <thead className="border-b border-line text-left text-xs uppercase tracking-wide text-ink-muted">
            <tr>
              <th className="px-3 pb-2">Ativo</th>
              <th className="px-3 pb-2 text-right">Bruto</th>
              <th className="px-3 pb-2 text-right">Imposto</th>
              <th className="px-3 pb-2 text-right">Líquido</th>
              <th className="px-3 pb-2 text-right">% do total</th>
              <th className="px-3 pb-2 text-right">Pagamentos</th>
              <th className="px-3 pb-2 text-right">Custo atual</th>
              <th className="px-3 pb-2 text-right">Yield on cost</th>
              <th className="px-3 pb-2 text-right">Último</th>
            </tr>
          </thead>
          <tbody>
            {pageRows.map((row) => (
              <tr key={row.ticker} className="table-row">
                <td className="px-3 py-2 font-medium">
                  <Link to={`/ativos/${row.ticker}`} className="hover:text-accent">
                    {row.ticker}
                  </Link>
                </td>
                <td className="tnum px-3 py-2 text-right text-ink-secondary">{money(row.total)}</td>
                <td className="tnum px-3 py-2 text-right text-ink-secondary">
                  {row.tax ? `− ${money(row.tax)}` : "-"}
                </td>
                <td className="tnum px-3 py-2 text-right font-medium text-positive">{money(row.net)}</td>
                <td className="tnum px-3 py-2 text-right text-ink-secondary">{percent(row.share, 1)}</td>
                <td className="tnum px-3 py-2 text-right text-ink-secondary">{row.payments}</td>
                <td className="tnum px-3 py-2 text-right text-ink-secondary">
                  {row.cost_basis ? money(row.cost_basis) : "-"}
                </td>
                <td className="tnum px-3 py-2 text-right">
                  {row.yield_on_cost_net === null ? (
                    <span className="text-ink-muted">encerrado</span>
                  ) : (
                    percent(row.yield_on_cost_net, 1)
                  )}
                </td>
              <td className="px-3 py-2 text-right text-ink-muted">{shortDate(row.last)}</td>
            </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-3">
        <Pager
          page={current}
          pages={pages}
          total={classRows.length}
          pageSize={pageSize}
          onChange={setPage}
          noun="ativos"
          pageSizeOptions={PAGE_SIZES}
          onPageSizeChange={setPageSize}
        />
      </div>
    </Card>
  );
}

/** The most recent payments, newest first, a page at a time. */
function RecentPayments({ payments }: { payments: IncomePayment[] }) {
  const [pageSize, setPageSize] = useState(15);
  const [page, setPage] = useState(1);

  useEffect(() => setPage(1), [pageSize]);

  const size = pageSize > 0 ? pageSize : Math.max(payments.length, 1);
  const pages = Math.max(1, Math.ceil(payments.length / size));
  const current = Math.min(page, pages);
  const visible = payments.slice((current - 1) * size, current * size);

  return (
    <Card className="flex flex-col p-5">
      <SectionTitle
        title="Últimos pagamentos"
        subtitle="O que caiu na conta mais recentemente"
        action={<CalendarDays size={16} className="text-ink-muted" aria-hidden />}
      />
      {!payments.length ? (
        <p className="text-sm text-ink-muted">Nenhum pagamento registrado.</p>
      ) : (
        <>
          <ul className="flex-1 space-y-1.5">
            {visible.map((payment, index) => (
              <li
                key={`${payment.date}-${payment.ticker}-${index}`}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg px-2 py-1.5 text-sm hover:bg-surface-hover"
              >
                <span className="tnum w-[74px] shrink-0 text-xs text-ink-muted">{shortDate(payment.date)}</span>
                <Link to={`/ativos/${payment.ticker}`} className="font-medium hover:text-accent">
                  {payment.ticker}
                </Link>
                <Badge>{opLabel(payment.op_type)}</Badge>
                {payment.quantity ? (
                  <span className="text-xs text-ink-muted">{quantity(payment.quantity)} un.</span>
                ) : null}
                <span className="tnum ml-auto font-medium text-positive">{money(payment.net)}</span>
              </li>
            ))}
          </ul>
          <div className="mt-3">
            <Pager
              page={current}
              pages={pages}
              total={payments.length}
              pageSize={pageSize}
              onChange={setPage}
              noun="pagamentos"
              pageSizeOptions={PAGE_SIZES}
              onPageSizeChange={setPageSize}
            />
          </div>
        </>
      )}
    </Card>
  );
}

export default function Dividends() {
  const toast = useToast();
  const [granularity, setGranularity] = useState<Granularity>("month");
  const [range, setRange] = useState<Range>("1y");
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [grouping, setGrouping] = useState<Grouping>("none");
  // Only the period series depends on the grouping; totals, classes and payers
  // are identical across it. Keeping the previous data on screen while the new
  // grouping loads stops the whole page collapsing into a skeleton.
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["dividends", granularity],
    queryFn: () => api.dividends(granularity),
    placeholderData: keepPreviousData,
  });
  const monthly = useQuery({
    queryKey: ["dividends", "month"],
    queryFn: () => api.dividends("month"),
    placeholderData: keepPreviousData,
  });
  // Paged rather than scrolled now, so the window is worth widening — 60 was
  // sized for a list you could only scroll a little way down.
  const calendar = useQuery({ queryKey: ["dividend-calendar"], queryFn: () => api.dividendCalendar(300) });
  const queryClient = useQueryClient();
  // The scheduled side of the calendar. Cached server-side; the button asks
  // the backend to re-query B3 for stale assets and hands back the new list.
  const upcoming = useQuery({
    queryKey: ["dividends-upcoming"],
    queryFn: () => api.upcomingDividends(),
    staleTime: 30 * 60_000,
  });
  const refreshUpcoming = useMutation({
    mutationFn: () => api.upcomingDividends(true),
    onSuccess: (fresh) => {
      queryClient.setQueryData(["dividends-upcoming"], fresh);
      const count = fresh.items.length;
      toast.success(
        count
          ? `${count} provento${count === 1 ? "" : "s"} anunciado${count === 1 ? "" : "s"}.`
          : "Nenhum provento anunciado no momento.",
      );
    },
    onError: (error) => toast.error("Não foi possível consultar os proventos anunciados.", error),
  });

  // Stack keys for the selected axis, largest first (the API orders both
  // breakdowns by total). Empty when nothing is being split.
  const keys = useMemo(() => {
    if (grouping === "type") return (data?.by_type ?? []).map((row) => row.op_type);
    // Classes stack in palette order: the segments touch, and only adjacent
    // palette slots are guaranteed to be separable.
    if (grouping === "kind")
      return (data?.by_kind ?? []).map((row) => row.kind).sort((a, b) => kindRank(a) - kindRank(b));
    return [];
  }, [grouping, data?.by_type, data?.by_kind]);

  const labelFor = grouping === "kind" ? kindLabel : opLabel;

  const toggleKey = (key: string) =>
    setHidden((current) => {
      const next = new Set(current);
      if (!next.delete(key)) next.add(key);
      return next;
    });

  // Switching the axis rebuilds the keys, so old hidden keys mean nothing.
  useEffect(() => setHidden(new Set()), [grouping]);

  const series = useMemo(() => {
    const all = data?.series ?? [];
    const months = RANGE_MONTHS[range];
    if (months === null) return all;
    const now = new Date();
    const cutoff = new Date(now.getFullYear(), now.getMonth() - (months - 1), 1);
    return all.filter((point) => periodEnd(point.period) >= cutoff);
  }, [data?.series, range]);

  // Recharts stacks sibling keys on the row itself, so the selected axis is
  // flattened onto each point. The other axis simply is not read.
  const chartData = useMemo(
    () =>
      series.map((point) => ({
        period: point.period,
        // Net everywhere on this chart: the segments are net, so the single
        // bar has to be too, or collapsing the stack would change its height.
        // Gross and withholding stay in the table below.
        total: Number(point.net),
        ...(grouping === "kind" ? point.kinds_net : grouping === "type" ? point.types_net : {}),
      })),
    [series, grouping],
  );

  const byKind = useMemo(
    () => kindSlices((data?.by_kind ?? []).map((row) => ({ kind: row.kind, value: Number(row.total), percent: Number(row.share) }))),
    [data?.by_kind],
  );

  // Already sorted by total; Recharts puts the first category at the top, which
  // is where the biggest payer belongs.
  const topPayers = useMemo(() => (data?.by_asset ?? []).slice(0, 12), [data?.by_asset]);

  if (isError) return <ErrorState error={error} retry={() => refetch()} />;
  if (isLoading || !data) return <Skeleton className="h-96 w-full" />;
  if (!data.totals.payments) {
    return (
      <EmptyState
        icon={Coins}
        title="Nenhum provento recebido ainda"
        description="Dividendos, JCP, rendimentos e juros aparecem aqui assim que forem importados."
      />
    );
  }

  const totals = data.totals;

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">Renda</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Proventos</h1>
        <p className="mt-2 max-w-2xl text-sm text-ink-secondary">
          Dividendos, juros sobre capital próprio, rendimentos de fundos e juros de renda fixa:{" "}
          <span className="tnum text-ink">{totals.payments}</span> pagamentos de{" "}
          <span className="tnum text-ink">{totals.assets}</span> ativos.
        </p>
      </header>

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {/* Net leads, gross explains it. What reached the account is the
            number that matters day to day; the gross figure is what gets
            declared, so both are on screen rather than one or the other. */}
        <StatTile
          label="Recebido (líquido)"
          value={money(totals.net)}
          icon={Coins}
          tone="positive"
          hint={
            totals.tax
              ? `${money(totals.all_time)} bruto − ${money(totals.tax)} de imposto`
              : "sem imposto retido na fonte"
          }
        />
        <StatTile
          label="Últimos 12 meses"
          value={money(totals.net_last_12m)}
          hint={`${money(totals.net_monthly_average_12m)} por mês, líquido`}
        />
        <StatTile
          label="Yield on cost"
          value={percent(totals.yield_on_cost_net, 2)}
          icon={PiggyBank}
          hint={`líquido, 12m sobre ${money(totals.cost_basis, { compact: true })} investidos`}
        />
        <StatTile
          label="Melhor mês"
          value={money(totals.best_month_amount)}
          icon={Trophy}
          hint={totals.best_month ? periodLabel(totals.best_month) : undefined}
        />
      </section>

      <ChartFrame
        title={`Proventos por ${GRANULARITY_LABELS[granularity].toLowerCase()}`}
        subtitle={GROUPING_HINTS[grouping]}
        height={320}
        action={
          <>
            <Segmented
              size="sm"
              value={range}
              onChange={setRange}
              options={(Object.keys(RANGE_LABELS) as Range[]).map((value) => ({
                value,
                label: RANGE_LABELS[value],
              }))}
            />
            <Segmented
              size="sm"
              value={granularity}
              onChange={setGranularity}
              options={(Object.keys(GRANULARITY_LABELS) as Granularity[]).map((value) => ({
                value,
                label: GRANULARITY_LABELS[value],
              }))}
            />
            <Segmented
              size="sm"
              value={grouping}
              onChange={setGrouping}
              options={(Object.keys(GROUPING_LABELS) as Grouping[]).map((value) => ({
                value,
                label: GROUPING_LABELS[value],
              }))}
            />
          </>
        }
        table={
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-surface text-left text-xs uppercase tracking-wide text-ink-muted">
              <tr>
                <th className="px-2 py-2">Período</th>
                {keys.map((key) => (
                  <th key={key} className="px-2 py-2 text-right">
                    {labelFor(key)}
                  </th>
                ))}
                <th className="px-2 py-2 text-right">Imposto</th>
                <th className="px-2 py-2 text-right">Líquido</th>
                <th className="px-2 py-2 text-right">Acumulado</th>
              </tr>
            </thead>
            <tbody>
              {[...data.series].reverse().map((point) => (
                <tr key={point.period} className="table-row">
                  <td className="px-2 py-1.5">{periodLabel(point.period)}</td>
                  {keys.map((key) => {
                    const values = grouping === "kind" ? point.kinds_net : point.types_net;
                    return (
                      <td key={key} className="tnum px-2 py-1.5 text-right text-ink-secondary">
                        {values[key] ? money(values[key]) : "-"}
                      </td>
                    );
                  })}
                  <td className="tnum px-2 py-1.5 text-right text-ink-secondary">
                    {point.tax ? `− ${money(point.tax)}` : "-"}
                  </td>
                  <td className="tnum px-2 py-1.5 text-right font-medium">{money(point.net)}</td>
                  <td className="tnum px-2 py-1.5 text-right text-ink-muted">{money(point.cumulative)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        }
      >
        <IncomeBreakdownBars
          data={chartData}
          keys={keys}
          labelFor={labelFor}
          colorFor={grouping === "kind" ? kindColor : undefined}
          hidden={hidden}
          onToggle={toggleKey}
          onSelect={setSelected}
          height={320}
        />
      </ChartFrame>

      <DividendCalendar
        payments={calendar.data ?? []}
        upcoming={upcoming.data}
        onRefresh={() => refreshUpcoming.mutate()}
        refreshing={refreshUpcoming.isPending}
      />

      <div className="grid gap-4 xl:grid-cols-2">
        <ChartFrame
          title="Proventos por classe"
          subtitle="Quem paga a renda da carteira"
          height={300}
          table={
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-surface text-left text-xs uppercase tracking-wide text-ink-muted">
                <tr>
                  <th className="px-2 py-2">Classe</th>
                  <th className="px-2 py-2 text-right">Total</th>
                  <th className="px-2 py-2 text-right">%</th>
                </tr>
              </thead>
              <tbody>
                {data.by_kind.map((row) => (
                  <tr key={row.kind} className="table-row">
                    <td className="px-2 py-1.5">{kindLabel(row.kind)}</td>
                    <td className="tnum px-2 py-1.5 text-right">{money(row.net)}</td>
                    <td className="tnum px-2 py-1.5 text-right">{percent(row.share, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          }
        >
          <AllocationDonut
            data={byKind}
            height={300}
            centerLabel="Total"
            centerValue={money(totals.net, { compact: true })}
          />
        </ChartFrame>

        <Card className="p-5">
          <SectionTitle title="Por tipo de provento" subtitle="Dividendo, JCP, rendimento, juros" />
          <ul className="space-y-3">
            {data.by_type.map((row, index) => (
              <li key={row.op_type}>
                <div className="flex items-baseline justify-between gap-3 text-sm">
                  <span className="flex items-center gap-2">
                    <span
                      className="h-2.5 w-2.5 rounded-sm"
                      style={{ background: SERIES[index % SERIES.length] }}
                      aria-hidden
                    />
                    {opLabel(row.op_type)}
                  </span>
                  <span className="tnum font-medium">{money(row.net)}</span>
                </div>
                {/* The bar repeats the share the number already states — it is
                    there to make the ranking scannable, not to be read off. */}
                <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-surface-raised">
                  <div
                    className="h-full rounded-full"
                    style={{
                      width: `${Math.max(Number(row.share), 1)}%`,
                      background: SERIES[index % SERIES.length],
                    }}
                  />
                </div>
                <p className="mt-1 text-xs text-ink-muted">{percent(row.share, 1)} do total</p>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <Card className="p-5">
        <SectionTitle
          title="Mapa de proventos"
          subtitle="Cada mês desde o início: a intensidade acompanha o valor recebido"
        />
        {/* On error the skeleton used to shimmer forever — say what happened. */}
        {monthly.isError ? (
          <ErrorState error={monthly.error} retry={() => monthly.refetch()} />
        ) : monthly.data ? (
          <IncomeMatrix data={monthly.data.series.map((p) => ({ period: p.period, total: p.net }))} />
        ) : (
          <Skeleton className="h-40 w-full" />
        )}
      </Card>

      <div className="grid gap-4 xl:grid-cols-2">
        <ChartFrame
          title="Maiores pagadores"
          subtitle="Proventos acumulados por ativo"
          height={360}
          table={
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-surface text-left text-xs uppercase tracking-wide text-ink-muted">
                <tr>
                  <th className="px-2 py-2">Ativo</th>
                  <th className="px-2 py-2 text-right">Bruto</th>
                  <th className="px-2 py-2 text-right">Líquido</th>
                  <th className="px-2 py-2 text-right">Yield on cost</th>
                </tr>
              </thead>
              <tbody>
                {data.by_asset.map((row) => (
                  <tr key={row.ticker} className="table-row">
                    <td className="px-2 py-1.5">{row.ticker}</td>
                    <td className="tnum px-2 py-1.5 text-right text-ink-secondary">{money(row.total)}</td>
                    <td className="tnum px-2 py-1.5 text-right">{money(row.net)}</td>
                    <td className="tnum px-2 py-1.5 text-right">
                      {row.yield_on_cost_net === null ? "-" : percent(row.yield_on_cost_net, 1)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          }
        >
          <TopPayersBars data={topPayers} height={360} />
        </ChartFrame>

        {calendar.isError ? (
          <Card className="p-5">
            <SectionTitle title="Últimos pagamentos" subtitle="O que caiu na conta mais recentemente" />
            <ErrorState error={calendar.error} retry={() => calendar.refetch()} />
          </Card>
        ) : !calendar.data ? (
          <Skeleton className="h-72 w-full" />
        ) : (
          <RecentPayments payments={calendar.data} />
        )}
      </div>

      <AssetIncomeTable rows={data.by_asset} />

      <PeriodBreakdown period={selected} granularity={granularity} onClose={() => setSelected(null)} />
    </div>
  );
}
