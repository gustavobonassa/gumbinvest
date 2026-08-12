/** Searchable, sortable, paginated transaction ledger with CSV export. */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { ArrowDown, ArrowUp, Download, Plus, Search, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { useToast } from "@/components/Toast";
import {
  Badge,
  Card,
  Combobox,
  DateField,
  EmptyState,
  ErrorState,
  Modal,
  MultiSelect,
  Pager,
  Select,
  Skeleton,
} from "@/components/ui";
import { api } from "@/lib/api";
import { baseCurrency, kindLabel, money, opLabel, quantity, shortDate } from "@/lib/format";

/** Today as `yyyy-mm-dd` in the local calendar, which is what DateField speaks. */
function isoToday(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

/**
 * Read an amount the way a Brazilian types it — "3.000,00" — while still
 * accepting the "3000.50" a numeric keyboard produces. A comma always marks the
 * decimals; without one, a trailing dot with one or two digits is a decimal
 * point and anything else is a thousands separator.
 */
function parseAmount(text: string): number {
  const raw = text.trim();
  if (!raw) return 0;
  const normalised = raw.includes(",")
    ? raw.replace(/\./g, "").replace(",", ".")
    : /\.\d{1,2}$/.test(raw)
      ? raw
      : raw.replace(/\./g, "");
  const value = Number(normalised);
  return Number.isFinite(value) ? value : 0;
}

/** `hide` collapses secondary columns on phones: date, asset, operation and
    total answer "what happened"; quantity, unit price and broker come back
    with screen width instead of forcing a sideways scroll. */
const SORTABLE = [
  { key: "date", label: "Data" },
  { key: "ticker", label: "Ativo" },
  { key: "op_type", label: "Operação" },
  { key: "quantity", label: "Qtd.", hide: "hidden lg:table-cell" },
  { key: "unit_price", label: "Preço", hide: "hidden lg:table-cell" },
  { key: "gross_amount", label: "Total" },
] as const;

type SortKey = (typeof SORTABLE)[number]["key"];

/**
 * Everything no export reaches, typed in by hand.
 *
 * The form asks for exactly what the chosen operation needs — a purchase wants
 * quantity and price, a dividend only an amount — because a form that asks for
 * a "unit price" on a dividend teaches people to put zeros in fields that then
 * reach the calculation engine. The catalogue comes from the server, so the
 * fields and the accounting always agree.
 */
/** Write a number back into a text field the way the user would type it. */
function formatAmount(value: number, decimals = 2): string {
  if (!Number.isFinite(value) || value === 0) return "";
  return new Intl.NumberFormat("pt-BR", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

/** The close on or before `day`; the series is ascending, so scan backwards. */
function closeOn(history: { date: string; close: number }[] | undefined, day: string): number | null {
  if (!history?.length || !day) return null;
  for (let index = history.length - 1; index >= 0; index -= 1) {
    if (history[index].date <= day) return Number(history[index].close);
  }
  return null;
}

/**
 * Everything no export reaches, typed in by hand.
 *
 * The form asks for exactly what the chosen operation needs — a purchase wants
 * quantity and price, a dividend only an amount — because a form that asks for
 * a "unit price" on a dividend teaches people to put zeros in fields that then
 * reach the calculation engine. The catalogue comes from the server, so the
 * fields and the accounting always agree.
 *
 * What it can fill in, it fills in: the ticker suggests from the portfolio and
 * then from the market, picking one pulls the name and the closing price *on
 * the date being entered*, and the total follows quantity × price until the
 * moment the user types a total of their own. Every suggestion is a starting
 * value, never a lock.
 */
function ManualEntryModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const operations = useQuery({
    queryKey: ["transaction-operations"],
    queryFn: api.transactionOperations,
    enabled: open,
  });
  // Same key the Ativos page uses, so this is usually already in cache.
  const held = useQuery({ queryKey: ["assets"], queryFn: () => api.assets(true), enabled: open });
  const filters = useQuery({ queryKey: ["transaction-filters"], queryFn: api.transactionFilters });

  const [operation, setOperation] = useState("BUY");
  const [ticker, setTicker] = useState("");
  const [name, setName] = useState("");
  const [when, setWhen] = useState(isoToday);
  const [qty, setQty] = useState("");
  const [price, setPrice] = useState("");
  const [amount, setAmount] = useState("");
  const [fees, setFees] = useState("");
  const [broker, setBroker] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  // Once either figure is typed by hand it stops following the others: the
  // form may guess, but it may never overwrite.
  const [priceTouched, setPriceTouched] = useState(false);
  const [totalTouched, setTotalTouched] = useState(false);
  // What the market said about a ticker the portfolio has never held. Without
  // it a new NVDA would be created in reais and then classified by the B3
  // rules — landing on "Outros" and quoted as NVDA.SA.
  const [picked, setPicked] = useState<{ kind: string; currency: string } | null>(null);

  const chosen = (operations.data ?? []).find((item) => item.code === operation);
  const needs = chosen?.needs ?? "trade";
  // Shown next to the money fields when the asset does not trade in the
  // portfolio's currency, so a US purchase is not typed in as if it were reais.
  const foreignCurrency =
    picked?.currency && picked.currency.toUpperCase() !== baseCurrency() ? picked.currency : null;
  const cleanTicker = ticker.trim().toUpperCase();
  const known = (held.data ?? []).some((asset) => asset.ticker === cleanTicker);

  // Tickers the market knows but this portfolio never traded. Debounced and
  // only asked for once the local list has nothing — it leaves the machine.
  const [marketQuery, setMarketQuery] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setMarketQuery(cleanTicker.length >= 2 && !known ? cleanTicker : ""), 350);
    return () => clearTimeout(timer);
  }, [cleanTicker, known]);
  const market = useQuery({
    queryKey: ["search-market", marketQuery],
    queryFn: () => api.searchMarket(marketQuery),
    enabled: open && marketQuery.length >= 2,
  });

  const suggestions = useMemo(() => {
    const local = (held.data ?? []).map((asset) => ({
      value: asset.ticker,
      label: asset.name,
      hint: kindLabel(asset.kind),
    }));
    // The class, not the exchange. "São Paulo" says where a ticker trades;
    // "Ação" says what it is — which is what decides the tab it lands in, the
    // colour it gets and the bucket it is counted in. The exchange only shows
    // when the class could not be worked out.
    const remote = (market.data?.items ?? []).map((item) => ({
      value: item.ticker,
      label: item.name,
      hint: item.kind && item.kind !== "OTHER" ? kindLabel(item.kind) : item.exchange,
    }));
    const seen = new Set(local.map((item) => item.value));
    return [...local, ...remote.filter((item) => !seen.has(item.value))];
  }, [held.data, market.data]);

  // The picked asset's own history, for the price on the date being entered.
  const detail = useQuery({
    queryKey: ["asset", cleanTicker],
    queryFn: () => api.asset(cleanTicker),
    enabled: open && known && cleanTicker.length > 0,
    retry: false,
  });
  const history = useQuery({
    queryKey: ["asset-price-history", cleanTicker],
    queryFn: () => api.assetPriceHistory(cleanTicker),
    enabled: open && known && cleanTicker.length > 0,
    retry: false,
  });

  const suggestedPrice = closeOn(history.data, when) ?? Number(detail.data?.current_price) ?? null;

  useEffect(() => {
    if (priceTouched || needs !== "trade" || !suggestedPrice) return;
    setPrice(formatAmount(suggestedPrice, suggestedPrice < 1 ? 6 : 2));
  }, [suggestedPrice, priceTouched, needs]);

  useEffect(() => {
    if (totalTouched || needs === "amount") return;
    const total = parseAmount(qty) * parseAmount(price);
    setAmount(total > 0 ? formatAmount(total) : "");
  }, [qty, price, totalTouched, needs]);

  const reset = () => {
    setTicker("");
    setName("");
    setQty("");
    setPrice("");
    setAmount("");
    setFees("");
    setNotes("");
    setError(null);
    setPriceTouched(false);
    setTotalTouched(false);
  };

  const save = useMutation({
    mutationFn: () =>
      api.createTransaction({
        operation,
        ticker: cleanTicker,
        date: when,
        name: name.trim() || undefined,
        kind: picked?.kind,
        currency: picked?.currency,
        quantity: needs === "amount" ? 0 : parseAmount(qty),
        unit_price: needs === "trade" ? parseAmount(price) : 0,
        amount: parseAmount(amount) || null,
        fees: parseAmount(fees),
        broker: broker.trim() || null,
        notes: notes.trim() || null,
      }),
    onSuccess: (created) => {
      toast.success(`${opLabel(created.op_type)} de ${created.ticker} lançada`);
      reset();
      queryClient.invalidateQueries();
      onClose();
    },
    onError: (err) => setError(err instanceof Error ? err.message : "não foi possível lançar"),
  });

  const filled =
    cleanTicker &&
    when &&
    (needs === "amount"
      ? parseAmount(amount) > 0
      : parseAmount(qty) > 0 && (needs !== "trade" || parseAmount(price) > 0 || parseAmount(amount) > 0));

  return (
    <Modal
      open={open}
      title="Novo lançamento"
      subtitle="Entra no histórico como qualquer movimento importado e recalcula a carteira"
      onClose={onClose}
    >
      <div className="space-y-3">
        <div>
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">O que aconteceu</span>
          <Select
            ariaLabel="Operação"
            value={operation}
            onChange={setOperation}
            options={(operations.data ?? []).map((item) => ({
              value: item.code,
              label: item.label,
              hint: item.hint ?? undefined,
            }))}
          />
          {chosen?.hint ? <p className="mt-1.5 text-xs text-ink-muted">{chosen.hint}</p> : null}
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <span className="mb-1.5 block text-xs font-medium text-ink-muted">Ativo</span>
            <Combobox
              ariaLabel="Ativo"
              placeholder="PETR4"
              inputClassName="uppercase"
              value={ticker}
              onChange={(next) => {
                setTicker(next);
                setPriceTouched(false);
                setPicked(null);
              }}
              onPick={(code) => {
                const local = (held.data ?? []).find((asset) => asset.ticker === code);
                const remote = (market.data?.items ?? []).find((item) => item.ticker === code);
                const match = local ?? remote;
                if (match && !name.trim()) setName(match.name);
                // Held assets count too: the form needs the currency to label
                // its amount fields, and for one already on file the server
                // ignores what is sent and keeps the asset's own.
                setPicked(match ? { kind: match.kind, currency: match.currency } : null);
              }}
              options={suggestions}
              emptyHint="Nenhum ativo encontrado. Continue digitando para criar um novo."
            />
          </div>
          <div>
            <span className="mb-1.5 block text-xs font-medium text-ink-muted">Data</span>
            <DateField ariaLabel="Data do lançamento" className="w-full" value={when} onChange={setWhen} />
          </div>
        </div>

        {/* Only worth asking for a ticker nobody has traded: an existing asset
            already has a name, and a second one would be a second truth. */}
        {!known ? (
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-ink-muted">
              Nome <span className="font-normal">(opcional, para um ativo novo)</span>
            </span>
            <input
              className="input"
              placeholder="PETROLEO BRASILEIRO S.A."
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-3">
          {needs !== "amount" ? (
            <label className="block">
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">Quantidade</span>
              <input
                className="input tnum"
                inputMode="decimal"
                placeholder="100"
                value={qty}
                onChange={(event) => setQty(event.target.value)}
              />
            </label>
          ) : null}
          {needs === "trade" ? (
            <label className="block">
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">
                Preço unitário
                {foreignCurrency ? <span className="ml-1 text-warning">em {foreignCurrency}</span> : null}
              </span>
              <input
                className="input tnum"
                inputMode="decimal"
                placeholder="30,00"
                value={price}
                onChange={(event) => {
                  setPrice(event.target.value);
                  setPriceTouched(true);
                }}
              />
              {suggestedPrice && !priceTouched ? (
                <span className="mt-1 block text-[11px] text-ink-muted">
                  fechamento em {shortDate(when)}
                </span>
              ) : null}
            </label>
          ) : null}
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-ink-muted">
              {needs === "amount" ? "Valor" : "Total"}
              {foreignCurrency ? <span className="ml-1 text-warning">em {foreignCurrency}</span> : null}
            </span>
            <input
              className="input tnum"
              inputMode="decimal"
              placeholder="3.000,00"
              value={amount}
              onChange={(event) => {
                setAmount(event.target.value);
                setTotalTouched(true);
              }}
            />
            {needs === "trade" && totalTouched ? (
              <button
                type="button"
                onClick={() => setTotalTouched(false)}
                className="mt-1 block text-[11px] text-ink-muted hover:text-ink-secondary"
              >
                voltar a calcular
              </button>
            ) : null}
          </label>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-ink-muted">
              Taxas <span className="font-normal">(opcional)</span>
            </span>
            <input
              className="input tnum"
              inputMode="decimal"
              placeholder="0,00"
              value={fees}
              onChange={(event) => setFees(event.target.value)}
            />
          </label>
          <div>
            <span className="mb-1.5 block text-xs font-medium text-ink-muted">
              Corretora <span className="font-normal">(opcional)</span>
            </span>
            <Combobox
              ariaLabel="Corretora"
              placeholder="XP Investimentos"
              value={broker}
              onChange={setBroker}
              options={(filters.data?.brokers ?? []).map((item) => ({ value: item, label: "" }))}
              emptyHint="Nenhuma corretora com esse nome. Continue digitando para cadastrar."
            />
          </div>
        </div>

        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">
            Observação <span className="font-normal">(opcional)</span>
          </span>
          <input className="input" value={notes} onChange={(event) => setNotes(event.target.value)} />
        </label>

        {error ? <p className="text-xs text-negative">{error}</p> : null}

        <div className="flex items-center justify-end gap-2 border-t border-line pt-3">
          <button type="button" className="btn-ghost" onClick={onClose}>
            Cancelar
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={save.isPending || !filled}
            onClick={() => save.mutate()}
          >
            <Plus size={15} /> Lançar
          </button>
        </div>
      </div>
    </Modal>
  );
}


export default function Transactions() {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [opTypes, setOpTypes] = useState<string[]>([]);
  const [broker, setBroker] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [sort, setSort] = useState<SortKey>("date");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [adding, setAdding] = useState(false);

  const queryClient = useQueryClient();
  const toast = useToast();
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteTransaction(id),
    onSuccess: () => {
      toast.success("Lançamento excluído");
      queryClient.invalidateQueries();
    },
    onError: (err) => toast.error("Não foi possível excluir o lançamento", err),
  });

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebounced(search);
      setPage(1);
    }, 300);
    return () => clearTimeout(timer);
  }, [search]);

  const filters = useQuery({ queryKey: ["transaction-filters"], queryFn: api.transactionFilters });
  const params = {
    search: debounced || undefined,
    op_type: opTypes.length ? opTypes : undefined,
    broker: broker || undefined,
    start: start || undefined,
    end: end || undefined,
    sort,
    order,
    page,
    page_size: pageSize,
  };
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["transactions", params],
    queryFn: () => api.transactions(params),
    placeholderData: keepPreviousData,
  });

  const toggleSort = (key: SortKey) => {
    if (key === sort) setOrder((current) => (current === "asc" ? "desc" : "asc"));
    else {
      setSort(key);
      setOrder("desc");
    }
    setPage(1);
  };

  const clearFilters = () => {
    setSearch("");
    setOpTypes([]);
    setBroker("");
    setStart("");
    setEnd("");
    setPage(1);
  };

  const hasFilters = Boolean(debounced || opTypes.length || broker || start || end);

  if (isError) return <ErrorState error={error} retry={() => refetch()} />;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3 animate-fade-up">
        <div>
          <p className="text-sm text-ink-muted">Histórico</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">Transações</h1>
        </div>
        <div className="flex items-center gap-2">
          <a href={api.exportUrl({ ...params, page: undefined, page_size: undefined })} className="btn-ghost">
            <Download size={15} /> Exportar CSV
          </a>
          <button type="button" className="btn-primary" onClick={() => setAdding(true)}>
            <Plus size={15} /> Novo lançamento
          </button>
        </div>
      </header>

      <ManualEntryModal open={adding} onClose={() => setAdding(false)} />

      <Card className="space-y-3 p-4" hover={false}>
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative min-w-[240px] flex-1">
            <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted" />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Buscar ticker, empresa, movimento ou data"
              className="input pl-9"
            />
          </div>
          <MultiSelect
            ariaLabel="Tipos de operação"
            className="w-auto min-w-[190px]"
            placeholder="Todas as operações"
            values={opTypes}
            onChange={(next) => {
              setOpTypes(next);
              setPage(1);
            }}
            options={(filters.data?.op_types ?? []).map((type) => ({ value: type, label: opLabel(type) }))}
          />
          <Select
            ariaLabel="Corretora"
            className="w-auto min-w-[170px]"
            value={broker}
            onChange={(next) => {
              setBroker(next);
              setPage(1);
            }}
            options={[
              { value: "", label: "Todas as corretoras" },
              ...(filters.data?.brokers ?? []).map((item) => ({ value: item, label: item })),
            ]}
          />
          <DateField
            ariaLabel="Data inicial"
            placeholder="Data inicial"
            value={start}
            onChange={(next) => {
              setStart(next);
              setPage(1);
            }}
          />
          <DateField
            ariaLabel="Data final"
            placeholder="Data final"
            value={end}
            onChange={(next) => {
              setEnd(next);
              setPage(1);
            }}
          />
          {hasFilters ? (
            <button type="button" onClick={clearFilters} className="btn-ghost">
              <X size={14} /> Limpar
            </button>
          ) : null}
        </div>
      </Card>

      <Card className="overflow-hidden p-0">
        {isLoading ? (
          <div className="space-y-2 p-5">
            {Array.from({ length: 10 }).map((_, index) => (
              <Skeleton key={index} className="h-10 w-full" />
            ))}
          </div>
        ) : !data?.items.length ? (
          <EmptyState title="Nenhuma transação encontrada" description="Ajuste os filtros ou importe um novo arquivo." />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className={clsx("w-full text-sm transition-opacity", isFetching && "opacity-60")}>
                <thead className="border-b border-line bg-surface-raised/60 text-xs uppercase tracking-wide text-ink-muted">
                  <tr>
                    {SORTABLE.map((column) => (
                      <th
                        key={column.key}
                        aria-sort={sort === column.key ? (order === "asc" ? "ascending" : "descending") : undefined}
                        className={clsx(
                          "px-4 py-3 font-medium",
                          ["quantity", "unit_price", "gross_amount"].includes(column.key) ? "text-right" : "text-left",
                          "hide" in column ? column.hide : undefined,
                        )}
                      >
                        <button
                          type="button"
                          onClick={() => toggleSort(column.key)}
                          className={clsx("inline-flex items-center gap-1 hover:text-ink", sort === column.key && "text-ink")}
                        >
                          {column.label}
                          {sort === column.key ? order === "asc" ? <ArrowUp size={12} /> : <ArrowDown size={12} /> : null}
                        </button>
                      </th>
                    ))}
                    <th className="hidden px-4 py-3 text-left font-medium xl:table-cell">Corretora</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((transaction) => (
                    <tr key={transaction.id} className="table-row">
                      <td className="whitespace-nowrap px-4 py-2.5 text-ink-secondary">{shortDate(transaction.date)}</td>
                      <td className="px-4 py-2.5">
                        <Link to={`/ativos/${transaction.ticker}`} className="font-medium hover:text-accent">
                          {transaction.ticker}
                        </Link>
                        <span className="block max-w-[7.5rem] truncate text-xs text-ink-muted sm:max-w-[220px]">
                          {transaction.name}
                        </span>
                      </td>
                      <td className="px-4 py-2.5">
                        <Badge
                          tone={
                            transaction.op_type === "BUY"
                              ? "accent"
                              : transaction.op_type === "SELL"
                                ? "negative"
                                : ["DIVIDEND", "JCP", "YIELD", "INTEREST"].includes(transaction.op_type)
                                  ? "positive"
                                  : "neutral"
                          }
                        >
                          {opLabel(transaction.op_type)}
                        </Badge>
                        <span className="mt-0.5 block max-w-[6.5rem] truncate text-[11px] text-ink-muted sm:max-w-[200px]">
                          {transaction.movement}
                        </span>
                      </td>
                      <td className="tnum hidden px-4 py-2.5 text-right lg:table-cell">{quantity(transaction.quantity)}</td>
                      <td className="tnum hidden px-4 py-2.5 text-right lg:table-cell">
                        {money(transaction.unit_price, { currency: transaction.currency })}
                      </td>
                      {/* Shows the gross magnitude; the colour carries the
                          direction of the net cash flow. The tooltip makes
                          the pairing explicit instead of looking like a bug. */}
                      <td
                        className={clsx(
                          "tnum px-4 py-2.5 text-right font-medium",
                          Number(transaction.net_amount) > 0 && "text-positive",
                          Number(transaction.net_amount) < 0 && "text-negative",
                        )}
                        title={`Líquido: ${money(transaction.net_amount, { currency: transaction.currency })}`}
                      >
                        {money(transaction.gross_amount, { currency: transaction.currency })}
                      </td>
                      <td className="hidden px-4 py-2.5 text-xs text-ink-muted xl:table-cell">{transaction.broker ?? "-"}</td>
                      {/* Only hand-entered rows: an imported one is reproducible
                          from its file, and deleting it would either come back
                          on the next upload or quietly rewrite history. */}
                      <td className="px-4 py-2.5 text-right">
                        {transaction.is_manual ? (
                          <button
                            type="button"
                            className="btn-ghost px-1.5 py-1 text-negative"
                            aria-label={`Excluir lançamento de ${transaction.ticker}`}
                            title="Excluir lançamento manual"
                            disabled={remove.isPending}
                            onClick={() => remove.mutate(transaction.id)}
                          >
                            <Trash2 size={14} />
                          </button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="px-4 pb-3">
              <Pager
                page={data.page}
                pages={data.pages}
                total={data.total}
                pageSize={data.page_size}
                onChange={setPage}
                noun="movimentações"
                pageSizeOptions={[25, 50, 100, 200]}
                onPageSizeChange={(next) => {
                  setPageSize(next);
                  setPage(1);
                }}
              />
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
