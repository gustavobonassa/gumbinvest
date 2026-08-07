/** Fixed income: yield terms per paper and the value accrued from the index. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  Building2,
  ChevronDown,
  ChevronUp,
  Info,
  Landmark,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  TrendingUp,
  Wallet,
} from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { useToast } from "@/components/Toast";
import {
  Badge,
  Card,
  DateField,
  EmptyState,
  ErrorState,
  Modal,
  SectionTitle,
  Segmented,
  Select,
  Skeleton,
  StatTile,
  Tabs,
} from "@/components/ui";
import {
  api,
  type CashAccount,
  type FixedIncomeItem,
  type FixedIncomeTerms,
  type TreasuryItem,
} from "@/lib/api";
import { dateTime, decimal, money, percent, quantity, shortDate } from "@/lib/format";

/** Today as `yyyy-mm-dd` in the local calendar, which is what DateField speaks. */
function isoToday(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

/**
 * Read an amount the way a Brazilian types it — "100.000,00" — while still
 * accepting the "1000.50" a numeric keyboard produces. A comma always marks the
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

const INDEX_LABELS: Record<string, string> = {
  CDI: "% do CDI",
  SELIC: "% da Selic",
  IPCA: "IPCA + spread",
  PRE: "Prefixado",
};

function ImpliedHint({ item, onApply }: { item: FixedIncomeItem; onApply: (percent: number) => void }) {
  if (!item.implied) return null;
  return (
    <div className="mb-4 flex flex-wrap items-center gap-2 rounded-xl border border-accent/25 bg-accent-soft px-3 py-2 text-sm">
      <Info size={15} className="text-accent" aria-hidden />
      <span className="text-ink-secondary">
        Este papel pagou {money(item.implied.proceeds)} sobre {money(item.implied.invested)} entre{" "}
        {shortDate(item.implied.start)} e {shortDate(item.implied.end)}: equivale a{" "}
        <strong className="text-ink">
          {Number(item.implied.percent_of_index).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}% do{" "}
          {item.implied.index_code}
        </strong>
        .
      </span>
      <button
        type="button"
        className="btn-ghost px-2 py-1 text-xs"
        onClick={() => onApply(Number(item.implied!.percent_of_index))}
      >
        Usar esta taxa
      </button>
    </div>
  );
}

function TermsEditor({ item, onSaved }: { item: FixedIncomeItem; onSaved: () => void }) {
  const toast = useToast();
  const [terms, setTerms] = useState<FixedIncomeTerms>(item.terms);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    setTerms(item.terms);
    setDirty(false);
  }, [item.terms]);

  const save = useMutation({
    mutationFn: () => api.updateFixedIncome(item.ticker, terms),
    onSuccess: () => {
      setDirty(false);
      onSaved();
      toast.success(`Condições de ${item.ticker} salvas.`);
    },
    onError: (error) => toast.error(`Não foi possível salvar ${item.ticker}.`, error),
  });

  const update = <K extends keyof FixedIncomeTerms>(key: K, value: FixedIncomeTerms[K]) => {
    setTerms((current) => ({ ...current, [key]: value }));
    setDirty(true);
  };

  const isIndexed = terms.index_code === "CDI" || terms.index_code === "SELIC";

  return (
    <>
    <ImpliedHint
      item={item}
      onApply={(value) => {
        update("percent_of_index", value as FixedIncomeTerms["percent_of_index"]);
      }}
    />
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5 lg:items-end">
      <div>
        <span className="mb-1.5 block text-xs font-medium text-ink-muted">Indexador</span>
        <Select
          ariaLabel="Indexador"
          value={terms.index_code}
          onChange={(next) => update("index_code", next as FixedIncomeTerms["index_code"])}
          options={Object.entries(INDEX_LABELS).map(([value, label]) => ({ value, label }))}
        />
      </div>

      {isIndexed ? (
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">% do indexador</span>
          <input
            className="input tnum"
            inputMode="decimal"
            value={String(terms.percent_of_index ?? "")}
            onChange={(event) => update("percent_of_index", event.target.value as unknown as number)}
          />
        </label>
      ) : null}

      {terms.index_code === "PRE" ? (
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Taxa a.a. (%)</span>
          <input
            className="input tnum"
            inputMode="decimal"
            value={String(terms.fixed_rate_annual ?? "")}
            onChange={(event) => update("fixed_rate_annual", event.target.value as unknown as number)}
          />
        </label>
      ) : (
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Spread a.a. (%)</span>
          <input
            className="input tnum"
            inputMode="decimal"
            value={String(terms.spread_annual ?? "")}
            onChange={(event) => update("spread_annual", event.target.value as unknown as number)}
          />
        </label>
      )}

      <div>
        <span className="mb-1.5 block text-xs font-medium text-ink-muted">Vencimento</span>
        <DateField
          ariaLabel="Vencimento"
          className="w-full"
          value={terms.maturity_date ?? ""}
          onChange={(next) => update("maturity_date", next || null)}
        />
      </div>

      <button
        type="button"
        className={clsx(dirty ? "btn-primary" : "btn-ghost")}
        disabled={save.isPending}
        onClick={() => save.mutate()}
      >
        <Save size={15} /> {save.isPending ? "Salvando…" : dirty ? "Salvar" : "Salvo"}
      </button>
    </div>
    </>
  );
}

/** What a Tesouro yield is quoted *over*, so the number is not read bare. */
function rateBasis(name: string): string {
  const lower = name.toLowerCase();
  if (lower.includes("selic")) return "Selic +";
  if (lower.includes("prefixado")) return "";
  if (lower.includes("igpm") || lower.includes("igp-m")) return "IGP-M +";
  return "IPCA +"; // IPCA+, Renda+ and Educa+ are all inflation-linked
}

function Field({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div>
      <p className="text-xs font-medium text-ink-muted">{label}</p>
      <p className="tnum mt-0.5 text-sm text-ink">{value}</p>
      {hint ? <p className="tnum text-xs text-ink-muted">{hint}</p> : null}
    </div>
  );
}

function TreasuryCard({ item }: { item: TreasuryItem }) {
  const basis = rateBasis(item.name);
  const rateMoved =
    item.contracted_rate !== null && item.buy_rate !== null
      ? Number(item.buy_rate) - Number(item.contracted_rate)
      : null;

  return (
    <Card className="p-5">
      <SectionTitle
        title={item.ticker.replaceAll("-", " ")}
        subtitle={item.name}
        action={
          <div className="text-right">
            <p className="tnum text-lg font-semibold">{money(item.value)}</p>
            <p className={clsx("tnum text-xs", item.unrealized >= 0 ? "text-positive" : "text-negative")}>
              {item.unrealized >= 0 ? "+" : ""}
              {money(item.unrealized)} ({percent(item.unrealized_pct, 2)})
            </p>
          </div>
        }
      />

      <div className="mb-4 flex flex-wrap gap-2">
        <Badge>{quantity(item.quantity)} títulos</Badge>
        <Badge>custo {money(item.cost_basis)}</Badge>
        {item.price_date ? <Badge tone="accent">preço de {shortDate(item.price_date)}</Badge> : null}
        {item.stale ? <Badge tone="warning">preços desatualizados</Badge> : null}
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Field
          label="Resgate hoje (PU)"
          value={item.sell_price === null ? "-" : money(item.sell_price)}
          hint={item.sell_rate === null ? undefined : `${basis} ${percent(item.sell_rate, 2)}`}
        />
        <Field
          label="Compra hoje (PU)"
          value={item.buy_price === null ? "-" : money(item.buy_price)}
          hint={item.buy_rate === null ? undefined : `${basis} ${percent(item.buy_rate, 2)}`}
        />
        <Field label="Preço médio pago" value={money(item.average_price)} />
        <Field
          label="Taxa contratada"
          value={item.contracted_rate === null ? "-" : `${basis} ${percent(item.contracted_rate, 2)}`}
          hint={
            rateMoved === null || item.buy_rate === null
              ? undefined
              : `hoje o mesmo papel sai a ${basis} ${percent(item.buy_rate, 2)} ` +
                `(${rateMoved > 0 ? "+" : ""}${decimal(rateMoved, 2)} p.p.)`
          }
        />
      </div>
    </Card>
  );
}

function TreasuryPanel() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["treasury"],
    queryFn: api.treasury,
  });
  const sync = useMutation({
    mutationFn: api.syncTreasury,
    onSuccess: () => {
      queryClient.invalidateQueries();
      toast.success("Preços do Tesouro atualizados.");
    },
    onError: (err) => toast.error("Não foi possível atualizar os preços do Tesouro.", err),
  });

  if (isError) return <ErrorState error={error} retry={() => refetch()} />;
  if (isLoading || !data) return <Skeleton className="h-64 w-full" />;

  const open = data.items.filter((item) => item.is_open);

  return (
    <section className="space-y-4 animate-fade-up">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <p className="max-w-2xl text-sm text-ink-secondary">
          Preços diários do Tesouro Transparente. A posição é marcada pelo preço de recompra (o que o
          Tesouro paga em um resgate antecipado) e não pelo preço de compra, que é sempre mais alto.
        </p>
        <button type="button" className="btn-ghost" onClick={() => sync.mutate()} disabled={sync.isPending}>
          <RefreshCw size={15} className={sync.isPending ? "animate-spin" : undefined} /> Atualizar preços
        </button>
      </div>

      {!open.length ? (
        <EmptyState
          icon={Landmark}
          title="Nenhum título público em carteira"
          description="Compras de Tesouro Direto importadas do extrato aparecem aqui."
        />
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-3">
            <StatTile label="Investido" value={money(data.totals.cost_basis)} icon={Landmark} />
            <StatTile label="Valor de resgate" value={money(data.totals.value)} tone="accent" />
            <StatTile
              label="Resultado"
              value={money(data.totals.unrealized)}
              tone={data.totals.unrealized >= 0 ? "positive" : "negative"}
              hint={
                data.totals.cost_basis
                  ? percent((data.totals.unrealized / data.totals.cost_basis) * 100, 2)
                  : undefined
              }
            />
          </div>

          {open.map((item) => (
            <TreasuryCard key={item.ticker} item={item} />
          ))}
        </>
      )}
    </section>
  );
}

