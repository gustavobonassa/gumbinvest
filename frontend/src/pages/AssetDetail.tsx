/** Everything about one asset: metrics, price chart, dividends, movements. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  ArrowLeft,
  ArrowLeftRight,
  Coins,
  LayoutGrid,
  RefreshCw,
  Save,
  Scale,
  Search,
  SlidersHorizontal,
  Star,
  Wallet,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import AssetChat from "@/components/AssetChat";
import { ChartFrame, DividendPerShareBars, IncomeBars, PriceLine, YearlyFinancialsBars } from "@/components/charts";
import { useToast } from "@/components/Toast";
import {
  Badge,
  Card,
  DateField,
  EmptyState,
  ErrorState,
  KindTag,
  Modal,
  Pager,
  SectionTitle,
  Segmented,
  Select,
  Skeleton,
  StatTile,
  Tabs,
} from "@/components/ui";
import { api, type AssetDetail as AssetDetailData, type TransactionRow } from "@/lib/api";
import {
  baseCurrency,
  dateTime,
  decimal,
  fxRate,
  money,
  opLabel,
  percent,
  quantity,
  shortDate,
} from "@/lib/format";

const PAGE_SIZES = [15, 25, 0];

type PriceRange = "1m" | "6m" | "1y" | "5y" | "max";
const PRICE_RANGE_MONTHS: Record<PriceRange, number | null> = {
  "1m": 1,
  "6m": 6,
  "1y": 12,
  "5y": 60,
  max: null,
};

/**
 * The asset's ledger. The whole history arrives with the asset, so filtering and
 * paging happen here — no round trip, and the filters can be built from the
 * movements this asset actually has rather than from every type that exists.
 */
