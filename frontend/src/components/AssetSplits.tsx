/**
 * Declared share splits, and the form for the ones nobody declared.
 *
 * Why this screen exists: a quote provider states its price history in *today's*
 * shares, so every close before a split has already been divided by the ratio,
 * while the ledger counts the shares that existed on each day. Multiplying the
 * two values a past holding at a fraction of the truth. The provider supplies
 * most ratios automatically; this is the fallback for the ones it does not
 * publish — B3 bonuses, thinly traded funds, or a provider that has no split
 * feed at all.
 *
 * The AI fills the form rather than filing a proposal for review: the broad
 * sweep is already automatic, so what is left is "I know the ticker, tell me
 * its events" — and the user still presses the button that writes the row.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Plus, Split, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useToast } from "@/components/Toast";
import { Badge, Card, DateField, ErrorState, SectionTitle, Skeleton } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { SplitCandidate } from "@/lib/api";
import { shortDate } from "@/lib/format";

/** "6" reads as nothing on its own; "1 vira 6" is the event. */
function ratioLabel(ratio: number): string {
  if (ratio > 1) return `1 cota vira ${Number(ratio.toFixed(6))}`;
  return `${Number((1 / ratio).toFixed(6))} cotas viram 1`;
}

function AiLookup({ onPick }: { onPick: (candidate: SplitCandidate) => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [ticker, setTicker] = useState("");
  const [found, setFound] = useState<SplitCandidate[] | null>(null);
  const processedJobRef = useRef<string | null>(null);

  const jobQ = useQuery({
    queryKey: ["split-lookup"],
    queryFn: api.splitLookup,
    refetchInterval: (query) => (query.state.data?.active ? 2_000 : false),
  });
  const job = jobQ.data;
  const running = Boolean(job?.active);

  // The answer arrives on the job, not on the mutation: a web search takes
  // longer than a request should, so it runs in the background and this polls.
  useEffect(() => {
    if (!job || job.active || !job.id) return;
    if (processedJobRef.current === job.id) return;
    processedJobRef.current = job.id;
    const result = job.result as { splits?: SplitCandidate[] } | undefined;
    if (result?.splits) setFound(result.splits);
  }, [job]);

  const start = useMutation({
    mutationFn: () => api.startSplitLookup(ticker.trim().toUpperCase()),
    onSuccess: () => {
      setFound(null);
      queryClient.invalidateQueries({ queryKey: ["split-lookup"] });
    },
    onError: (error) =>
      toast.error(
        "Não foi possível iniciar a busca.",
        error instanceof ApiError ? error : undefined,
      ),
  });

  return (
    <div className="rounded-xl border border-line bg-surface-raised p-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Buscar com IA</span>
          <input
            className="input w-40 uppercase"
            value={ticker}
            placeholder="VOOG"
            onChange={(event) => setTicker(event.target.value)}
          />
        </label>
        <button
          type="button"
          className="btn-ghost"
          disabled={running || start.isPending || ticker.trim().length < 2}
          onClick={() => start.mutate()}
        >
          <Bot size={15} className={running ? "animate-pulse" : undefined} aria-hidden />
          {running ? "Pesquisando…" : "Procurar desdobramentos"}
        </button>
        {job?.status && running ? <span className="text-xs text-ink-muted">{job.status}</span> : null}
        {job?.error && !running ? <span className="text-xs text-negative">{job.error}</span> : null}
      </div>

      {found?.length ? (
        <ul className="mt-3 space-y-2">
          {found.map((candidate) => (
            <li
              key={candidate.date}
              className="flex flex-wrap items-center gap-3 rounded-xl border border-line px-3 py-2.5 text-sm"
            >
              <span className="font-medium">{shortDate(candidate.date)}</span>
              <Badge tone="accent">{ratioLabel(Number(candidate.ratio))}</Badge>
              <span className="min-w-0 flex-1 text-xs text-ink-muted">
                {candidate.rationale ?? candidate.event_type ?? "—"}
                {candidate.source ? ` · ${candidate.source}` : ""}
              </span>
              <button type="button" className="btn-ghost" onClick={() => onPick(candidate)}>
                <Plus size={14} aria-hidden /> Usar
              </button>
            </li>
          ))}
        </ul>
      ) : found ? (
        <p className="mt-3 text-xs text-ink-muted">
          Nada encontrado além do que já está registrado. Uma lista vazia é uma resposta melhor
          que um palpite — se você souber do evento, preencha à mão abaixo.
        </p>
      ) : null}
      <p className="mt-3 text-xs text-ink-muted">
        A IA só preenche o formulário: nada é gravado sem você confirmar. Confira a razão antes
        de salvar — ela reescreve todo o valor histórico anterior àquela data.
      </p>
    </div>
  );
}

export default function AssetSplits() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [ticker, setTicker] = useState("");
  const [when, setWhen] = useState("");
  const [ratio, setRatio] = useState("");

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["splits"],
    queryFn: api.splits,
  });

  const create = useMutation({
    mutationFn: api.createSplit,
    onSuccess: (_row, payload) => {
      // Every historical value before that date is restated: refetch the lot.
      queryClient.invalidateQueries();
      setTicker("");
      setWhen("");
      setRatio("");
      toast.success(`Desdobramento de ${payload.ticker} registrado.`, {
        description: "O histórico anterior à data foi recalculado.",
      });
    },
    onError: (error) => toast.error("Não foi possível registrar o desdobramento.", error),
  });
  const remove = useMutation({
    mutationFn: api.deleteSplit,
    onSuccess: () => {
      queryClient.invalidateQueries();
      toast.success("Desdobramento removido.");
    },
    onError: (error) => toast.error("Não foi possível remover.", error),
  });

  const canSubmit = ticker.trim().length >= 2 && when && Number(ratio) > 0 && Number(ratio) !== 1;

  return (
    <Card className="p-5">
      <SectionTitle
        title="Desdobramentos e grupamentos"
        subtitle="O provedor de cotações reescreve o histórico de preços nesses eventos; declarar aqui o que ele não publica"
      />

      {isError ? (
        <ErrorState error={error} retry={() => refetch()} />
      ) : isLoading || !data ? (
        <Skeleton className="h-24 w-full" />
      ) : (
        <div className="space-y-5">
          {data.length ? (
            /* Capped and scrolled: a portfolio of B3 papers accumulates dozens
               of these, and an uncapped table would push the form that adds a
               missing one several screens below the fold. */
            <div className="max-h-[22rem] overflow-auto rounded-xl border border-line px-3">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-ink-muted">
                    <th className="py-2 pr-3 font-medium">Ativo</th>
                    <th className="py-2 pr-3 font-medium">Data</th>
                    <th className="py-2 pr-3 font-medium">Proporção</th>
                    <th className="py-2 pr-3 font-medium">Origem</th>
                    <th className="py-2" />
                  </tr>
                </thead>
                <tbody>
                  {data.map((split) => (
                    <tr key={split.id} className="border-b border-line last:border-b-0">
                      <td className="py-2.5 pr-3 font-medium">{split.ticker}</td>
                      <td className="py-2.5 pr-3 tnum">{shortDate(split.date)}</td>
                      <td className="py-2.5 pr-3">
                        <span className={split.ignored_reason ? "text-ink-muted line-through" : undefined}>
                          {ratioLabel(Number(split.ratio))}
                        </span>
                        {/* Not applied, and saying why: dropping it in silence
                            would be the same fault as applying it in silence. */}
                        {split.ignored_reason ? (
                          <span className="mt-1 block text-[11px] leading-relaxed text-warning">
                            Não aplicado. {split.ignored_reason}{" "}
                            {/* The way out, said where the problem is: declaring
                                the same date by hand replaces the provider's
                                row, and a hand-typed ratio is never questioned. */}
                            <button
                              type="button"
                              className="underline underline-offset-2 hover:text-ink"
                              disabled={create.isPending}
                              onClick={() =>
                                create.mutate({
                                  ticker: split.ticker,
                                  date: split.date,
                                  ratio: String(split.ratio),
                                })
                              }
                            >
                              Aplicar mesmo assim
                            </button>
                          </span>
                        ) : null}
                      </td>
                      <td className="py-2.5 pr-3">
                        <Badge tone={split.ignored_reason ? "warning" : split.editable ? "accent" : "neutral"}>
                          {split.editable ? "declarado" : split.source}
                        </Badge>
                      </td>
                      <td className="py-2.5 text-right">
                        {split.editable ? (
                          <button
                            type="button"
                            className="btn-ghost px-2 py-1"
                            aria-label={`Remover desdobramento de ${split.ticker}`}
                            disabled={remove.isPending}
                            onClick={() => remove.mutate(split.id)}
                          >
                            <Trash2 size={14} aria-hidden />
                          </button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="flex items-center gap-2 text-sm text-ink-muted">
              <Split size={15} aria-hidden />
              Nenhum desdobramento conhecido nos seus ativos.
            </p>
          )}

          <AiLookup
            onPick={(candidate) => {
              setWhen(candidate.date);
              setRatio(candidate.ratio);
            }}
          />

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 lg:items-end">
            <label className="block">
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">Ativo</span>
              <input
                className="input uppercase"
                value={ticker}
                placeholder="VOOG"
                onChange={(event) => setTicker(event.target.value)}
              />
            </label>
            <div>
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">Data ex</span>
              <DateField ariaLabel="Data do desdobramento" className="w-full" value={when} onChange={setWhen} />
            </div>
            <label className="block">
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">
                Cotas depois por cota antes
              </span>
              <input
                className="input tnum"
                inputMode="decimal"
                value={ratio}
                placeholder="6"
                onChange={(event) => setRatio(event.target.value)}
              />
            </label>
            <button
              type="button"
              className="btn-primary"
              disabled={!canSubmit || create.isPending}
              onClick={() =>
                create.mutate({
                  ticker: ticker.trim().toUpperCase(),
                  date: when,
                  ratio: ratio.trim(),
                })
              }
            >
              <Plus size={15} aria-hidden /> Registrar
            </button>
          </div>
          <p className="text-xs text-ink-muted">
            A proporção é quantas cotas você passou a ter para cada uma que tinha: desdobramento
            1:6 é <strong>6</strong>, grupamento 10:1 é <strong>0,1</strong>, bonificação de 5% é{" "}
            <strong>1,05</strong>. Use a data ex — o dia em que o preço na bolsa passou a refletir
            o evento.
          </p>
        </div>
      )}
    </Card>
  );
}
