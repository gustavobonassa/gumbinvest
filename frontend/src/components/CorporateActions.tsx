/** Declaring that an asset was replaced by another (merger, rename, write-off). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Check, GitMerge, Plus, Sparkles, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useToast } from "@/components/Toast";
import { Badge, Card, DateField, ErrorState, Modal, SectionTitle, Select, Skeleton } from "@/components/ui";
import { api, ApiError, type CorporateAiSuggestion, type SuccessionSuggestion } from "@/lib/api";
import { money, quantity as qty, shortDate } from "@/lib/format";

const EVENT_TYPE_LABELS: Record<CorporateAiSuggestion["event_type"], string> = {
  rename: "Mudança de ticker",
  merger: "Incorporação",
  delisting: "Fechamento de capital",
  spinoff: "Cisão",
  other: "Evento",
};

/** The AI scan: a background job proposing events for the user's own tickers. */
function AiEventScan() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const processedJobRef = useRef<string | null>(null);

  const jobQ = useQuery({
    queryKey: ["corporate-ai-scan"],
    queryFn: api.corporateAiScan,
    refetchInterval: (query) => (query.state.data?.active ? 2_000 : false),
  });
  const job = jobQ.data;
  const running = Boolean(job?.active);

  const suggestionsQ = useQuery({
    queryKey: ["corporate-ai-suggestions"],
    queryFn: api.corporateAiSuggestions,
  });

  useEffect(() => {
    if (!job || job.active || !job.id) return;
    if (processedJobRef.current === job.id) return;
    processedJobRef.current = job.id;
    queryClient.invalidateQueries({ queryKey: ["corporate-ai-suggestions"] });
  }, [job, queryClient]);

  const start = useMutation({
    mutationFn: api.startCorporateAiScan,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["corporate-ai-scan"] }),
    onError: (error) =>
      toast.error(
        "Não foi possível iniciar a busca.",
        error instanceof ApiError ? error : undefined,
      ),
  });

  const settle = () => {
    queryClient.invalidateQueries(); // accepting reprocesses the whole history
  };
  const accept = useMutation({
    mutationFn: api.acceptCorporateAiSuggestion,
    onSuccess: (data) => {
      settle();
      toast.success(
        data.suggestion.to_ticker
          ? `${data.suggestion.from_ticker} passou a ser ${data.suggestion.to_ticker}.`
          : `${data.suggestion.from_ticker} baixado.`,
        { description: "O histórico foi reprocessado com o evento." },
      );
    },
    onError: (error) => toast.error("Não foi possível aplicar o evento.", error),
  });
  const decline = useMutation({
    mutationFn: api.declineCorporateAiSuggestion,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["corporate-ai-suggestions"] });
      toast.success("Sugestão recusada.", {
        description: "Ela não volta a aparecer em buscas futuras.",
      });
    },
    onError: (error) => toast.error("Não foi possível recusar.", error),
  });

  const result = job?.result as { found?: number; stored?: number } | null;
  const items = suggestionsQ.data ?? [];

  return (
    <div className="border-t border-line pt-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-ink-secondary">
          A IA pesquisa na web eventos que afetaram os seus ativos. Cada achado vira uma
          sugestão para você aceitar ou recusar, agora ou depois:
        </p>
        <button
          type="button"
          className="btn-ghost"
          disabled={running || start.isPending}
          onClick={() => start.mutate()}
        >
          <Sparkles size={15} /> {running ? "Buscando…" : "Buscar eventos com IA"}
        </button>
      </div>

      {running && job?.status ? (
        <p className="mt-2 flex items-center gap-2 text-sm text-ink-secondary">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" aria-hidden />
          {job.status}
          <span className="text-xs text-ink-muted">: pode levar alguns minutos; você pode navegar à vontade.</span>
        </p>
      ) : null}
      {!running && job?.error ? <p className="mt-2 text-sm text-negative">{job.error}</p> : null}
      {!running && result && !items.length ? (
        <p className="mt-2 text-xs text-ink-muted">
          {result.found
            ? "Nenhuma sugestão nova. Os eventos encontrados já estavam declarados ou decididos."
            : "Nenhum evento corporativo encontrado para os seus ativos."}
        </p>
      ) : null}

      {items.length ? (
        <ul className="mt-3 space-y-3">
          {items.map((item) => (
            <li key={item.id} className="rounded-xl border border-line bg-surface-raised/50 p-3.5">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone="accent">{EVENT_TYPE_LABELS[item.event_type] ?? item.event_type}</Badge>
                <Badge>{item.from_ticker}</Badge>
                <ArrowRight size={14} className="text-ink-muted" aria-hidden />
                {item.to_ticker ? (
                  <Badge tone="accent">{item.to_ticker}</Badge>
                ) : (
                  <span className="text-sm text-ink-muted">baixado</span>
                )}
                <span className="text-sm text-ink-secondary">{shortDate(item.effective_date)}</span>
                {Number(item.cash_amount) ? (
                  <span className="tnum text-sm text-ink-secondary">caixa {money(item.cash_amount)}</span>
                ) : null}
                <Badge className="ml-auto">{item.model}</Badge>
              </div>
              {item.rationale ? (
                <p className="mt-2 max-w-3xl text-sm text-ink-secondary">{item.rationale}</p>
              ) : null}
              {item.source ? <p className="mt-1 text-xs text-ink-muted">Fonte: {item.source}</p> : null}
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  className="btn-primary px-3 py-1.5 text-sm"
                  disabled={accept.isPending || decline.isPending}
                  onClick={() => accept.mutate(item.id)}
                >
                  <Check size={14} /> Aceitar
                </button>
                <button
                  type="button"
                  className="btn-ghost px-3 py-1.5 text-sm"
                  disabled={accept.isPending || decline.isPending}
                  onClick={() => decline.mutate(item.id)}
                >
                  <X size={14} /> Recusar
                </button>
              </div>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

const VOID = "__void__";

function SuggestionRow({
  suggestion,
  onApply,
  pending,
}: {
  suggestion: SuccessionSuggestion;
  onApply: (payload: { to_ticker: string | null; effective_date: string; cash_amount: number }) => void;
  pending: boolean;
}) {
  const best = suggestion.candidates[0];
  const [target, setTarget] = useState<string>(best ? best.ticker : VOID);
  const [cash, setCash] = useState("0");

  const chosen = suggestion.candidates.find((candidate) => candidate.ticker === target);
  const effective = chosen ? chosen.date : suggestion.last_trade;

  return (
    <li className="rounded-xl border border-line bg-surface-raised/50 p-3.5">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-medium">{suggestion.ticker}</span>
        <span className="text-sm text-ink-secondary">{suggestion.name}</span>
        <span className="tnum text-xs text-ink-muted">
          {qty(suggestion.quantity)} un. · custo {money(suggestion.cost_basis)} · sem movimento desde{" "}
          {shortDate(suggestion.last_trade)}
        </span>
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 lg:items-end">
        <div className="lg:col-span-2">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Substituído por</span>
          <Select
            ariaLabel="Substituído por"
            value={target}
            onChange={setTarget}
            options={[
              ...suggestion.candidates.map((candidate) => ({
                value: candidate.ticker,
                label:
                  `${candidate.ticker}: ${candidate.movement} de ${qty(candidate.quantity)} un. em ` +
                  `${shortDate(candidate.date)}${candidate.exact_quantity_match ? " (quantidade idêntica)" : ""}`,
              })),
              { value: VOID, label: "Nenhum: baixar o ativo" },
            ]}
          />
        </div>

        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Caixa recebido (R$)</span>
          <input
            className="input tnum"
            inputMode="decimal"
            value={cash}
            onChange={(event) => setCash(event.target.value)}
          />
        </label>

        <button
          type="button"
          className="btn-primary"
          disabled={pending}
          onClick={() =>
            onApply({
              to_ticker: target === VOID ? null : target,
              effective_date: effective,
              cash_amount: Number(cash.replace(",", ".")) || 0,
            })
          }
        >
          <GitMerge size={15} /> Aplicar
        </button>
      </div>

      {best?.exact_quantity_match ? (
        <p className="mt-2 text-xs text-ink-muted">
          {best.ticker} recebeu exatamente {qty(best.quantity)} unidades (a mesma posição, creditada sem
          custo).
        </p>
      ) : null}
    </li>
  );
}

function ManualForm({
  onSubmit,
  pending,
}: {
  onSubmit: (payload: {
    from_ticker: string;
    to_ticker: string | null;
    effective_date: string;
    cash_amount: number;
  }) => void;
  pending: boolean;
}) {
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [when, setWhen] = useState("");
  const [cash, setCash] = useState("0");

  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5 lg:items-end">
      <label className="block">
        <span className="mb-1.5 block text-xs font-medium text-ink-muted">Ativo antigo</span>
        <input
          className="input uppercase"
          value={from}
          placeholder="SULA11"
          onChange={(event) => setFrom(event.target.value)}
        />
      </label>
      <label className="block">
        <span className="mb-1.5 block text-xs font-medium text-ink-muted">
          Substituído por <span className="font-normal">(vazio = baixar)</span>
        </span>
        <input
          className="input uppercase"
          value={to}
          placeholder="RDOR3"
          onChange={(event) => setTo(event.target.value)}
        />
      </label>
      <div>
        <span className="mb-1.5 block text-xs font-medium text-ink-muted">Data do evento</span>
        <DateField ariaLabel="Data do evento" className="w-full" value={when} onChange={setWhen} />
      </div>
      <label className="block">
        <span className="mb-1.5 block text-xs font-medium text-ink-muted">Caixa recebido (R$)</span>
        <input
          className="input tnum"
          inputMode="decimal"
          value={cash}
          onChange={(event) => setCash(event.target.value)}
        />
      </label>
      <button
        type="button"
        className="btn-ghost"
        disabled={pending || !from.trim() || !when}
        onClick={() =>
          onSubmit({
            from_ticker: from.trim().toUpperCase(),
            to_ticker: to.trim() ? to.trim().toUpperCase() : null,
            effective_date: when,
            cash_amount: Number(cash.replace(",", ".")) || 0,
          })
        }
      >
        <Plus size={15} /> Declarar
      </button>
    </div>
  );
}

export default function CorporateActions() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["corporate-actions"],
    queryFn: api.corporateActions,
  });
  // Undoing a succession reprocesses the entire history — it earns a confirm.
  const [undoTarget, setUndoTarget] = useState<{ id: number; from: string; to: string | null } | null>(null);

  const create = useMutation({
    mutationFn: api.createSuccession,
    onSuccess: (_data, payload) => {
      queryClient.invalidateQueries();
      toast.success(
        payload.to_ticker
          ? `${payload.from_ticker} passou a ser ${payload.to_ticker}.`
          : `${payload.from_ticker} baixado.`,
        { description: "O histórico foi reprocessado com o evento." },
      );
    },
    onError: (error) => toast.error("Não foi possível registrar o evento.", error),
  });
  const remove = useMutation({
    mutationFn: api.deleteSuccession,
    onSuccess: () => {
      queryClient.invalidateQueries();
      toast.success("Evento desfeito.");
    },
    onError: (error) => toast.error("Não foi possível desfazer o evento.", error),
  });

  return (
    <Card className="p-5">
      <SectionTitle
        title="Eventos corporativos"
        subtitle="O extrato credita o ativo novo e nunca debita o antigo; o vínculo é declarado aqui"
      />

      {isError ? (
        <ErrorState error={error} retry={() => refetch()} />
      ) : isLoading || !data ? (
        <Skeleton className="h-24 w-full" />
      ) : (
        <div className="space-y-5">
          {data.items.length ? (
            <ul className="space-y-2">
              {data.items.map((item) => (
                <li
                  key={item.id}
                  className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-surface-raised/50 px-3.5 py-2.5 text-sm"
                >
                  <Badge>{item.from_ticker}</Badge>
                  <ArrowRight size={14} className="text-ink-muted" aria-hidden />
                  {item.to_ticker ? (
                    <Badge tone="accent">{item.to_ticker}</Badge>
                  ) : (
                    <span className="text-ink-muted">baixado</span>
                  )}
                  <span className="text-ink-secondary">{shortDate(item.effective_date)}</span>
                  {Number(item.cash_amount) ? (
                    <span className="tnum text-ink-secondary">caixa {money(item.cash_amount)}</span>
                  ) : null}
                  {item.note ? <span className="text-xs text-ink-muted">{item.note}</span> : null}
                  <button
                    type="button"
                    className="btn-ghost ml-auto px-2.5 py-1.5 text-xs"
                    onClick={() => setUndoTarget({ id: item.id, from: item.from_ticker, to: item.to_ticker })}
                  >
                    <Trash2 size={14} /> Desfazer
                  </button>
                </li>
              ))}
            </ul>
          ) : null}

          {data.suggestions.length ? (
            <div>
              <p className="mb-2 text-sm text-ink-secondary">
                Posições paradas sem cotação, com outro ativo creditado logo em seguida:
              </p>
              <ul className="space-y-3">
                {data.suggestions.map((suggestion) => (
                  <SuggestionRow
                    key={suggestion.ticker}
                    suggestion={suggestion}
                    pending={create.isPending}
                    onApply={(payload) =>
                      create.mutate({
                        from_ticker: suggestion.ticker,
                        source: "detected",
                        ...payload,
                      })
                    }
                  />
                ))}
              </ul>
            </div>
          ) : (
            <p className="text-sm text-ink-muted">
              {data.items.length
                ? "Nenhuma outra posição órfã encontrada."
                : "Nenhuma posição órfã encontrada."}
            </p>
          )}

          <AiEventScan />

          <div className="border-t border-line pt-4">
            <p className="mb-3 text-sm text-ink-secondary">
              Declarar um evento que a detecção não encontrou (inclusive veículos intermediários, que o
              extrato cria e resgata dentro da própria operação):
            </p>
            <ManualForm onSubmit={(payload) => create.mutate(payload)} pending={create.isPending} />
          </div>
        </div>
      )}

      <Modal
        open={undoTarget !== null}
        title="Desfazer evento corporativo?"
        subtitle={
          undoTarget
            ? undoTarget.to
              ? `${undoTarget.from} → ${undoTarget.to}`
              : `Baixa de ${undoTarget.from}`
            : undefined
        }
        onClose={() => setUndoTarget(null)}
      >
        <p className="text-sm text-ink-secondary">
          Todo o histórico será reprocessado sem este vínculo: a posição antiga volta a aparecer
          em aberto e o custo carregado retorna a ela. Nada é apagado das movimentações. Dá para
          declarar o evento de novo depois.
        </p>
        <div className="mt-4 flex flex-wrap justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={() => setUndoTarget(null)}>
            Cancelar
          </button>
          <button
            type="button"
            className="btn-primary bg-negative hover:brightness-110"
            disabled={remove.isPending}
            onClick={() => {
              if (undoTarget) remove.mutate(undoTarget.id);
              setUndoTarget(null);
            }}
          >
            {remove.isPending ? "Desfazendo…" : "Desfazer evento"}
          </button>
        </div>
      </Modal>
    </Card>
  );
}