function MovementsCard({ transactions, currency }: { transactions: TransactionRow[]; currency: string }) {
  const [search, setSearch] = useState("");
  const [opTypes, setOpTypes] = useState<string[]>([]);
  const [broker, setBroker] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [pageSize, setPageSize] = useState(15);
  const [page, setPage] = useState(1);

  const inNative = (value: unknown) => money(value, { currency });

  const availableOps = useMemo(
    () => [...new Set(transactions.map((item) => item.op_type))],
    [transactions],
  );
  const brokers = useMemo(
    () => [...new Set(transactions.map((item) => item.broker).filter(Boolean))] as string[],
    [transactions],
  );

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return transactions.filter((item) => {
      if (opTypes.length && !opTypes.includes(item.op_type)) return false;
      if (broker && item.broker !== broker) return false;
      // Dates are ISO, so a string compare is the date compare.
      if (start && item.date < start) return false;
      if (end && item.date > end) return false;
      if (needle) {
        const haystack = `${item.movement} ${opLabel(item.op_type)} ${item.broker ?? ""} ${item.notes ?? ""}`;
        if (!haystack.toLowerCase().includes(needle)) return false;
      }
      return true;
    });
  }, [transactions, search, opTypes, broker, start, end]);

  useEffect(() => setPage(1), [search, opTypes, broker, start, end, pageSize]);

  const size = pageSize > 0 ? pageSize : Math.max(rows.length, 1);
  const pages = Math.max(1, Math.ceil(rows.length / size));
  const current = Math.min(page, pages);
  const visible = rows.slice((current - 1) * size, current * size);

  const hasFilters = Boolean(search || opTypes.length || broker || start || end);
  const clear = () => {
    setSearch("");
    setOpTypes([]);
    setBroker("");
    setStart("");
    setEnd("");
  };

  const toggleOp = (value: string) =>
    setOpTypes((currentOps) =>
      currentOps.includes(value) ? currentOps.filter((item) => item !== value) : [...currentOps, value],
    );

  return (
    <Card className="p-5">
      <SectionTitle
        title="Histórico de movimentações"
        subtitle={
          hasFilters
            ? `${rows.length} de ${transactions.length} lançamentos`
            : `${transactions.length} lançamentos importados`
        }
      />

      <div className="mb-4 space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative min-w-[220px] flex-1">
            <Search
              size={15}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted"
            />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Buscar movimento, corretora ou observação"
              className="input pl-9"
            />
          </div>
          {brokers.length > 1 ? (
            <Select
              ariaLabel="Corretora"
              className="w-auto min-w-[160px]"
              value={broker}
              onChange={setBroker}
              options={[
                { value: "", label: "Todas as corretoras" },
                ...brokers.map((item) => ({ value: item, label: item })),
              ]}
            />
          ) : null}
          <DateField ariaLabel="Data inicial" placeholder="Data inicial" value={start} onChange={setStart} />
          <DateField ariaLabel="Data final" placeholder="Data final" value={end} onChange={setEnd} />
          {hasFilters ? (
            <button type="button" onClick={clear} className="btn-ghost">
              <X size={14} /> Limpar
            </button>
          ) : null}
        </div>

        {availableOps.length > 1 ? (
          <div className="flex flex-wrap gap-1.5">
            {availableOps.map((type) => (
              <button
                key={type}
                type="button"
                onClick={() => toggleOp(type)}
                aria-pressed={opTypes.includes(type)}
                className={clsx(
                  "rounded-full border px-2.5 py-1 text-xs font-medium transition-all duration-200 ease-premium",
                  opTypes.includes(type)
                    ? "border-accent/40 bg-accent-soft text-ink"
                    : "border-line bg-surface-raised text-ink-muted hover:text-ink-secondary",
                )}
              >
                {opLabel(type)}
              </button>
            ))}
          </div>
        ) : null}
      </div>

      {!rows.length ? (
        <EmptyState
          title="Nenhuma movimentação encontrada"
          description="Ajuste os filtros para ver os lançamentos deste ativo."
        />
      ) : (
        <>
          <div className="-mx-2 overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead className="bg-surface text-left text-xs uppercase tracking-wide text-ink-muted">
                <tr>
                  <th className="px-3 py-2">Data</th>
                  <th className="px-3 py-2">Operação</th>
                  <th className="px-3 py-2 text-right">Qtd.</th>
                  <th className="px-3 py-2 text-right">Preço</th>
                  <th className="px-3 py-2 text-right">Total ({currency})</th>
                  <th className="px-3 py-2">Corretora</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((transaction) => (
                  <tr key={transaction.id} className="table-row">
                    <td className="whitespace-nowrap px-3 py-2 text-ink-secondary">{shortDate(transaction.date)}</td>
                    <td className="px-3 py-2">
                      <span className="font-medium">{opLabel(transaction.op_type)}</span>
                      <span className="block text-xs text-ink-muted">{transaction.movement}</span>
                    </td>
                    <td className="tnum px-3 py-2 text-right">{quantity(transaction.quantity)}</td>
                    <td className="tnum px-3 py-2 text-right">{inNative(transaction.unit_price)}</td>
                    <td
                      className={clsx(
                        "tnum px-3 py-2 text-right",
                        Number(transaction.net_amount) > 0 && "text-positive",
                        Number(transaction.net_amount) < 0 && "text-negative",
                      )}
                    >
                      {inNative(transaction.gross_amount)}
                    </td>
                    <td className="px-3 py-2 text-xs text-ink-muted">{transaction.broker ?? "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-3">
            <Pager
              page={current}
              pages={pages}
              total={rows.length}
              pageSize={pageSize}
              onChange={setPage}
              noun="lançamentos"
              pageSizeOptions={PAGE_SIZES}
              onPageSizeChange={setPageSize}
            />
          </div>
        </>
      )}
    </Card>
  );
}

/** Every payment this asset made, newest first. */
function DividendsCard({ data }: { data: AssetDetailData }) {
  const [pageSize, setPageSize] = useState(15);
  const [page, setPage] = useState(1);

  const inNative = (value: unknown) => money(value, { currency: data.currency });
  const rows = [...data.dividends].sort((a, b) => (a.date < b.date ? 1 : -1));

  useEffect(() => setPage(1), [pageSize]);

  const size = pageSize > 0 ? pageSize : Math.max(rows.length, 1);
  const pages = Math.max(1, Math.ceil(rows.length / size));
  const current = Math.min(page, pages);
  const visible = rows.slice((current - 1) * size, current * size);

  if (!rows.length) {
    return (
      <EmptyState
        icon={Coins}
        title="Nenhum provento registrado"
        description="Este ativo ainda não pagou proventos no histórico."
      />
    );
  }

  return (
    <Card className="p-5">
      <SectionTitle title="Pagamentos" subtitle={`${rows.length} proventos recebidos`} />
      <div className="-mx-2 overflow-x-auto">
        <table className="w-full min-w-[520px] text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-ink-muted">
            <tr>
              <th className="px-3 py-2">Data</th>
              <th className="px-3 py-2">Tipo</th>
              <th className="px-3 py-2 text-right">Qtd.</th>
              <th className="px-3 py-2 text-right">Por unidade</th>
              <th className="px-3 py-2 text-right">Total</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((payment, index) => (
              <tr key={`${payment.date}-${payment.op_type}-${index}`} className="table-row">
                <td className="whitespace-nowrap px-3 py-2 text-ink-secondary">{shortDate(payment.date)}</td>
                <td className="px-3 py-2">{opLabel(payment.op_type)}</td>
                <td className="tnum px-3 py-2 text-right text-ink-secondary">
                  {payment.quantity ? quantity(payment.quantity) : "-"}
                </td>
                <td className="tnum px-3 py-2 text-right text-ink-secondary">
                  {payment.unit_price ? inNative(payment.unit_price) : "-"}
                </td>
                <td className="tnum px-3 py-2 text-right font-medium text-positive">{inNative(payment.amount)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-3">
        <Pager
          page={current}
          pages={pages}
          total={rows.length}
          pageSize={pageSize}
          onChange={setPage}
          noun="pagamentos"
          pageSizeOptions={PAGE_SIZES}
          onPageSizeChange={setPageSize}
        />
      </div>
    </Card>
  );
}

/** One fact of a group: label left, value right, hairline between rows.
 *
 * A definition list, not a box per number — fourteen bordered boxes in a grid
 * read as noise, while grouped rows give the eye one scan line per fact and a
 * heading that says what the group answers.
 */
type Fact = {
  label: string;
  value?: number | string | null;
  hint?: string;
  tone?: "positive" | "negative";
};

function FactRow({ label, value, hint, tone }: Fact) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-line/60 py-2 last:border-0">
      <span className="shrink-0 text-[13px] text-ink-muted">{label}</span>
      <span className="min-w-0 text-right">
        <span
          className={clsx(
            "tnum block text-sm font-medium",
            tone === "positive" && "text-positive",
            tone === "negative" && "text-negative",
            !tone && "text-ink",
          )}
        >
          {value}
        </span>
        {hint ? <span className="mt-0.5 block text-[11px] leading-snug text-ink-muted">{hint}</span> : null}
      </span>
    </div>
  );
}

const growthTone = (value?: number) =>
  value === undefined ? undefined : value >= 0 ? "positive" : "negative";

/**
 * What the company behind the ticker earns, is worth and pays.
 *
 * Cached server-side and fetched per asset, so this never blocks the page: the
 * position numbers above render immediately and the company data fills in.
 */
function FundamentalsCard({ ticker, currency }: { ticker: string; currency: string }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [expanded, setExpanded] = useState(false);
  const { data, isLoading } = useQuery({
    queryKey: ["fundamentals", ticker],
    queryFn: () => api.assetFundamentals(ticker),
    staleTime: 60 * 60_000,
  });
  const refresh = useMutation({
    mutationFn: () => api.assetFundamentals(ticker, true),
    onSuccess: (fresh) => {
      queryClient.setQueryData(["fundamentals", ticker], fresh);
      toast.success("Dados da empresa atualizados.");
    },
    onError: (error) => toast.error("Não foi possível atualizar os dados da empresa.", error),
  });

  if (isLoading) return <Skeleton className="h-56 w-full" />;
  if (!data?.supported) return null;

  const f = data.data;
  if (!f) {
    return (
      <Card className="p-5">
        <SectionTitle title="Sobre a empresa" subtitle="Nenhum dado fundamentalista disponível para este papel" />
      </Card>
    );
  }

  // Statements are reported in the company's own currency, which for a BDR or
  // an ADR is not the currency the paper trades in.
  const reported = f.currency ?? currency;
  const big = (value?: number) =>
    value === undefined ? undefined : money(value, { currency: reported, compact: true, decimals: 0 });
  const pct = (value?: number, decimals = 1) => (value === undefined ? undefined : percent(value, decimals));

  const upcoming = f.announced_dividends ?? [];

  return (
    <Card className="p-5">
      <SectionTitle
        title="Sobre a empresa"
        subtitle={[f.sector, f.industry].filter(Boolean).join(" · ") || undefined}
        action={
          <button
            type="button"
            className="btn-ghost px-2 py-1 text-xs"
            onClick={() => refresh.mutate()}
            disabled={refresh.isPending}
          >
            <RefreshCw size={13} className={refresh.isPending ? "animate-spin" : undefined} /> Atualizar
          </button>
        }
      />

      {/* Market cap, the 52-week range and the analyst target live in the
          price strip at the top of the tab; here the facts are grouped by the
          question they answer and read as rows, not boxes. */}
      <div className="grid gap-x-10 gap-y-6 md:grid-cols-2 xl:grid-cols-4">
        {(
          [
            {
              title: "Resultados (12 meses)",
              rows: [
                { label: "Receita", value: big(f.revenue), hint: pct(f.revenue_growth) ? `${pct(f.revenue_growth)} a/a` : undefined },
                { label: "Lucro líquido", value: big(f.net_income), tone: growthTone(f.net_income) },
                { label: "EBITDA", value: big(f.ebitda) },
                { label: "Margem líquida", value: pct(f.profit_margin), tone: growthTone(f.profit_margin) },
                { label: "ROE", value: pct(f.return_on_equity), tone: growthTone(f.return_on_equity) },
              ],
            },
            {
              title: "Valuation",
              rows: [
                {
                  label: "P/L",
                  value: f.pe_trailing === undefined ? undefined : decimal(f.pe_trailing, 2),
                  hint: f.pe_forward === undefined ? undefined : `projetado ${decimal(f.pe_forward, 2)}`,
                },
                { label: "P/VP", value: f.price_to_book === undefined ? undefined : decimal(f.price_to_book, 2) },
                { label: "LPA", value: f.eps_trailing === undefined ? undefined : money(f.eps_trailing, { currency: reported }) },
                { label: "Dívida / patrimônio", value: f.debt_to_equity === undefined ? undefined : decimal(f.debt_to_equity, 1) },
              ],
            },
            {
              title: "Dividendos",
              rows: [
                {
                  label: "Dividend yield",
                  value: pct(f.dividend_yield, 2),
                  hint: f.dividend_rate === undefined ? undefined : `${money(f.dividend_rate, { currency })} por cota/ação ao ano`,
                  tone: f.dividend_yield === undefined ? undefined : ("positive" as const),
                },
                { label: "Payout", value: pct(f.payout_ratio), hint: "do lucro distribuído" },
                { label: "Data-com mais recente", value: f.ex_dividend_date ? shortDate(f.ex_dividend_date) : undefined },
              ],
            },
            {
              title: "Analistas e agenda",
              rows: [
                {
                  label: "Recomendação",
                  value: f.recommendation,
                  hint: f.analyst_count ? `${f.analyst_count} analistas` : undefined,
                },
                { label: "Próximo resultado", value: f.earnings_dates?.length ? shortDate(f.earnings_dates[0]) : undefined },
              ],
            },
          ] as { title: string; rows: Fact[] }[]
        )
          .map((group) => ({
            ...group,
            rows: group.rows.filter((row) => row.value !== undefined && row.value !== null && row.value !== ""),
          }))
          .filter((group) => group.rows.length)
          .map((group) => (
            <div key={group.title}>
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">{group.title}</p>
              {group.rows.map((row) => (
                <FactRow key={row.label} {...row} />
              ))}
            </div>
          ))}
      </div>

      {upcoming.length ? (
        <div className="mt-5">
          <p className="mb-2 text-sm font-medium text-ink">Proventos anunciados</p>
          <ul className="space-y-1.5">
            {upcoming.map((payment, index) => (
              <li
                key={`${payment.payment_date}-${index}`}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg bg-surface-raised/50 px-3 py-2 text-sm"
              >
                <span className="tnum w-[74px] shrink-0 text-xs text-ink-muted">
                  {payment.payment_date ? shortDate(payment.payment_date) : "a definir"}
                </span>
                {payment.label ? <Badge>{payment.label}</Badge> : null}
                {payment.period ? <span className="text-xs text-ink-muted">{payment.period}</span> : null}
                {payment.record_date ? (
                  <span className="text-xs text-ink-muted">data-com {shortDate(payment.record_date)}</span>
                ) : null}
                <span className="tnum ml-auto font-medium text-positive">
                  {money(payment.rate, { currency, decimals: 6 })} por cota
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : f.next_dividend_date ? (
        <p className="mt-4 text-sm text-ink-secondary">
          Próximo pagamento previsto para <strong className="text-ink">{shortDate(f.next_dividend_date)}</strong>.
        </p>
      ) : null}

      {f.summary ? (
        <div className="mt-5 border-t border-line pt-4">
          <p className="text-sm leading-relaxed text-ink-secondary">
            {expanded || f.summary.length <= 420 ? f.summary : `${f.summary.slice(0, 420)}…`}
          </p>
          <div className="mt-2 flex items-center gap-4 text-xs">
            {f.summary.length > 420 ? (
              <button type="button" className="text-accent hover:underline" onClick={() => setExpanded((v) => !v)}>
                {expanded ? "Ver menos" : "Ver mais"}
              </button>
            ) : null}
            {f.website ? (
              <a href={f.website} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                Site da empresa ↗
              </a>
            ) : null}
          </div>
        </div>
      ) : null}

      <p className="mt-3 text-xs text-ink-muted">
        Fonte: Yahoo Finance{upcoming.length ? " e B3" : ""}
        {data.fetched_at ? ` · atualizado ${dateTime(data.fetched_at)}` : ""}
        {data.stale ? " · provedor indisponível, exibindo a última cópia" : ""}
      </p>
    </Card>
  );
}

/**
 * Follow / unfollow the asset on the watchlist. Only rendered on watch-only
 * pages: a held position is already followed by definition. The list itself
 * lives in Configurações, same query key, so both screens stay in sync.
 */
function WatchlistButton({ ticker }: { ticker: string }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { data } = useQuery({ queryKey: ["watchlist"], queryFn: api.watchlist, staleTime: 60_000 });
  const entry = data?.find((item) => item.ticker.toUpperCase() === ticker.toUpperCase());
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["watchlist"] });
  const add = useMutation({
    mutationFn: () => api.addWatchlist(ticker),
    onSuccess: () => {
      invalidate();
      toast.success(`${ticker} adicionado à watchlist.`);
    },
    onError: (error) => toast.error("Não foi possível adicionar à watchlist.", error),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.removeWatchlist(id),
    onSuccess: () => {
      invalidate();
      toast.success(`${ticker} removido da watchlist.`);
    },
    onError: (error) => toast.error("Não foi possível remover da watchlist.", error),
  });

  return (
    <button
      type="button"
      disabled={!data || add.isPending || remove.isPending}
      onClick={() => (entry ? remove.mutate(entry.id) : add.mutate())}
      aria-pressed={Boolean(entry)}
      title={entry ? "Remover da watchlist" : "Acompanhar este ativo na watchlist"}
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium transition-all duration-200 ease-premium disabled:opacity-60",
        entry
          ? "border-accent/40 bg-accent-soft text-ink"
          : "border-line bg-surface-raised text-ink-secondary hover:border-accent/40 hover:text-ink",
      )}
    >
      <Star size={13} className={entry ? "fill-current text-accent" : undefined} aria-hidden />
      {entry ? "Na watchlist" : "Adicionar à watchlist"}
    </button>
  );
}

type TabValue = "visao" | "posicao" | "proventos" | "movimentacoes" | "ajustes";

export default function AssetDetail() {
  const { ticker = "" } = useParams();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [notes, setNotes] = useState<string | null>(null);
  const [manualPrice, setManualPrice] = useState<string>("");
  const [realBalance, setRealBalance] = useState<string>("");
  // The tab lives in the URL, so a reload or a shared link opens the same view.
  const [params, setParams] = useSearchParams();
  const requested = params.get("aba");
  const tab: TabValue = (["visao", "posicao", "proventos", "movimentacoes", "ajustes"] as const).includes(
    requested as TabValue,
  )
    ? (requested as TabValue)
    : "visao";
  const setTab = (next: TabValue) =>
    setParams(next === "visao" ? {} : { aba: next }, { replace: true });

  const asset = useQuery({ queryKey: ["asset", ticker], queryFn: () => api.asset(ticker) });
  const prices = useQuery({ queryKey: ["asset-prices", ticker], queryFn: () => api.assetPriceHistory(ticker) });
  // Same key FundamentalsCard uses, so this costs no extra request — it feeds
  // the price-strip tiles that render above the card.
  const fundamentals = useQuery({
    queryKey: ["fundamentals", ticker],
    queryFn: () => api.assetFundamentals(ticker),
    staleTime: 60 * 60_000,
  });

  const fund = fundamentals.data?.supported ? fundamentals.data.data : null;

  const [priceRange, setPriceRange] = useState<PriceRange>("1y");
  const [showAverage, setShowAverage] = useState(true);

  const visiblePrices = useMemo(() => {
    const all = prices.data ?? [];
    const months = PRICE_RANGE_MONTHS[priceRange];
    if (months === null) return all;
    const cut = new Date();
    cut.setMonth(cut.getMonth() - months);
    const iso = `${cut.getFullYear()}-${String(cut.getMonth() + 1).padStart(2, "0")}-${String(cut.getDate()).padStart(2, "0")}`;
    // ISO strings compare as dates.
    return all.filter((point) => point.date >= iso);
  }, [prices.data, priceRange]);

  const priceStats = useMemo(() => {
    if (visiblePrices.length < 2) return null;
    const first = visiblePrices[0];
    const last = visiblePrices[visiblePrices.length - 1];
    const change = last.close - first.close;
    return {
      first,
      last,
      change,
      pct: first.close > 0 ? (change / first.close) * 100 : null,
    };
  }, [visiblePrices]);

  // Declared payout per year plus the yield it represented: rate over that
  // year's closing price (today's price for the running year). Only years the
  // stored history can price get a yield; the payout bar shows regardless.
  const dividendYears = useMemo(() => {
    const rows = fund?.dividends_by_year ?? [];
    if (!rows.length) return [];
    const closes = new Map<number, number>();
    for (const point of prices.data ?? []) closes.set(Number(point.date.slice(0, 4)), point.close);
    const currentYear = new Date().getFullYear();
    const currentPrice = Number(asset.data?.current_price) || undefined;
    return rows.slice(-10).map((row) => {
      const reference = row.year === currentYear ? currentPrice : closes.get(row.year);
      return { ...row, yield_pct: reference ? (row.total_rate / reference) * 100 : null };
    });
  }, [fund?.dividends_by_year, prices.data, asset.data?.current_price]);


  // Local edit state starts from the server value and then owns the field —
  // blending the two (`local || server`) made an emptied input snap back, so
  // a manual price could never be removed.
  useEffect(() => {
    setNotes(asset.data?.user_notes ?? null);
    setManualPrice(asset.data?.manual_price != null ? String(asset.data.manual_price) : "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticker, asset.data?.user_notes, asset.data?.manual_price]);

  const save = useMutation({
    mutationFn: (payload: Record<string, unknown>) => api.updateAsset(ticker, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["asset", ticker] });
      queryClient.invalidateQueries({ queryKey: ["assets"] });
      queryClient.invalidateQueries({ queryKey: ["positions"] });
      toast.success("Ajustes salvos.");
    },
    onError: (error) => toast.error("Não foi possível salvar os ajustes.", error),
  });

  // The adjustment writes a synthetic movement into the ledger, so the user
  // sees the exact delta before committing rather than learning it from the
  // toast afterwards.
  const [reconcilePreview, setReconcilePreview] = useState<number | null>(null);
  // A correction changes the position, which every other figure is built from,
  // so the whole cache goes rather than the three keys above.
  const reconcile = useMutation({
    mutationFn: (value: number) => api.reconcileAsset(ticker, value),
    onSuccess: (result) => {
      setRealBalance("");
      queryClient.invalidateQueries();
      if (result.applied) {
        toast.success(`Ajuste de ${quantity(result.difference)} registrado.`);
      } else {
        toast.info("A posição já corresponde ao saldo informado.");
      }
    },
    onError: (error) => toast.error("Não foi possível ajustar a posição.", error),
  });

  if (asset.isError) return <ErrorState error={asset.error} retry={() => asset.refetch()} />;
  if (asset.isLoading || !asset.data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-56" />
        <div className="grid gap-4 sm:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <Skeleton key={index} className="h-28" />
          ))}
        </div>
        <Skeleton className="h-72" />
      </div>
    );
  }

  const data = asset.data;
  // Watch-only: the market knows this paper, the wallet never traded it. The
  // whole wallet layer (tabs, average line, adjustments) disappears — what is
  // left is exactly the company view, which is why the page exists at all.
  const held = data.held !== false;
  const activeTab: TabValue = held ? tab : "visao";
  // A foreign asset is shown in its own currency — the price, the average and
  // every movement below are dollars, and labelling them "R$" would be wrong.
  // The base-currency equivalent travels alongside as a hint.
  const native = { currency: data.currency };
  const inNative = (value: unknown) => money(value, native);
  const inBase = (value: unknown) => money(value, { currency: baseCurrency() });
  const showBase = Boolean(data.is_foreign);

  const upside =
    fund?.target_mean_price !== undefined && Number(data.current_price) > 0
      ? (fund.target_mean_price / Number(data.current_price) - 1) * 100
      : undefined;

  // Net of withholding, exactly like the Proventos page — the same month has to
  // read the same on both screens. A month can be entirely withholding refunds
  // and still pay, so this comes from the server rather than from the payments.
  const dividendsByMonth = (data.income_months ?? []).map((month) => ({
    period: month.period,
    total: Number(month.net),
  }));
  const incomeNet = Number(data.income) - Number(data.income_tax ?? 0);

  return (
    <div className="space-y-6">
      <div className="animate-fade-up">
        <Link to="/ativos" className="inline-flex items-center gap-1.5 text-sm text-ink-muted hover:text-ink">
          <ArrowLeft size={15} /> Ativos
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">{data.ticker}</h1>
          <KindTag kind={data.kind} />
          {!held ? <Badge tone="accent">não está na carteira</Badge> : null}
          {held && !data.is_open ? <Badge>posição encerrada</Badge> : null}
          {data.has_market_price ? (
            <Badge tone="neutral">cotação: {data.price_source}</Badge>
          ) : (
            <Badge tone="warning">sem cotação, avaliado pelo preço médio</Badge>
          )}
          {showBase && data.fx_rate ? (
            <Badge tone="accent">{fxRate(data.fx_rate, data.currency)}</Badge>
          ) : null}
          {!held ? (
            <span className="ml-auto">
              <WatchlistButton ticker={data.ticker} />
            </span>
          ) : null}
        </div>
        <p className="mt-1 text-sm text-ink-muted">{data.name}</p>
      </div>

      {data.warnings?.length || data.notes?.length ? (
        <Card className="space-y-1.5 border-warning/25 bg-warning/5 p-4 text-sm text-ink-secondary" hover={false}>
          {data.warnings?.map((message) => (
            <p key={message}>⚠ {message}</p>
          ))}
          {data.notes?.slice(0, 4).map((message) => (
            <p key={message} className="text-ink-muted">
              ℹ {message}
            </p>
          ))}
        </Card>
      ) : null}

      {held ? (
        <Tabs
          value={tab}
          onChange={setTab}
          options={[
            { value: "visao", label: "Visão geral", icon: LayoutGrid },
            { value: "posicao", label: "Minha posição", icon: Wallet },
            { value: "proventos", label: "Proventos", icon: Coins, count: data.dividends.length },
            {
              value: "movimentacoes",
              label: "Movimentações",
              icon: ArrowLeftRight,
              count: data.transactions.length,
            },
            { value: "ajustes", label: "Ajustes", icon: SlidersHorizontal },
          ]}
        />
      ) : null}

      {activeTab === "visao" ? (
        <>
        {/* Asset only — what this paper is and does on the market. Everything
            the wallet holds moved to "Minha posição": the split keeps each tab
            a clean context (and, later, a clean prompt for the AI panel). */}
        <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <StatTile
            label="Cotação atual"
            value={inNative(data.current_price)}
            tone="accent"
            delta={data.day_change_pct}
            hint={data.has_market_price ? `${inNative(data.day_change)} hoje` : "avaliado pelo preço médio"}
          />
          <StatTile
            label="Mínima / máxima 52 semanas"
            loading={fundamentals.isLoading}
            value={
              fund?.fifty_two_week_low !== undefined && fund?.fifty_two_week_high !== undefined
                ? `${inNative(fund.fifty_two_week_low)} – ${inNative(fund.fifty_two_week_high)}`
                : "-"
            }
            hint={
              fund?.fifty_two_week_high && data.current_price
                ? `atual a ${percent((data.current_price / fund.fifty_two_week_high) * 100, 0)} da máxima`
                : undefined
            }
          />
          <StatTile
            label="Valor de mercado"
            loading={fundamentals.isLoading}
            value={
              fund?.market_cap !== undefined
                ? money(fund.market_cap, { currency: fund.currency ?? data.currency, compact: true, decimals: 0 })
                : "-"
            }
            hint={[fund?.sector, fund?.industry].filter(Boolean).join(" · ") || undefined}
          />
          <StatTile
            label="Preço-alvo dos analistas"
            loading={fundamentals.isLoading}
            value={fund?.target_mean_price !== undefined ? inNative(fund.target_mean_price) : "-"}
            tone={upside !== undefined ? (upside >= 0 ? "positive" : "negative") : undefined}
            hint={
              upside !== undefined
                ? `${percent(upside, 1, true)} sobre a cotação${fund?.analyst_count ? ` · ${fund.analyst_count} analistas` : ""}`
                : undefined
            }
          />
        </section>

        <ChartFrame
          title="Histórico de preço"
          subtitle="Fechamentos diários armazenados"
          height={300}
          error={prices.isError ? prices.error : undefined}
          retry={() => prices.refetch()}
          action={
            <div className="flex flex-wrap items-center gap-2">
              {held ? (
                <button
                  type="button"
                  onClick={() => setShowAverage((current) => !current)}
                  aria-pressed={showAverage}
                  className={clsx(
                    "rounded-full border px-2.5 py-1 text-xs font-medium transition-all duration-200 ease-premium",
                    showAverage
                      ? "border-accent/40 bg-accent-soft text-ink"
                      : "border-line bg-surface-raised text-ink-muted hover:text-ink-secondary",
                  )}
                >
                  Preço médio
                </button>
              ) : null}
              <Segmented
                size="sm"
                value={priceRange}
                onChange={setPriceRange}
                options={[
                  { value: "1m", label: "1M" },
                  { value: "6m", label: "6M" },
                  { value: "1y", label: "1A" },
                  { value: "5y", label: "5A" },
                  { value: "max", label: "Máx" },
                ]}
              />
            </div>
          }
          footer={
            priceStats ? (
              <span>
                {shortDate(priceStats.first.date)} → {shortDate(priceStats.last.date)}: variação de{" "}
                <span className={clsx("tnum font-medium", priceStats.change >= 0 ? "text-positive" : "text-negative")}>
                  {inNative(priceStats.change)}
                  {priceStats.pct !== null ? ` (${percent(priceStats.pct, 1, true)})` : ""}
                </span>
                {showAverage && Number(data.average_price) ? " · linha tracejada: seu preço médio" : ""}
              </span>
            ) : undefined
          }
          table={
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-surface text-left text-xs uppercase tracking-wide text-ink-muted">
                <tr>
                  <th className="px-2 py-2">Data</th>
                  <th className="px-2 py-2 text-right">Fechamento</th>
                  <th className="px-2 py-2 text-right">Variação</th>
                </tr>
              </thead>
              <tbody>
                {visiblePrices
                  .map((point, index) => ({
                    ...point,
                    previous: index > 0 ? visiblePrices[index - 1].close : null,
                  }))
                  .reverse()
                  .map((point) => (
                    <tr key={point.date} className="table-row">
                      <td className="px-2 py-1.5">{shortDate(point.date)}</td>
                      <td className="tnum px-2 py-1.5 text-right">{inNative(point.close)}</td>
                      <td
                        className={clsx(
                          "tnum px-2 py-1.5 text-right",
                          point.previous !== null && point.close > point.previous && "text-positive",
                          point.previous !== null && point.close < point.previous && "text-negative",
                        )}
                      >
                        {point.previous ? percent(((point.close - point.previous) / point.previous) * 100, 2, true) : "-"}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          }
        >
          <PriceLine
            data={visiblePrices}
            height={300}
            averagePrice={showAverage ? Number(data.average_price) || undefined : undefined}
            currency={data.currency}
          />
        </ChartFrame>

        {fund?.yearly_financials?.length || dividendYears.length ? (
          <div className="grid gap-4 xl:grid-cols-2">
            {fund?.yearly_financials?.length ? (
              <ChartFrame
                title="Receita e lucro por ano"
                subtitle="Resultados anuais reportados pela empresa"
                height={260}
                className={dividendYears.length ? undefined : "xl:col-span-2"}
              >
                <YearlyFinancialsBars
                  data={fund.yearly_financials}
                  currency={fund.currency ?? data.currency}
                  height={260}
                />
              </ChartFrame>
            ) : null}
            {dividendYears.length ? (
              <ChartFrame
                title="Proventos por cota e yield por ano"
                subtitle="Distribuições declaradas na B3 · % sobre o fechamento de cada ano"
                height={260}
                className={fund?.yearly_financials?.length ? undefined : "xl:col-span-2"}
              >
                <DividendPerShareBars data={dividendYears} currency={data.currency} height={260} />
              </ChartFrame>
            ) : null}
          </div>
        ) : null}

        <FundamentalsCard ticker={data.ticker} currency={data.currency} />
        </>
      ) : null}

      {activeTab === "posicao" ? (
        <>
        <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <StatTile label="Quantidade" value={quantity(data.quantity)} hint={`${data.transactions_count} movimentos`} />
          <StatTile label="Preço médio" value={inNative(data.average_price)} hint={`atual ${inNative(data.current_price)}`} />
          <StatTile
            label="Valor investido"
            value={inNative(data.cost_basis)}
            hint={
              showBase
                ? `${inBase(data.cost_basis_base)} · valor atual ${inNative(data.market_value)}`
                : `valor atual ${inNative(data.market_value)}`
            }
          />
          <StatTile
            label="Não realizado"
            value={inNative(data.unrealized_pnl)}
            tone={data.unrealized_pnl >= 0 ? "positive" : "negative"}
            delta={data.unrealized_pct}
            hint={showBase ? inBase(data.unrealized_pnl_base) : undefined}
          />
        </section>

        <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <StatTile
            label="Lucro realizado"
            value={inNative(data.realized_pnl)}
            tone={data.realized_pnl >= 0 ? "positive" : "negative"}
            hint={showBase ? inBase(data.realized_pnl_base) : undefined}
          />
          <StatTile
            label="Proventos"
            value={inNative(incomeNet)}
            tone="positive"
            hint={
              Number(data.income_tax ?? 0)
                ? `líquido · ${inNative(data.income)} bruto − ${inNative(data.income_tax)} de imposto`
                : `${data.dividends.length} pagamentos`
            }
          />
          <StatTile
            label="Resultado total"
            value={inNative(data.total_return)}
            tone={data.total_return >= 0 ? "positive" : "negative"}
            delta={data.total_return_pct}
          />
          <StatTile label="Alocação" value={percent(data.allocation_pct, 2)} hint="da carteira" />
        </section>

        <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <StatTile
            label="Yield on cost"
            value={Number(data.cost_basis) > 0 ? percent((incomeNet / Number(data.cost_basis)) * 100, 2) : "-"}
            tone="positive"
            hint="proventos líquidos sobre o valor investido"
          />
          <StatTile
            label="Variação do dia"
            value={inNative(data.day_change)}
            tone={data.day_change >= 0 ? "positive" : "negative"}
            delta={data.day_change_pct}
            hint={showBase ? inBase(data.day_change_base) : undefined}
          />
          <StatTile
            label="Capital devolvido"
            value={inNative(data.returned_capital)}
            hint="amortizações e devoluções de capital"
          />
          <StatTile
            label="Valor de mercado da posição"
            value={inNative(data.market_value)}
            hint={showBase ? inBase(data.market_value_base) : undefined}
          />
        </section>
        </>
      ) : null}

      {activeTab === "proventos" ? (
        <>
          <ChartFrame
            title="Proventos por mês"
            subtitle={showBase ? "Líquidos de imposto retido, em dólar" : "Líquidos de impostos e taxas"}
            height={280}
          >
            <IncomeBars data={dividendsByMonth} height={280} colorIndex={2} currency={data.currency} />
          </ChartFrame>
          <DividendsCard data={data} />
        </>
      ) : null}

      {activeTab === "movimentacoes" ? (
        <MovementsCard transactions={data.transactions} currency={data.currency} />
      ) : null}

      {activeTab === "ajustes" ? (
        <Card className="p-5">
          <SectionTitle title="Ajustes do ativo" subtitle="Anotações e preço manual (para ativos sem cotação pública)" />
          <div className="grid gap-4 lg:grid-cols-[1fr_260px]">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-ink-muted" htmlFor="asset-notes">
                Anotações
              </label>
              <textarea
                id="asset-notes"
                value={notes ?? data.user_notes ?? ""}
                onChange={(event) => setNotes(event.target.value)}
                rows={4}
                className="input resize-y"
                placeholder="Tese de investimento, lembretes, eventos…"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-medium text-ink-muted" htmlFor="manual-price">
                Preço manual
              </label>
              <input
                id="manual-price"
                value={manualPrice}
                onChange={(event) => setManualPrice(event.target.value)}
                inputMode="decimal"
                placeholder="ex.: 10.50"
                className="input"
              />
              <p className="mt-1.5 text-xs text-ink-muted">
                Preenchido, substitui a cotação automática, útil para CDB, Tesouro e fundos fechados.
              </p>
              <button
                type="button"
                className="btn-primary mt-3 w-full"
                disabled={save.isPending}
                onClick={() =>
                  save.mutate({
                    notes: notes ?? "",
                    // An emptied field clears the override and returns the
                    // asset to automatic pricing.
                    ...(manualPrice.trim()
                      ? { manual_price: Number(manualPrice.replace(",", ".")), price_manual: true }
                      : { manual_price: null, price_manual: false }),
                  })
                }
              >
                <Save size={15} /> {save.isPending ? "Salvando…" : "Salvar"}
              </button>
            </div>
            {/* Some of a position cannot be derived from any export: juros que
                capitalizam dentro de um produto de staking entram no saldo sem
                virar movimento. Em vez de deixar a posição errada, o usuário
                informa o saldo real e a diferença vira mais um movimento — a
                posição continua sendo o resultado do replay. */}
            <div>
              <label className="mb-1.5 block text-xs font-medium text-ink-muted" htmlFor="real-balance">
                Saldo informado pela corretora
              </label>
              <input
                id="real-balance"
                value={realBalance}
                onChange={(event) => setRealBalance(event.target.value)}
                inputMode="decimal"
                placeholder={quantity(data.quantity)}
                className="input"
              />
              <p className="mt-1.5 text-xs text-ink-muted">
                Posição calculada: <span className="tnum">{quantity(data.quantity)}</span>. Informe o saldo que
                a corretora mostra e a diferença é registrada como um ajuste, sem apagar nada do histórico.
              </p>
              <button
                type="button"
                className="btn-ghost mt-3 w-full"
                disabled={reconcile.isPending || !realBalance.trim()}
                onClick={() => {
                  const entered = Number(realBalance.replace(",", "."));
                  if (!Number.isFinite(entered) || entered < 0) {
                    toast.error("Saldo inválido: informe um número, ex.: 1.573,25.");
                    return;
                  }
                  setReconcilePreview(entered);
                }}
              >
                <Scale size={15} /> {reconcile.isPending ? "Ajustando…" : "Ajustar posição"}
              </button>
            </div>
          </div>

          <Modal
            open={reconcilePreview !== null}
            title="Confirmar ajuste de posição?"
            subtitle={data.ticker}
            onClose={() => setReconcilePreview(null)}
          >
            {reconcilePreview !== null ? (
              <div className="space-y-3 text-sm text-ink-secondary">
                <div className="grid grid-cols-2 gap-3">
                  <div className="rounded-xl border border-line bg-surface-raised/50 p-3">
                    <p className="text-xs text-ink-muted">Posição calculada</p>
                    <p className="tnum mt-0.5 font-medium text-ink">{quantity(data.quantity)}</p>
                  </div>
                  <div className="rounded-xl border border-line bg-surface-raised/50 p-3">
                    <p className="text-xs text-ink-muted">Saldo informado</p>
                    <p className="tnum mt-0.5 font-medium text-ink">{quantity(reconcilePreview)}</p>
                  </div>
                </div>
                <p>
                  {Math.abs(reconcilePreview - Number(data.quantity)) < 1e-9 ? (
                    "Os dois valores já são iguais, nada será registrado."
                  ) : (
                    <>
                      Um movimento de ajuste de{" "}
                      <span className={clsx("tnum font-medium", reconcilePreview >= Number(data.quantity) ? "text-positive" : "text-negative")}>
                        {reconcilePreview >= Number(data.quantity) ? "+" : "−"}
                        {quantity(Math.abs(reconcilePreview - Number(data.quantity)))}
                      </span>{" "}
                      será acrescentado ao histórico. Nada é apagado; o ajuste fica visível nas
                      movimentações e pode ser conferido depois.
                    </>
                  )}
                </p>
              </div>
            ) : null}
            <div className="mt-4 flex flex-wrap justify-end gap-2">
              <button type="button" className="btn-ghost" onClick={() => setReconcilePreview(null)}>
                Cancelar
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={reconcile.isPending}
                onClick={() => {
                  if (reconcilePreview !== null) reconcile.mutate(reconcilePreview);
                  setReconcilePreview(null);
                }}
              >
                Registrar ajuste
              </button>
            </div>
          </Modal>
        </Card>
      ) : null}

      <AssetChat ticker={data.ticker} />
    </div>
  );
}
