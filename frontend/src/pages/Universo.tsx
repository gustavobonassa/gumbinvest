/**
 * Universo de ativos — screen every listed paper, not just the ones held.
 *
 * Everything shown here is computed locally from the B3 and CVM files the
 * ingest downloads, so a blank cell means "not published", never zero. The
 * table says so explicitly rather than printing a dash that could be read as a
 * value, and every fundamentals column carries the financial year it came from.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Database, Radar, Search } from "lucide-react";

import { api, type UniverseRow } from "@/lib/api";
import { decimal, money, percent, shortDate } from "@/lib/format";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  Pager,
  Skeleton,
  Tabs,
} from "@/components/ui";
import PortfolioFitPanel from "@/components/PortfolioFit";
import UniverseJobs from "@/components/UniverseJobs";
import UniverseSettings from "@/components/UniverseSettings";

type TabValue = "ACOES" | "FII" | "ETF" | "BDR" | "STOCKS" | "REITS" | "COMPARAR" | "SINCRONIZAR";

const TABS: { value: TabValue; label: string }[] = [
  { value: "ACOES", label: "Ações" },
  { value: "FII", label: "FIIs" },
  { value: "ETF", label: "ETFs" },
  { value: "BDR", label: "BDRs" },
  { value: "STOCKS", label: "Stocks (EUA)" },
  { value: "REITS", label: "REITs (EUA)" },
  { value: "COMPARAR", label: "Como me comparo" },
  { value: "SINCRONIZAR", label: "Sincronização" },
];

/** Instrument families per table tab. The two non-table tabs are excluded. */
type TableTab = Exclude<TabValue, "COMPARAR" | "SINCRONIZAR">;

const KINDS: Record<TableTab, string[]> = {
  ACOES: ["STOCK", "UNIT"],
  FII: ["FII"],
  ETF: ["ETF", "ETF_INTL"],
  BDR: ["BDR"],
  STOCKS: ["STOCK_INTL"],
  REITS: ["REIT"],
};

/**
 * What each class is ranked by when you arrive.
 *
 * This has to differ by class, and getting it wrong is invisible: ETFs and
 * BDRs have no market capitalisation — there is no company behind them filing
 * a balance sheet — so ordering them by it returns *nothing*, because the
 * screener excludes rows missing the column it ranks on. Liquidity is the
 * figure every traded paper has.
 */
const DEFAULT_ORDER: Record<TableTab, string> = {
  ACOES: "valor_de_mercado",
  FII: "valor_de_mercado",
  ETF: "liquidez_media_diaria",
  BDR: "liquidez_media_diaria",
  // The US rows have no market capitalisation and no liquidity: the SEC
  // publishes filings, not prices. Revenue is the size ranking there.
  STOCKS: "receita",
  REITS: "receita",
};


type Column = {
  key: string;
  label: string;
  align?: "right";
  render: (row: UniverseRow) => React.ReactNode;
  /** Columns that only make sense for one family. */
  only?: TabValue[];
};

/** A missing figure is stated, not dashed away as if it were a value. */
const absent = <span className="text-ink-muted" title="não publicado">sem dado</span>;

const num = (value: number | null, format: (v: number) => string) =>
  value === null || value === undefined ? absent : <span className="tnum">{format(value)}</span>;

