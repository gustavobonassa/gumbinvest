/**
 * Aporte inteligente — the AI splits the day's contribution across assets the
 * user already owns.
 *
 * The one AI feature that sees the real portfolio, on purpose: pick how much
 * new money there is and which categories it may go to, and the model weighs
 * the current holdings (price vs. cost, fundamentals, news, macro) to say
 * where the marginal real works hardest. The analysis runs as a backend job —
 * navigating away never loses it — and nothing is ever executed automatically.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Check, ChevronDown, History, PiggyBank, Sparkles, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useToast } from "@/components/Toast";
import { Badge, Card, EmptyState, ErrorState, SectionTitle, Select, Skeleton, Tabs } from "@/components/ui";
import { api, ApiError, type SmartInvestResult, type SmartInvestRun } from "@/lib/api";
import { dateTime, money, quantity as qty } from "@/lib/format";

/** Kinds with no ticker page to link to. */
const UNLINKED_KINDS = new Set(["FIXED_INCOME", "TREASURY"]);

export default function AporteInteligente() {
  const queryClient = useQueryClient();
  const toast = useToast();

  // The tab lives in the URL, like every other tabbed page.
  const [params, setParams] = useSearchParams();
  const aba = params.get("aba") === "historico" ? "historico" : "novo";
  const setAba = (next: string) =>
    setParams(next === "novo" ? {} : { aba: next }, { replace: true });

  const optionsQ = useQuery({ queryKey: ["smart-invest-options"], queryFn: api.smartInvestOptions });
  const settingsQ = useQuery({ queryKey: ["settings"], queryFn: api.settings, staleTime: 5 * 60_000 });
  const historyQ = useQuery({ queryKey: ["smart-invest-history"], queryFn: api.smartInvestHistory });

  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState<"BRL" | "USD">("BRL");
  const [kinds, setKinds] = useState<string[]>([]);
  // Completion feedback only for a run started in this visit — not for an
  // hour-old result the registry still remembers when the page reopens.
  const watchingRef = useRef(false);

  const jobQ = useQuery({
    queryKey: ["smart-invest-job"],
    queryFn: api.smartInvestJob,
    refetchInterval: (query) => (query.state.data?.active ? 2_000 : false),
  });
  const job = jobQ.data;
  const running = Boolean(job?.active);
  const result = !running && job?.result ? (job.result as unknown as SmartInvestResult) : null;

  useEffect(() => {
    if (!job || job.active || !watchingRef.current) return;
    watchingRef.current = false;
    if (job.error) toast.error("A análise do aporte falhou.", new Error(job.error));
    else {
      toast.success("Distribuição do aporte pronta.");
      queryClient.invalidateQueries({ queryKey: ["smart-invest-history"] });
    }
  }, [job, toast, queryClient]);

  // No picker here: the analysis always runs with the provider/model chosen
  // in Configurações — shown below with a link for whoever wants to change it.
  const activeAi = settingsQ.data?.ai;
  const activeProvider = activeAi?.providers.find((item) => item.id === activeAi.active_provider);
  const keyConfigured = Boolean(activeProvider?.key_configured);

  const start = useMutation({
    mutationFn: () => api.startSmartInvest({ amount: Number(amount), currency, kinds }),
    onSuccess: () => {
      watchingRef.current = true;
      queryClient.invalidateQueries({ queryKey: ["smart-invest-job"] });
    },
    onError: (error) =>
      toast.error("Não foi possível iniciar a análise.", error instanceof ApiError ? error : undefined),
  });

  const parsedAmount = Number(amount);
  const canStart =
    !running && keyConfigured && kinds.length > 0 && Number.isFinite(parsedAmount) && parsedAmount > 0;

  const toggleKind = (kind: string) =>
    setKinds((current) =>
      current.includes(kind) ? current.filter((item) => item !== kind) : [...current, kind],
    );

  if (optionsQ.isError) return <ErrorState error={optionsQ.error} retry={() => optionsQ.refetch()} />;

  const categories = optionsQ.data?.categories ?? [];

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">IA</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Aporte inteligente</h1>
        <p className="mt-2 max-w-2xl text-sm text-ink-secondary">
          Diga quanto você tem para investir hoje e em quais categorias. A IA analisa os ativos
          que você já possui, preço atual contra preço médio, fundamentos, notícias e cenário
          macro, e sugere como dividir o valor. Nada é executado: a sugestão é sua para levar
          à corretora.
        </p>
      </header>

      <Tabs
        value={aba}
        onChange={setAba}
        options={[
          { value: "novo", label: "Novo aporte", icon: Sparkles },
          {
            value: "historico",
            label: "Histórico",
            icon: History,
            count: historyQ.data?.length || undefined,
          },
        ]}
      />

      {aba === "historico" ? (
        <HistoryCard runs={historyQ.data ?? []} loading={historyQ.isLoading} />
      ) : optionsQ.isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : categories.length === 0 ? (
        <EmptyState
          icon={PiggyBank}
          title="Nenhum ativo elegível"
          description="O aporte inteligente distribui entre os ativos que você já possui. Importe suas movimentações primeiro."
        />
      ) : (
        <Card className="p-5">
          <SectionTitle
            title="Novo aporte"
            subtitle="Quanto entra hoje e onde pode ser investido"
          />
          <div className="grid max-w-xl gap-4 sm:grid-cols-2">
            <div>
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">Valor do aporte</span>
              <input
                type="number"
                min="0"
                step="0.01"
                inputMode="decimal"
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
                placeholder="10000"
                className="input w-full"
              />
            </div>
            <div>
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">Moeda</span>
              <Select
                ariaLabel="Moeda do aporte"
                value={currency}
                onChange={setCurrency}
                options={[
                  { value: "BRL", label: "Real (R$)" },
                  { value: "USD", label: "Dólar (US$)" },
                ]}
              />
            </div>
          </div>

          <div className="mt-4">
            <span className="mb-1.5 block text-xs font-medium text-ink-muted">
              Categorias que podem receber o aporte
            </span>
            <div className="flex flex-wrap gap-2">
              {categories.map((category) => {
                const selected = kinds.includes(category.kind);
                return (
                  <button
                    key={category.kind}
                    type="button"
                    onClick={() => toggleKind(category.kind)}
                    aria-pressed={selected}
                    className={clsx(
                      "flex items-center gap-1.5 rounded-xl border px-3 py-2 text-sm transition-colors",
                      selected
                        ? "border-accent/60 bg-accent-soft text-accent"
                        : "border-line text-ink-secondary hover:bg-surface-hover",
                    )}
                  >
                    <Check size={14} className={clsx(!selected && "opacity-0")} aria-hidden />
                    {category.label}
                    <span className="text-xs text-ink-muted">
                      {category.count} {category.count === 1 ? "ativo" : "ativos"}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          {!keyConfigured && !settingsQ.isLoading ? (
            <p className="mt-4 text-sm text-ink-muted">
              Informe a chave{activeProvider ? ` da ${activeProvider.label}` : " de um provedor de IA"} em{" "}
              <Link to="/configuracoes?aba=ia" className="text-accent hover:underline">
                Configurações → Inteligência Artificial
              </Link>{" "}
              para usar esta ferramenta.
            </p>
          ) : null}

          <div className="mt-5 flex flex-wrap items-center gap-3">
            <button
              type="button"
              className="btn-primary"
              disabled={!canStart || start.isPending}
              onClick={() => start.mutate()}
            >
              <Sparkles size={15} /> {running ? "Analisando…" : "Distribuir com IA"}
            </button>
            {!running && keyConfigured && activeProvider ? (
              <p className="text-xs text-ink-muted">
                Análise com {activeProvider.label} · {activeAi?.active_model}{" "}
                <Link to="/configuracoes?aba=ia" className="text-accent hover:underline">
                  alterar
                </Link>
              </p>
            ) : null}
            {running ? (
              <p className="flex items-center gap-2 text-sm text-ink-secondary">
                <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" aria-hidden />
                {job?.status ?? "Analisando…"}
                <span className="text-xs text-ink-muted">
                  Pode levar alguns minutos; você pode navegar à vontade.
                </span>
              </p>
            ) : null}
          </div>
          {!running && job?.error ? (
            <p className="mt-3 text-sm text-negative">{job.error}</p>
          ) : null}
        </Card>
      )}

      {aba === "novo" && result ? <ResultCard result={result} /> : null}
    </div>
  );
}

/** The AI's split, fresh from the job, in its own card. */
function ResultCard({ result }: { result: SmartInvestResult }) {
  return (
    <Card className="p-5">
      <SectionTitle
        title="Distribuição sugerida"
        subtitle={`Aporte de ${money(Number(result.amount), { currency: result.currency })} em ${result.categories.join(", ")}`}
      />
      <ResultDetails result={result} />
    </Card>
  );
}

/** The split itself, grouped by category, with the audit trail on display —
 *  shared between the live result and expanded history entries. */
function ResultDetails({ result }: { result: SmartInvestResult }) {
  const fmt = (value: string | number | null | undefined, code?: string) =>
    value === null || value === undefined ? "—" : money(Number(value), { currency: code ?? result.currency });

  const groups = new Map<string, typeof result.allocations>();
  for (const item of result.allocations) {
    const list = groups.get(item.label) ?? [];
    list.push(item);
    groups.set(item.label, list);
  }
  const leftover = Number(result.leftover);
  // A selected category the model left empty is a decision, not an omission —
  // it must be visible, next to the strategy that should be justifying it.
  const emptyCategories = result.categories.filter((label) => !groups.has(label));

  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="accent">
          {result.provider_label} · {result.model}
        </Badge>
        <Badge tone={result.used_search ? "positive" : "neutral"}>
          {result.used_search ? "com busca na web" : "sem busca na web"}
        </Badge>
        <span className="text-xs text-ink-muted">{dateTime(result.generated_at)}</span>
      </div>

      {result.strategy ? (
        <p className="mt-4 rounded-xl border border-line bg-surface-raised/50 p-3 text-sm text-ink-secondary">
          {result.strategy}
        </p>
      ) : null}

      <div className="mt-4 space-y-5">
        {[...groups.entries()].map(([label, items]) => (
          <div key={label}>
            <div className="mb-2 flex items-baseline justify-between gap-2">
              <h3 className="text-sm font-semibold text-ink">{label}</h3>
              <span className="tnum text-sm text-ink-secondary">
                {fmt(items.reduce((sum, item) => sum + Number(item.amount), 0))}
              </span>
            </div>
            <ul className="space-y-2">
              {items.map((item) => (
                <li key={item.ticker} className="rounded-xl border border-line p-3">
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="min-w-0">
                      {UNLINKED_KINDS.has(item.kind) ? (
                        <span className="font-medium">{item.ticker}</span>
                      ) : (
                        <Link to={`/ativos/${item.ticker}`} className="font-medium text-accent hover:underline">
                          {item.ticker}
                        </Link>
                      )}
                      {item.name && item.name !== item.ticker ? (
                        <span className="ml-2 text-xs text-ink-muted">{item.name}</span>
                      ) : null}
                    </span>
                    <span className="tnum text-sm font-medium">{fmt(item.amount)}</span>
                  </div>
                  <p className="tnum mt-0.5 text-xs text-ink-muted">
                    {item.approx_quantity !== null
                      ? `≈ ${qty(Number(item.approx_quantity))} × ${fmt(item.current_price, item.price_currency)}`
                      : "aporte em valor"}
                  </p>
                  {item.rationale ? (
                    <p className="mt-1.5 text-sm text-ink-secondary">{item.rationale}</p>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      {emptyCategories.length ? (
        <p className="mt-4 text-xs text-warning">
          O modelo não alocou nada em: {emptyCategories.join(", ")}. O motivo deve estar na
          estratégia acima — se não estiver, vale rodar de novo ou trocar o modelo.
        </p>
      ) : null}
      {leftover > 0 ? (
        <p className="mt-4 text-sm text-ink-secondary">
          Fica em caixa: <span className="tnum font-medium">{fmt(result.leftover)}</span>
          {result.strategy ? "" : " — o modelo preferiu não alocar tudo."}
        </p>
      ) : null}
      {result.skipped.length ? (
        <p className="mt-2 text-xs text-warning">
          Ignorados por não fazerem parte da sua carteira: {result.skipped.join(", ")}.
        </p>
      ) : null}
    </>
  );
}

/** Every past analysis, expandable in place — the advice is never lost. */
function HistoryCard({ runs, loading }: { runs: SmartInvestRun[]; loading: boolean }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [openId, setOpenId] = useState<number | null>(null);

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteSmartInvestRun(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["smart-invest-history"] });
      toast.success("Análise removida do histórico.");
    },
    onError: (error) =>
      toast.error("Não foi possível remover a análise.", error instanceof ApiError ? error : undefined),
  });

  if (loading) return <Skeleton className="h-40 w-full" />;
  if (!runs.length) {
    return (
      <EmptyState
        icon={History}
        title="Nenhuma análise salva ainda"
        description="Cada aporte analisado fica registrado aqui, com os preços e as justificativas do momento em que foi sugerido."
      />
    );
  }

  return (
    <Card className="p-5">
      <SectionTitle
        title="Histórico de aportes"
        subtitle="Cada análise fica salva com os preços e justificativas do momento"
      />
      <ul className="divide-y divide-line">
        {runs.map((run: SmartInvestRun) => {
          const open = openId === run.id;
          return (
            <li key={run.id} className="py-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => setOpenId(open ? null : run.id)}
                  aria-expanded={open}
                  className="flex min-w-0 flex-1 items-center gap-2 text-left"
                >
                  <ChevronDown
                    size={15}
                    className={clsx(
                      "shrink-0 text-ink-muted transition-transform duration-200",
                      open && "rotate-180",
                    )}
                    aria-hidden
                  />
                  <span className="tnum font-medium">
                    {money(Number(run.amount), { currency: run.currency })}
                  </span>
                  <span className="min-w-0 truncate text-sm text-ink-secondary">
                    {run.categories.join(", ")}
                  </span>
                  <span className="ml-auto text-xs text-ink-muted">{dateTime(run.created_at)}</span>
                </button>
                <button
                  type="button"
                  onClick={() => remove.mutate(run.id)}
                  disabled={remove.isPending}
                  className="rounded-lg p-2 text-ink-muted transition-colors hover:bg-surface-hover hover:text-negative"
                  aria-label="Remover análise"
                >
                  <Trash2 size={14} />
                </button>
              </div>
              {open ? (
                <div className="mt-3 border-l-2 border-line pl-3">
                  <ResultDetails result={run} />
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