function PrivatePanel() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["fixed-income"],
    queryFn: api.fixedIncome,
  });

  const sync = useMutation({
    mutationFn: api.syncIndices,
    onSuccess: () => {
      queryClient.invalidateQueries();
      toast.success("Índices atualizados.");
    },
    onError: (err) => toast.error("Não foi possível atualizar os índices.", err),
  });

  const invalidate = () => queryClient.invalidateQueries();

  if (isError) return <ErrorState error={error} retry={() => refetch()} />;
  if (isLoading || !data) return <Skeleton className="h-96 w-full" />;

  const open = data.items.filter((item) => item.is_open);
  const closed = data.items.filter((item) => !item.is_open);
  const cdi = data.indices.find((index) => index.code === "CDI");

  return (
    <div className="space-y-4 animate-fade-up">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <p className="max-w-2xl text-sm text-ink-secondary">
          CDB, LCI, LCA e RDB são calculados: o extrato da B3 não informa a taxa, então ela é configurada
          aqui e o indexador é acumulado desde a data de cada aplicação, com as séries do Banco Central.
        </p>
        <button type="button" className="btn-ghost" onClick={() => sync.mutate()} disabled={sync.isPending}>
          <RefreshCw size={15} className={sync.isPending ? "animate-spin" : undefined} /> Atualizar índices
        </button>
      </div>

      <Card className="flex flex-wrap items-center gap-3 px-5 py-3.5 text-sm" hover={false}>
        <Info size={16} className="text-ink-muted" aria-hidden />
        <span className="text-ink-secondary">Séries do Banco Central:</span>
        {data.indices.map((index) => (
          <Badge key={index.code} tone="neutral">
            {index.code}: {shortDate(index.start)} → {shortDate(index.end)} ({index.points} pontos)
          </Badge>
        ))}
        {cdi ? <span className="text-xs text-ink-muted">atualizado {dateTime(cdi.checked_at)}</span> : null}
      </Card>

      {!open.length ? (
        <EmptyState
          icon={Building2}
          title="Nenhum papel privado em carteira"
          description="CDBs, LCIs, LCAs e RDBs importados do extrato aparecem aqui."
        />
      ) : (
        <section className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-3">
            <StatTile label="Principal aplicado" value={money(data.totals.principal)} icon={Building2} />
            <StatTile label="Valor atualizado" value={money(data.totals.value)} tone="accent" />
            <StatTile
              label="Juros acumulados"
              value={money(data.totals.interest)}
              tone={data.totals.interest >= 0 ? "positive" : "negative"}
              hint={
                data.totals.principal
                  ? percent((data.totals.interest / data.totals.principal) * 100, 2)
                  : undefined
              }
            />
          </div>

          {open.map((item) => (
            <Card key={item.ticker} className="p-5">
              <SectionTitle
                title={item.ticker}
                subtitle={item.name}
                action={
                  item.accrual ? (
                    <div className="text-right">
                      <p className="tnum text-lg font-semibold">{money(item.accrual.value)}</p>
                      <p className="tnum text-xs text-positive">
                        +{money(item.accrual.interest)} ({percent(item.accrual.yield_percent, 2)})
                      </p>
                    </div>
                  ) : null
                }
              />

              {item.accrual ? (
                <div className="mb-4 flex flex-wrap gap-2">
                  <Badge>principal {money(item.accrual.principal)}</Badge>
                  <Badge>{item.accrual.business_days} dias úteis</Badge>
                  <Badge tone="accent">
                    fator {Number(item.accrual.factor).toLocaleString("pt-BR", { maximumFractionDigits: 6 })}
                  </Badge>
                  {item.accrual.stale ? (
                    <Badge tone="warning">série do índice desatualizada, valor até {shortDate(item.accrual.through)}</Badge>
                  ) : null}
                </div>
              ) : (
                <p className="mb-4 text-sm text-ink-muted">
                  Sem aplicações em aberto ou sem série do índice para calcular.
                </p>
              )}

              <TermsEditor item={item} onSaved={invalidate} />
            </Card>
          ))}
        </section>
      )}

      {closed.length ? (
        <Card className="p-5">
          <SectionTitle title="Papéis encerrados" subtitle="Vencidos ou resgatados" />
          <ul className="space-y-2">
            {closed.map((item) => (
              <li key={item.ticker} className="flex flex-wrap items-center gap-3 text-sm">
                <Badge>{item.ticker}</Badge>
                {item.implied ? (
                  <span className="text-ink-secondary">
                    rendeu {money(item.implied.proceeds - item.implied.invested)} sobre{" "}
                    {money(item.implied.invested)}:{" "}
                    <strong className="text-ink">
                      {Number(item.implied.percent_of_index).toLocaleString("pt-BR", {
                        maximumFractionDigits: 1,
                      })}
                      % do {item.implied.index_code}
                    </strong>
                  </span>
                ) : (
                  <span className="text-ink-muted">sem dados de resgate</span>
                )}
              </li>
            ))}
          </ul>
        </Card>
      ) : null}
    </div>
  );
}

