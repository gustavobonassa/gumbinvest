/** Sortable, filterable list of every asset ever held. */
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { ArrowDown, ArrowUp, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Badge, Card, EmptyState, ErrorState, Pager, Segmented, Skeleton, Tabs } from "@/components/ui";
import { api, type PositionRow } from "@/lib/api";
import { kindColor } from "@/lib/colors";
import { KIND_LABELS, kindLabel, money, percent, quantity } from "@/lib/format";

type SortKey =
  | "market_value"
  | "ticker"
  | "average_price"
  | "unrealized_pct"
  | "total_return"
  | "allocation_pct"
  | "income";

/** `hide` collapses secondary columns on narrow screens: a phone shows the
    asset, what it is worth and how it is doing — the rest returns with width
    instead of forcing a 960px sideways scroll. */
const COLUMNS: { key: SortKey; label: string; align?: "right"; hide?: string }[] = [
  { key: "ticker", label: "Ativo" },
  { key: "average_price", label: "Preço médio", align: "right", hide: "hidden xl:table-cell" },
  { key: "market_value", label: "Valor atual", align: "right" },
  { key: "unrealized_pct", label: "Não realizado", align: "right" },
  { key: "income", label: "Proventos", align: "right", hide: "hidden xl:table-cell" },
  { key: "total_return", label: "Resultado total", align: "right", hide: "hidden md:table-cell" },
  { key: "allocation_pct", label: "Alocação", align: "right", hide: "hidden md:table-cell" },
];

/** Class order follows the labels table, so the tabs read the same everywhere. */
const KIND_ORDER = Object.keys(KIND_LABELS);
const PAGE_SIZES = [15, 25, 0];

/**
 * Classes with no tab. Subscription rights and receipts are plumbing — they
 * exist for a few days between a right being credited and the shares arriving,
 * and carry no value of their own; options are not held as positions here.
 */
const HIDDEN_KINDS = new Set(["SUBSCRIPTION", "OPTION"]);

/**
 * What the list below adds up to.
 *
 * The same five figures the table's columns carry, totalled — so the reader can
 * go from "how is this class doing" to "which asset is doing it" without
 * changing screens or doing the sum by eye. It follows the *filtered* list, not
 * the class in the abstract: the numbers on screen always describe the rows on
 * screen, which is why it says how many assets it covers.
 */
function CategoryTotals({ rows, kind }: { rows: PositionRow[]; kind: string }) {
  const totals = useMemo(() => {
    const sum = (pick: (row: PositionRow) => number) =>
      rows.reduce((total, row) => total + Number(pick(row) ?? 0), 0);
    const invested = sum((row) => row.cost_basis_base);
    const unrealized = sum((row) => row.unrealized_pnl_base);
    const result = sum((row) => row.total_return_base);
    // A closed position keeps its result but no longer has cost to divide by,
    // so a history-scoped class can total a real gain over almost no basis.
    // Below a cent of capital there is no percentage to state.
    const share = (value: number) => (invested > 0.01 ? (value / invested) * 100 : null);
    return {
      invested,
      value: sum((row) => row.market_value_base),
      unrealized,
      unrealizedPct: share(unrealized),
      income: sum((row) => row.income_base),
      result,
      resultPct: share(result),
    };
  }, [rows]);

  const figures: { label: string; value: string; hint?: string; tone?: number }[] = [
    { label: "Investido", value: money(totals.invested), hint: `${rows.length} ativo${rows.length === 1 ? "" : "s"}` },
    { label: "Valor atual", value: money(totals.value) },
    {
      label: "Não realizado",
      value: money(totals.unrealized),
      hint: totals.unrealizedPct === null ? undefined : percent(totals.unrealizedPct, 2, true),
      tone: totals.unrealized,
    },
    { label: "Proventos", value: money(totals.income) },
    {
      label: "Resultado total",
      value: money(totals.result),
      hint: totals.resultPct === null ? undefined : percent(totals.resultPct, 2, true),
      tone: totals.result,
    },
  ];

  const color = kindColor(kind);

  return (
    <Card className="relative overflow-hidden p-4" hover={false}>
      {/* The class colour bled into the corner the eye lands on first, so the
          panel is recognisable as "this class" before a word is read. Alpha is
          kept in the low teens: it has to tint the surface, not compete with
          the figures sitting on it. */}
      <span
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background: `radial-gradient(120% 120% at 0% 0%, ${color}24 0%, ${color}0d 30%, transparent 62%)`,
        }}
      />
      {/* Positioned, so it paints above the absolutely positioned wash. */}
      <div className="relative">
        <div className="flex items-center gap-2">
          <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: color }} aria-hidden />
          <span className="text-sm font-medium text-ink">{kindLabel(kind)}</span>
        </div>
        <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-5">
          {figures.map((figure) => (
            <div key={figure.label} className="min-w-0">
              <dt className="text-xs text-ink-muted">{figure.label}</dt>
              <dd
                className={clsx(
                  "tnum mt-0.5 truncate text-lg font-semibold leading-tight",
                  figure.tone === undefined && "text-ink",
                  figure.tone !== undefined && (figure.tone >= 0 ? "text-positive" : "text-negative"),
                )}
                title={figure.value}
              >
                {figure.value}
              </dd>
              {figure.hint ? <dd className="tnum text-xs text-ink-muted">{figure.hint}</dd> : null}
            </div>
          ))}
        </dl>
      </div>
    </Card>
  );
}