const COLUMNS: Column[] = [
  {
    key: "valor_de_mercado",
    label: "Valor de mercado",
    align: "right",
    render: (row) => num(row.valor_de_mercado, (v) => money(v, { compact: true })),
    only: ["ACOES", "FII"],
  },
  {
    key: "liquidez_media_diaria",
    label: "Liquidez (21d)",
    align: "right",
    render: (row) => num(row.liquidez_media_diaria, (v) => money(v, { compact: true })),
    only: ["ACOES", "FII", "ETF", "BDR"],
  },
  {
    key: "preco",
    label: "Preço",
    align: "right",
    render: (row) => num(row.preco, (v) => money(v)),
    only: ["ACOES", "FII", "ETF", "BDR"],
  },
  {
    key: "variacao_12m_pct",
    label: "12 meses",
    align: "right",
    only: ["ACOES", "FII", "ETF", "BDR"],
    render: (row) =>
      row.variacao_12m_pct === null ? (
        absent
      ) : (
        <span className={`tnum ${row.variacao_12m_pct >= 0 ? "text-positive" : "text-negative"}`}>
          {percent(row.variacao_12m_pct, 1, true)}
        </span>
      ),
  },
  {
    key: "volatilidade_12m_pct",
    label: "Volatilidade",
    align: "right",
    render: (row) => num(row.volatilidade_12m_pct, (v) => percent(v, 0)),
    only: ["ETF", "BDR"],
  },
  {
    key: "p_l",
    label: "P/L",
    align: "right",
    render: (row) => num(row.p_l, (v) => decimal(v, 1)),
    only: ["ACOES"],
  },
  {
    key: "p_vp",
    label: "P/VP",
    align: "right",
    render: (row) => num(row.p_vp, (v) => decimal(v, 2)),
    only: ["ACOES", "FII"],
  },
  {
    key: "dividend_yield_pct",
    label: "DY",
    align: "right",
    render: (row) => num(row.dividend_yield_pct, (v) => percent(v, 1)),
    only: ["ACOES", "FII"],
  },
  {
    key: "roe_pct",
    label: "ROE",
    align: "right",
    render: (row) => num(row.roe_pct, (v) => percent(v, 1)),
    only: ["ACOES", "BDR", "STOCKS", "REITS"],
  },
  {
    key: "margem_liquida_pct",
    label: "Margem líq.",
    align: "right",
    render: (row) => num(row.margem_liquida_pct, (v) => percent(v, 1)),
    only: ["ACOES", "STOCKS", "REITS"],
  },
  {
    key: "divida_sobre_patrimonio",
    label: "Dív./PL",
    align: "right",
    render: (row) => num(row.divida_sobre_patrimonio, (v) => decimal(v, 2)),
    only: ["ACOES", "STOCKS", "REITS"],
  },
  {
    key: "receita",
    label: "Receita (12m)",
    align: "right",
    render: (row) => num(row.receita, (v) => money(v, { compact: true, currency: "USD" })),
    only: ["STOCKS", "REITS"],
  },
  {
    key: "lucro_liquido",
    label: "Lucro (12m)",
    align: "right",
    render: (row) => num(row.lucro_liquido, (v) => money(v, { compact: true, currency: "USD" })),
    only: ["STOCKS", "REITS"],
  },
  {
    key: "vintage",
    label: "Dados de",
    render: (row) => (
      <span className="block text-xs leading-tight">
        {row.preco_em ? (
          <span className="block text-ink-secondary">cotação {shortDate(row.preco_em)}</span>
        ) : null}
        {row.exercicio_dos_fundamentos ? (
          <span className="block text-ink-muted">balanço {row.exercicio_dos_fundamentos}</span>
        ) : (
          <span className="block text-ink-muted">sem balanço</span>
        )}
      </span>
    ),
  },
  {
    key: "patrimonio_do_fundo",
    label: "Patrimônio",
    align: "right",
    render: (row) => num(row.patrimonio_do_fundo, (v) => money(v, { compact: true })),
    only: ["FII"],
  },
];

/** The B3 indices worth offering as a filter, in the order people ask for them. */
const INDEXES: { code: string; label: string }[] = [
  { code: "IBOV", label: "Ibovespa" },
  { code: "IBXX", label: "IBrX 100" },
  { code: "IBRA", label: "IBrA (amplo)" },
  { code: "SMLL", label: "Small Caps" },
  { code: "MLCX", label: "MidLarge Cap" },
  { code: "IDIV", label: "Dividendos" },
  { code: "IFIX", label: "FIIs (IFIX)" },
  { code: "BDRX", label: "BDRs (BDRX)" },
  { code: "IGCX", label: "Governança" },
  { code: "ICON", label: "Consumo" },
  { code: "IEEX", label: "Energia Elétrica" },
  { code: "IFNC", label: "Financeiro" },
  { code: "IMAT", label: "Materiais Básicos" },
  { code: "UTIL", label: "Utilidade Pública" },
];

const PAGE_SIZES = [25, 50, 100];