/**
 * Bank balances the export cannot reach.
 *
 * Money in a Nubank account is renda fixa in every way that matters — it earns
 * the CDI and it is part of the net worth — so it is entered here and then
 * behaves like any other paper across the app. Each account keeps its own
 * movements: a deposit and a withdrawal both accrue from their own date, which
 * is why R$ 1.000 taken out of a balance that had grown does not cost the
 * interest the balance had already earned.
 */
function AccountEntryForm({ account }: { account: CashAccount }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [kind, setKind] = useState<"deposit" | "withdrawal">("deposit");
  const [amount, setAmount] = useState("");
  const [when, setWhen] = useState(isoToday());

  const add = useMutation({
    mutationFn: () =>
      api.addCashEntry(account.ticker, { amount: parseAmount(amount), date: when, kind }),
    onSuccess: () => {
      const entered = parseAmount(amount);
      setAmount("");
      queryClient.invalidateQueries();
      toast.success(
        `${kind === "deposit" ? "Depósito" : "Saque"} de ${money(entered)} lançado em ${account.name}.`,
      );
    },
    onError: (err) => toast.error("Não foi possível lançar o movimento.", err),
  });

  const value = parseAmount(amount);
  return (
    <div className="mt-3 border-t border-line pt-3">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 lg:items-end">
        <div>
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Movimento</span>
          <Segmented
            size="sm"
            value={kind}
            onChange={setKind}
            options={[
              { value: "deposit", label: "Depósito" },
              { value: "withdrawal", label: "Saque" },
            ]}
          />
        </div>
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Valor (R$)</span>
          <input
            className="input tnum"
            inputMode="decimal"
            placeholder="1.000,00"
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
          />
        </label>
        <div>
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Data</span>
          <DateField ariaLabel="Data do movimento" className="w-full" value={when} onChange={setWhen} />
        </div>
        <button
          type="button"
          className="btn-primary"
          disabled={add.isPending || !(value > 0) || !when}
          onClick={() => add.mutate()}
        >
          <Plus size={15} /> {add.isPending ? "Lançando…" : "Lançar"}
        </button>
      </div>
    </div>
  );
}