export default function Assets() {
  const [params, setParams] = useSearchParams();
  const [filter, setFilter] = useState("");
  const [scope, setScope] = useState<"open" | "all">("open");
  const [sort, setSort] = useState<SortKey>("market_value");
  const [direction, setDirection] = useState<"asc" | "desc">("desc");
  const [pageSize, setPageSize] = useState(15);
  const [page, setPage] = useState(1);

  const setKind = (next: string) => setParams({ classe: next }, { replace: true });

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["assets"],
    queryFn: () => api.assets(true),
  });

  // Everything the class tabs do not decide: scope and the search box. Tab
  // counts come from here, so they say how many rows each tab would actually
  // show rather than how many exist in the abstract.
  const scoped = useMemo(() => {
    let list = data ?? [];
    if (scope === "open") list = list.filter((asset) => asset.is_open);
    if (filter.trim()) {
      const needle = filter.trim().toLowerCase();
      list = list.filter(
        (asset) => asset.ticker.toLowerCase().includes(needle) || asset.name.toLowerCase().includes(needle),
      );
    }
    return list;
  }, [data, scope, filter]);

  const tabs = useMemo(() => {
    const counts = new Map<string, number>();
    for (const asset of scoped) {
      if (HIDDEN_KINDS.has(asset.kind)) continue;
      counts.set(asset.kind, (counts.get(asset.kind) ?? 0) + 1);
    }
    return [...counts.keys()]
      .sort((a, b) => (KIND_ORDER.indexOf(a) + 1 || 99) - (KIND_ORDER.indexOf(b) + 1 || 99))
      .map((item) => ({
        value: item,
        label: kindLabel(item),
        count: counts.get(item) ?? 0,
        color: kindColor(item),
      }));
  }, [scoped]);

  // A class the current scope or search leaves empty has no tab to sit on, so
  // the selection falls back to the first class that does.
  const requested = params.get("classe");
  const kind = tabs.some((tab) => tab.value === requested) ? String(requested) : (tabs[0]?.value ?? "");

  const rows = useMemo(() => {
    const list = scoped.filter((asset) => asset.kind === kind);
    const factor = direction === "asc" ? 1 : -1;
    return [...list].sort((a, b) => {
      const left = a[sort as keyof PositionRow];
      const right = b[sort as keyof PositionRow];
      if (typeof left === "string" && typeof right === "string") return left.localeCompare(right) * factor;
      return (Number(left) - Number(right)) * factor;
    });
  }, [scoped, kind, sort, direction]);

  // Any change to what is being listed puts the reader back on page 1 — page 4
  // of a different list is a page they never asked for.
  useEffect(() => setPage(1), [kind, filter, scope, pageSize]);

  const size = pageSize > 0 ? pageSize : Math.max(rows.length, 1);
  const pages = Math.max(1, Math.ceil(rows.length / size));
  const current = Math.min(page, pages);
  const visible = rows.slice((current - 1) * size, current * size);

  const toggleSort = (key: SortKey) => {
    if (key === sort) setDirection((current) => (current === "asc" ? "desc" : "asc"));
    else {
      setSort(key);
      setDirection(key === "ticker" ? "asc" : "desc");
    }
  };

  if (isError) return <ErrorState error={error} retry={() => refetch()} />;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3 animate-fade-up">
        <div>
          <p className="text-sm text-ink-muted">Carteira</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">Ativos</h1>
        </div>
        <Segmented
          value={scope}
          onChange={setScope}
          options={[
            { value: "open", label: "Em carteira" },
            { value: "all", label: "Histórico" },
          ]}
        />
      </header>

      {/* One class at a time, so the table is a list of comparable things. */}
      <Tabs value={kind} onChange={setKind} options={tabs} />

      {!isLoading && rows.length ? <CategoryTotals rows={rows} kind={kind} /> : null}

      <Card className="flex flex-wrap items-center gap-3 p-3" hover={false}>
        <div className="relative min-w-[220px] flex-1">
          <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted" />
          <input
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="Filtrar por ticker ou nome"
            className="input pl-9"
          />
        </div>
      </Card>

      <Card className="overflow-hidden p-0">
        {isLoading ? (
          <div className="space-y-2 p-5">
            {Array.from({ length: 8 }).map((_, index) => (
              <Skeleton key={index} className="h-11 w-full" />
            ))}
          </div>
        ) : !rows.length ? (
          <EmptyState title="Nenhum ativo encontrado" description="Ajuste os filtros ou importe novas transações." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b border-line bg-surface-raised/60 text-xs uppercase tracking-wide text-ink-muted">
                <tr>
                  {COLUMNS.map((column) => (
                    <th
                      key={column.key}
                      className={clsx(
                        "px-4 py-3 font-medium",
                        column.align === "right" ? "text-right" : "text-left",
                        column.hide,
                      )}
                    >
                      <button
                        type="button"
                        onClick={() => toggleSort(column.key)}
                        className={clsx(
                          "inline-flex items-center gap-1 transition-colors hover:text-ink",
                          sort === column.key && "text-ink",
                        )}
                      >
                        {column.label}
                        {sort === column.key ? (
                          direction === "asc" ? (
                            <ArrowUp size={12} />
                          ) : (
                            <ArrowDown size={12} />
                          )
                        ) : null}
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {visible.map((asset) => (
                  <tr key={asset.ticker} className="table-row">
                    <td className="px-4 py-3">
                      <Link to={`/ativos/${asset.ticker}`} className="group flex items-center gap-3">
                        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-surface-hover text-[11px] font-semibold text-ink-secondary">
                          {asset.ticker.slice(0, 4)}
                        </span>
                        {/* Bounded so long FII names cannot push the numeric
                            columns out of the viewport. */}
                        <span className="min-w-0 max-w-[9.5rem] sm:max-w-[260px] xl:max-w-[340px]">
                          <span className="flex items-center gap-2 font-medium text-ink group-hover:text-accent">
                            {asset.ticker}
                            {!asset.is_open ? <Badge>encerrado</Badge> : null}
                            {!asset.has_market_price && asset.is_open ? <Badge tone="warning">sem cotação</Badge> : null}
                          </span>
                          <span className="block truncate text-xs text-ink-muted" title={asset.name}>
                            {asset.name}
                          </span>
                        </span>
                      </Link>
                    </td>
                    <td className="hidden px-4 py-3 text-right xl:table-cell">
                      {/* Quoted in the asset's own currency, like the price it
                          sits beside — a US holding was bought in dollars. */}
                      <span className="tnum block text-ink-secondary">
                        {money(asset.average_price, { currency: asset.currency })}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right">
                      <span className="tnum block font-medium">
                        {money(asset.market_value_base)}
                      </span>
                      <span className="tnum block text-xs text-ink-muted">
                        {quantity(asset.quantity)} × {money(asset.current_price, { currency: asset.currency })}
                        {asset.is_foreign ? ` = ${money(asset.market_value, { currency: asset.currency })}` : ""}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right">
                      <span
                        className={clsx(
                          "tnum block font-medium",
                          asset.unrealized_pnl >= 0 ? "text-positive" : "text-negative",
                        )}
                      >
                        {money(asset.unrealized_pnl_base)}
                      </span>
                      <span className="tnum block text-xs text-ink-muted">{percent(asset.unrealized_pct, 2, true)}</span>
                    </td>
                    <td className="tnum hidden px-4 py-3 text-right text-ink-secondary xl:table-cell">
                      {money(asset.income_base)}
                    </td>
                    <td className="hidden px-4 py-3 text-right md:table-cell">
                      <span
                        className={clsx(
                          "tnum block font-medium",
                          asset.total_return >= 0 ? "text-positive" : "text-negative",
                        )}
                      >
                        {money(asset.total_return_base)}
                      </span>
                      <span className="tnum block text-xs text-ink-muted">{percent(asset.total_return_pct, 1, true)}</span>
                    </td>
                    <td className="hidden px-4 py-3 text-right md:table-cell">
                      <span className="tnum text-ink-secondary">{percent(asset.allocation_pct, 1)}</span>
                      <span className="mt-1 block h-1 w-full overflow-hidden rounded-full bg-surface-hover">
                        <span
                          className="block h-full rounded-full bg-accent transition-all duration-500 ease-premium"
                          style={{ width: `${Math.min(asset.allocation_pct, 100)}%` }}
                        />
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {!isLoading && rows.length ? (
          <div className="px-4 pb-3">
            <Pager
              page={current}
              pages={pages}
              total={rows.length}
              pageSize={pageSize}
              onChange={setPage}
              noun="ativos"
              pageSizeOptions={PAGE_SIZES}
              onPageSizeChange={setPageSize}
            />
          </div>
        ) : null}
      </Card>
    </div>
  );
}