export default function Universo() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("aba") as TabValue) ?? "ACOES";
  const [text, setText] = useState("");
  const [orderBy, setOrderBy] = useState<string | null>(null);
  const [descending, setDescending] = useState(true);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [minDy, setMinDy] = useState("");
  const [maxPe, setMaxPe] = useState("");
  const [maxPb, setMaxPb] = useState("");
  const [index, setIndex] = useState("");

  const setTab = (next: TabValue) => {
    const copy = new URLSearchParams(params);
    if (next === "ACOES") copy.delete("aba");
    else copy.set("aba", next);
    setParams(copy, { replace: true });
  };

  // A tab change drops a manual sort: the column that made sense for ações
  // may not exist for ETFs, and silently returning nothing is how this went
  // wrong in the first place.
  const effectiveOrder = orderBy ?? DEFAULT_ORDER[tab as TableTab] ?? "liquidez_media_diaria";
  // The human name of the sort column — echoing the raw key produced
  // unaccented pseudo-Portuguese ("divida sobre patrimonio").
  const orderLabel =
    COLUMNS.find((column) => column.key === effectiveOrder)?.label ?? effectiveOrder.replace(/_/g, " ");
  useEffect(() => setOrderBy(null), [tab]);
  useEffect(
    () => setPage(1),
    [tab, text, effectiveOrder, descending, pageSize, minDy, maxPe, maxPb, index],
  );

  const statusQ = useQuery({ queryKey: ["universe-status"], queryFn: api.universeStatus });
  const enabled = Boolean(statusQ.data?.settings?.enabled);
  const total = statusQ.data?.coverage?.total ?? 0;

  const listQ = useQuery({
    queryKey: ["universe", tab, text, effectiveOrder, descending, page, pageSize, minDy, maxPe, maxPb, index],
    queryFn: () =>
      api.universe({
        kind: KINDS[tab as Exclude<TabValue, "COMPARAR" | "SINCRONIZAR">],
        text: text || undefined,
        order_by: effectiveOrder,
        index: index || undefined,
        descending,
        limit: pageSize,
        offset: (page - 1) * pageSize,
        min_dy: minDy ? Number(minDy) : undefined,
        max_pe: maxPe ? Number(maxPe) : undefined,
        max_pb: maxPb ? Number(maxPb) : undefined,
      }),
    enabled: enabled && total > 0 && tab !== "COMPARAR" && tab !== "SINCRONIZAR",
  });

  const columns = useMemo(
    () => COLUMNS.filter((column) => !column.only || column.only.includes(tab)),
    [tab],
  );

  const toggleSort = (key: string) => {
    if (key === effectiveOrder) setDescending((value) => !value);
    else {
      setOrderBy(key);
      setDescending(true);
    }
  };

  const header = (
    <div>
      <p className="text-sm text-ink-muted">Ferramentas</p>
      <h1 className="mt-1 text-2xl font-semibold tracking-tight">Universo de ativos</h1>
    </div>
  );

  if (statusQ.isError) {
    return (
      <div className="space-y-6">
        {header}
        <ErrorState error={statusQ.error} retry={() => statusQ.refetch()} />
      </div>
    );
  }

  if (!statusQ.data) {
    return (
      <div className="space-y-6">
        {header}
        <Skeleton className="h-80 w-full" />
      </div>
    );
  }

  // The universe starts empty on a fresh install, so the sync tab has to be
  // reachable before there is anything to screen — it is where you fill it.
  if ((!enabled || total === 0) && tab !== "SINCRONIZAR") {
    return (
      <div className="space-y-6">
        {header}
        <Tabs value={tab} onChange={setTab} options={TABS} />
        <Card className="p-5">
          <EmptyState
            icon={Radar}
            title={enabled ? "O universo ainda não foi baixado" : "O universo de ativos está desativado"}
            description={
              enabled
                ? "Baixe os arquivos públicos da B3 e da CVM para montar a tabela local. Leva poucos minutos e roda em segundo plano."
                : "Ative o universo na aba Sincronização para filtrar todos os papéis listados por critérios objetivos e para dar à IA uma lista real de candidatos."
            }
            action={
              /* Enabled or not, the answer is the same tab now: it holds both
                 the switch and the button. */
              <button type="button" className="btn-primary" onClick={() => setTab("SINCRONIZAR")}>
                Ir para a sincronização
              </button>
            }
          />
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {header}

      <Tabs value={tab} onChange={setTab} options={TABS} />

      {tab === "SINCRONIZAR" ? (
        /* Settings first, then the run: turning the universe on and choosing
           which markets it covers is what makes a sync possible, and having
           the switch on a different screen from the button it enables was the
           confusing part. */
        <div className="space-y-6">
          <UniverseSettings />
          <UniverseJobs />
        </div>
      ) : tab === "COMPARAR" ? (
        <PortfolioFitPanel />
      ) : (
        <>
          <Card className="p-4">
            <div className="flex flex-wrap items-end gap-3">
              <label className="relative min-w-56 flex-1">
                <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted" />
                <input
                  className="input pl-9"
                  placeholder="Buscar por código ou nome"
                  value={text}
                  onChange={(event) => setText(event.target.value)}
                />
              </label>
              <label className="text-xs text-ink-muted">
                Índice
                <select
                  className="input mt-1 w-40"
                  value={index}
                  onChange={(event) => setIndex(event.target.value)}
                >
                  <option value="">Todos</option>
                  {INDEXES.map((item) => (
                    <option key={item.code} value={item.code}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-xs text-ink-muted">
                DY mínimo (%)
                <input
                  className="input mt-1 w-28"
                  inputMode="decimal"
                  value={minDy}
                  onChange={(event) => setMinDy(event.target.value)}
                />
              </label>
              <label className="text-xs text-ink-muted">
                P/L máximo
                <input
                  className="input mt-1 w-28"
                  inputMode="decimal"
                  value={maxPe}
                  onChange={(event) => setMaxPe(event.target.value)}
                />
              </label>
              <label className="text-xs text-ink-muted">
                P/VP máximo
                <input
                  className="input mt-1 w-28"
                  inputMode="decimal"
                  value={maxPb}
                  onChange={(event) => setMaxPb(event.target.value)}
                />
              </label>
            </div>
            <p className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
              <Database size={13} />
              <span>
                {total.toLocaleString("pt-BR")} ativos ·{" "}
                {(statusQ.data.coverage?.with_fundamentals ?? 0).toLocaleString("pt-BR")} com
                fundamentos
              </span>
              <span>
                Indicadores em UDM (últimos doze meses) até o trimestre mais recente de cada
                empresa. O período de cada um aparece sob o P/L.
              </span>
              {listQ.data?.dropped_for_missing_data ? (
                <span className="text-warning">
                  {listQ.data.dropped_for_missing_data} fora da lista por não terem “
                  {orderLabel}” publicado
                </span>
              ) : null}
            </p>
          </Card>

          {listQ.isError ? (
            <ErrorState error={listQ.error} retry={() => listQ.refetch()} />
          ) : listQ.isLoading || !listQ.data ? (
            <div className="space-y-2">
              {Array.from({ length: 8 }).map((_, index) => (
                <Skeleton key={index} className="h-12 w-full" />
              ))}
            </div>
          ) : listQ.data.items.length === 0 ? (
            <Card className="p-5">
              <EmptyState
                icon={Search}
                title="Nenhum ativo com esses filtros"
                description={
                  listQ.data.dropped_for_missing_data > 0
                    ? `Todos os ${listQ.data.dropped_for_missing_data} ativos desta classe ficaram de fora por não terem “${orderLabel}” publicado. Ordene por outra coluna: ETFs e BDRs não têm balanço próprio, então valor de mercado, P/L e P/VP não existem para eles.`
                    : "Afrouxe os limites ou limpe a busca."
                }
              />
            </Card>
          ) : (
            <>
              <Card className="overflow-hidden p-0">
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[900px] text-sm">
                    <thead>
                      <tr className="border-b border-line text-xs text-ink-muted">
                        <th className="px-4 py-3 text-left font-medium">Ativo</th>
                        {columns.map((column) => (
                          <th
                            key={column.key}
                            className={`px-4 py-3 font-medium ${column.align === "right" ? "text-right" : "text-left"}`}
                          >
                            <button
                              type="button"
                              className="inline-flex items-center gap-1 hover:text-ink"
                              onClick={() => toggleSort(column.key)}
                            >
                              {column.label}
                              {effectiveOrder === column.key ? (
                                descending ? (
                                  <ArrowDown size={12} />
                                ) : (
                                  <ArrowUp size={12} />
                                )
                              ) : null}
                            </button>
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {listQ.data.items.map((row) => (
                        <tr key={row.ticker} className="table-row">
                          <td className="px-4 py-3">
                            <span className="flex items-center gap-2">
                              {/* Any ticker opens its page — a paper you do not
                                  own is exactly the one worth reading about.
                                  Opening it does not enrol it in the scheduled
                                  refresh; see market.service.tracked_asset_ids. */}
                              <Link className="font-medium hover:text-accent" to={`/ativos/${row.ticker}`}>
                                {row.ticker}
                              </Link>
                              {row.na_carteira ? <Badge tone="accent">na carteira</Badge> : null}
                              {row.na_carteira_ia ? <Badge tone="neutral">carteira IA</Badge> : null}
                              {!row.na_carteira && !row.na_carteira_ia && row.na_watchlist ? (
                                <Badge tone="neutral">watchlist</Badge>
                              ) : null}
                            </span>
                            <span className="mt-0.5 block truncate text-xs text-ink-muted">
                              {row.name}
                              {row.sector ? ` · ${row.sector}` : ""}
                              {row.segmento_do_fundo ? ` · ${row.segmento_do_fundo}` : ""}
                            </span>
                          </td>
                          {columns.map((column) => (
                            <td
                              key={column.key}
                              className={`px-4 py-3 ${column.align === "right" ? "text-right" : ""}`}
                            >
                              {column.render(row)}

                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>

              <Pager
                page={page}
                pages={Math.max(Math.ceil(listQ.data.total / pageSize), 1)}
                total={listQ.data.total}
                pageSize={pageSize}
                onChange={setPage}
                noun="ativos"
                pageSizeOptions={PAGE_SIZES}
                onPageSizeChange={setPageSize}
              />
            </>
          )}
        </>
      )}
    </div>
  );
}