function AccountCard({ account }: { account: CashAccount }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  // Deleting a conta takes every lançamento with it — that earns a confirm.
  const [confirmRemove, setConfirmRemove] = useState(false);
  const remove = useMutation({
    mutationFn: () => api.deleteCashAccount(account.ticker),
    onSuccess: () => {
      queryClient.invalidateQueries();
      toast.success(`Conta ${account.name} removida.`);
    },
    onError: (error) => toast.error("Não foi possível remover a conta.", error),
  });
  const removeEntry = useMutation({
    mutationFn: (id: number) => api.deleteCashEntry(account.ticker, id),
    onSuccess: () => {
      queryClient.invalidateQueries();
      toast.success("Lançamento removido.");
    },
    onError: (error) => toast.error("Não foi possível remover o lançamento.", error),
  });

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{account.name}</span>
            <Badge>
              {Number(account.percent_of_index)}% do {account.index_code}
            </Badge>
            {account.stale ? <Badge tone="warning">índice atrasado</Badge> : null}
          </div>
          <p className="mt-1 text-xs text-ink-muted">
            {account.since ? `desde ${shortDate(account.since)}` : "sem lançamentos"}
            {account.business_days ? ` · ${account.business_days} dias úteis` : ""}
          </p>
        </div>
        <div className="text-right">
          <p className="tnum text-xl font-semibold">{money(account.balance)}</p>
          <p className="tnum text-xs text-ink-muted">
            {money(account.principal)} aplicados{" "}
            <span className="text-positive">
              + {money(account.interest)} ({percent(account.yield_percent, 2)})
            </span>
          </p>
        </div>
      </div>

      <AccountEntryForm account={account} />

      <div className="mt-3 flex items-center justify-between gap-3">
        <button type="button" className="btn-ghost text-xs" onClick={() => setOpen((value) => !value)}>
          {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          {account.entries.length} lançamento{account.entries.length === 1 ? "" : "s"}
        </button>
        <button
          type="button"
          className="btn-ghost text-xs text-negative"
          disabled={remove.isPending}
          onClick={() => setConfirmRemove(true)}
          title="Remover a conta e todos os seus lançamentos"
        >
          <Trash2 size={14} /> Remover conta
        </button>
      </div>

      <Modal
        open={confirmRemove}
        title={`Remover a conta ${account.name}?`}
        subtitle={`${account.entries.length} lançamento${account.entries.length === 1 ? "" : "s"} · saldo ${money(account.balance)}`}
        onClose={() => setConfirmRemove(false)}
      >
        <p className="text-sm text-ink-secondary">
          A conta e todos os seus lançamentos serão apagados. Esta ação não pode ser desfeita:
          os depósitos e saques teriam de ser lançados de novo.
        </p>
        <div className="mt-4 flex flex-wrap justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={() => setConfirmRemove(false)}>
            Cancelar
          </button>
          <button
            type="button"
            className="btn-primary bg-negative hover:brightness-110"
            disabled={remove.isPending}
            onClick={() => {
              remove.mutate();
              setConfirmRemove(false);
            }}
          >
            {remove.isPending ? "Removendo…" : "Remover conta"}
          </button>
        </div>
      </Modal>

      {open ? (
        <ul className="mt-2 max-h-72 space-y-1 overflow-auto pr-1">
          {account.entries.map((entry) => (
            <li
              key={entry.id}
              className="flex items-center gap-3 rounded-lg px-2 py-1.5 text-sm hover:bg-surface-hover"
            >
              <span className="tnum w-[74px] shrink-0 text-xs text-ink-muted">{shortDate(entry.date)}</span>
              <Badge tone={entry.kind === "deposit" ? "positive" : "negative"}>
                {entry.kind === "deposit" ? "Depósito" : "Saque"}
              </Badge>
              <span
                className={clsx(
                  "tnum ml-auto font-medium",
                  entry.kind === "deposit" ? "text-positive" : "text-negative",
                )}
              >
                {entry.kind === "deposit" ? "" : "− "}
                {money(entry.amount)}
              </span>
              <button
                type="button"
                className="btn-ghost px-2.5 py-2"
                aria-label="Remover lançamento"
                disabled={removeEntry.isPending}
                onClick={() => removeEntry.mutate(entry.id)}
              >
                <Trash2 size={14} />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </Card>
  );
}

function NewAccountForm() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [amount, setAmount] = useState("");
  const [when, setWhen] = useState(isoToday());
  const [rate, setRate] = useState("100");

  const create = useMutation({
    mutationFn: () =>
      api.createCashAccount({
        name,
        percent_of_index: parseAmount(rate) || 100,
        opening_amount: parseAmount(amount) || null,
        opening_date: when || null,
      }),
    onSuccess: () => {
      const created = name.trim();
      setName("");
      setAmount("");
      queryClient.invalidateQueries();
      toast.success(`Conta ${created} criada.`);
    },
    onError: (err) => toast.error("Não foi possível criar a conta.", err),
  });

  return (
    <Card className="p-5">
      <SectionTitle
        title="Nova conta"
        subtitle="O saldo entra no patrimônio como renda fixa e rende o índice a partir da data informada"
      />
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5 lg:items-end">
        <label className="block lg:col-span-2">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Onde está o dinheiro</span>
          <input
            className="input"
            placeholder="Nubank"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Saldo inicial (R$)</span>
          <input
            className="input tnum"
            inputMode="decimal"
            placeholder="100.000,00"
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
          />
        </label>
        <div>
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Desde</span>
          <DateField ariaLabel="Data do saldo inicial" className="w-full" value={when} onChange={setWhen} />
        </div>
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">% do CDI</span>
          <input
            className="input tnum"
            inputMode="decimal"
            value={rate}
            onChange={(event) => setRate(event.target.value)}
          />
        </label>
      </div>
      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          className="btn-primary"
          disabled={create.isPending || !name.trim()}
          onClick={() => create.mutate()}
        >
          <Plus size={15} /> {create.isPending ? "Criando…" : "Adicionar conta"}
        </button>
      </div>
    </Card>
  );
}

function AccountsPanel() {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["cash-accounts"],
    queryFn: api.cashAccounts,
  });

  if (isError) return <ErrorState error={error} retry={() => refetch()} />;
  if (isLoading || !data) return <Skeleton className="h-64 w-full" />;

  return (
    <div className="space-y-4">
      {data.items.length ? (
        <section className="grid gap-4 sm:grid-cols-3">
          <StatTile label="Saldo total" value={money(data.totals.balance)} icon={Wallet} tone="accent" />
          <StatTile label="Aplicado" value={money(data.totals.principal)} />
          <StatTile
            label="Juros acumulados"
            value={money(data.totals.interest)}
            tone="positive"
            icon={TrendingUp}
          />
        </section>
      ) : (
        <EmptyState
          icon={Wallet}
          title="Nenhuma conta cadastrada"
          description="Cadastre o dinheiro parado no banco para que ele entre no patrimônio e renda o CDI."
        />
      )}

      {data.items.map((account) => (
        <AccountCard key={account.ticker} account={account} />
      ))}

      <NewAccountForm />
    </div>
  );
}

/**
 * The page holds several kinds of fixed income that share nothing but the name:
 * each has its own source, its own refresh and its own totals. Tabs keep one
 * kind on screen at a time instead of stacking unrelated sections.
 */
const TABS = [
  { value: "privados", label: "Papéis privados", icon: Building2 },
  { value: "tesouro", label: "Tesouro Direto", icon: Landmark },
  { value: "contas", label: "Contas", icon: Wallet },
] as const;

type TabValue = (typeof TABS)[number]["value"];

export default function FixedIncome() {
  // The tab lives in the URL, so a reload or a shared link lands on the same view.
  const [params, setParams] = useSearchParams();
  const requested = params.get("aba");
  const tab: TabValue = TABS.some((option) => option.value === requested)
    ? (requested as TabValue)
    : "privados";

  // Counts label the tabs; each panel still owns the query it renders from.
  const fixedIncome = useQuery({ queryKey: ["fixed-income"], queryFn: api.fixedIncome });
  const treasury = useQuery({ queryKey: ["treasury"], queryFn: api.treasury });
  const cash = useQuery({ queryKey: ["cash-accounts"], queryFn: api.cashAccounts });

  const openCount = (items?: { is_open: boolean }[]) =>
    items ? items.filter((item) => item.is_open).length : undefined;

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">Carteira</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Renda fixa</h1>
        <p className="mt-2 max-w-2xl text-sm text-ink-secondary">
          Papéis privados são calculados a partir do indexador contratado; títulos públicos são cotados: o
          Tesouro publica o preço de cada título todo dia útil. Contas guardam o dinheiro que nenhum extrato
          alcança, rendendo o índice desde a data de cada movimento.
        </p>
      </header>

      <Tabs
        value={tab}
        onChange={(value) => setParams(value === "privados" ? {} : { aba: value }, { replace: true })}
        options={[
          { ...TABS[0], count: openCount(fixedIncome.data?.items) },
          { ...TABS[1], count: openCount(treasury.data?.items) },
          { ...TABS[2], count: cash.data?.items.length },
        ]}
      />

      {tab === "privados" ? <PrivatePanel /> : tab === "tesouro" ? <TreasuryPanel /> : <AccountsPanel />}
    </div>
  );
}
