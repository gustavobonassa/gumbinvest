/**
 * Configurações → Automações: the collectors that keep the portfolio current
 * without manual imports.
 *
 * One card per pipeline (credentials, run button, live narration), one shared
 * history table. The list itself is the poll: while any run is live it
 * refetches every 2 s, and quietly every 15 s otherwise — quietly on purpose,
 * because a *scheduled* run can park on a 2FA challenge with nobody watching,
 * and whoever has this tab open should get the modal without having started
 * anything.
 *
 * The code modal is the human half of the backend's `request_input`: the run
 * sits on `waiting_input` until the code typed here reaches it. Closing the
 * modal does not cancel the run — the card keeps a "Informar código" button
 * while the wait lasts, and the wait itself times out server-side.
 */
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Loader2, Play, Workflow, X } from "lucide-react";

import { api, type PipelineInfo, type PipelineRun } from "@/lib/api";
import { Badge, Card, EmptyState, Modal, Pager, SectionTitle, Skeleton } from "@/components/ui";
import { useToast } from "@/components/Toast";
import SecretField from "@/components/SecretField";

const RUN_STATE: Record<
  PipelineRun["status"],
  { label: string; tone: "positive" | "negative" | "warning" | "neutral" | "accent" }
> = {
  running: { label: "Em andamento", tone: "accent" },
  waiting_input: { label: "Aguardando código", tone: "warning" },
  success: { label: "Concluída", tone: "positive" },
  failed: { label: "Falhou", tone: "negative" },
  cancelled: { label: "Cancelada", tone: "warning" },
};

function when(value: string | null): string {
  if (!value) return "-";
  return new Date(value).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

/** Elapsed between start and finish as "1 min 20 s" — the scale these runs live at. */
function duration(run: PipelineRun): string {
  if (!run.started_at || !run.finished_at) return "-";
  const seconds = (new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
}

function resultSentence(run: PipelineRun): string {
  if (run.status === "failed") return run.error ?? "Falhou.";
  const imported = run.result?.rows_imported;
  if (typeof imported !== "number") return "-";
  const duplicate = Number(run.result?.rows_duplicate ?? 0);
  return `${imported.toLocaleString("pt-BR")} novas · ${duplicate.toLocaleString("pt-BR")} repetidas`;
}

function lastLogLine(run: PipelineRun): string {
  const entry = run.log[run.log.length - 1];
  return entry?.message ?? "Trabalhando…";
}

function PipelineCard({
  pipeline,
  onAnswer,
}: {
  pipeline: PipelineInfo;
  onAnswer: (run: PipelineRun) => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [fullHistory, setFullHistory] = useState(false);
  const run = pipeline.active_run;
  const waiting = run?.status === "waiting_input";

  const start = useMutation({
    mutationFn: () => api.startPipeline(pipeline.key, { full_history: fullHistory }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["pipelines"] });
      toast.info(fullHistory ? "Coleta do histórico completo iniciada." : "Coleta iniciada.", {
        description: "Roda em segundo plano, pode fechar esta página.",
      });
    },
    onError: (error) => toast.error("Não foi possível iniciar a coleta.", error),
  });

  const cancel = useMutation({
    mutationFn: (runId: number) => api.cancelPipelineRun(runId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["pipelines"] }),
    onError: (error) => toast.error("Não foi possível cancelar.", error),
  });

  return (
    <Card className="p-5">
      <SectionTitle
        title={pipeline.name}
        subtitle={pipeline.schedule}
        action={
          run ? (
            <Badge tone={RUN_STATE[run.status].tone}>{RUN_STATE[run.status].label}</Badge>
          ) : (
            <Badge tone={pipeline.configured ? "positive" : "warning"}>
              {pipeline.configured ? "configurada" : "credenciais pendentes"}
            </Badge>
          )
        }
      />
      <p className="text-sm text-ink-secondary">{pipeline.description}</p>

      <div className="mt-4 space-y-4">
        {pipeline.credentials.map((credential) => (
          <SecretField
            key={credential.key}
            label={credential.label}
            hint=""
            placeholder={credential.label}
            configured={credential.configured}
            settingKey={credential.key}
          />
        ))}
      </div>

      {!run ? (
        <label className="mt-4 flex cursor-pointer items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={fullHistory}
            onChange={(event) => setFullHistory(event.target.checked)}
          />
          <span>
            Baixar histórico completo
            <span className="mt-0.5 block text-xs text-ink-muted">
              Traz vários anos de uma vez, para a primeira carga. Depois, deixe desmarcado — as
              coletas seguintes só buscam o que é novo.
            </span>
          </span>
        </label>
      ) : null}

      <div className="mt-4 flex flex-wrap gap-2">
        <button
          type="button"
          className="btn-primary"
          onClick={() => start.mutate()}
          disabled={Boolean(run) || start.isPending || !pipeline.configured}
        >
          {run ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
          {run ? "Coletando…" : fullHistory ? "Baixar histórico completo" : "Executar agora"}
        </button>
        {waiting && run ? (
          <button type="button" className="btn-primary" onClick={() => onAnswer(run)}>
            <KeyRound size={15} /> Informar código
          </button>
        ) : null}
        {run ? (
          <button
            type="button"
            className="btn-ghost"
            onClick={() => cancel.mutate(run.id)}
            disabled={cancel.isPending}
          >
            <X size={15} /> Cancelar
          </button>
        ) : null}
      </div>

      {run ? (
        <p className="mt-3 flex items-center gap-2 text-sm text-ink-secondary">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" />
          {lastLogLine(run)}
        </p>
      ) : pipeline.last_run?.status === "failed" ? (
        <p className="mt-3 text-sm text-negative">{pipeline.last_run.error}</p>
      ) : null}
    </Card>
  );
}

export default function Pipelines() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [answering, setAnswering] = useState<PipelineRun | null>(null);
  const [code, setCode] = useState("");
  // Which finished run each pipeline was last toasted about, so a result that
  // predates the page mount stays silent.
  const processed = useRef<Record<string, number>>({});
  const seeded = useRef(false);
  // The code request we have already auto-opened the modal for. Keyed by run +
  // request time so we open once per prompt — a poll that still reports
  // `waiting_input` right after the code is sent does not flash it back open,
  // and a genuinely new prompt (a second 2FA during a re-login) still does.
  const handledRequest = useRef<string | null>(null);

  const pipelinesQ = useQuery({
    queryKey: ["pipelines"],
    queryFn: api.pipelines,
    refetchInterval: (query) =>
      query.state.data?.pipelines.some((item) => item.active_run) ? 2_000 : 15_000,
  });
  const [runsPage, setRunsPage] = useState(1);
  const runsQ = useQuery({
    queryKey: ["pipeline-runs", runsPage],
    queryFn: () => api.pipelineRuns(runsPage),
  });

  const pipelines = pipelinesQ.data?.pipelines;

  useEffect(() => {
    if (!pipelines) return;
    if (!seeded.current) {
      // First payload: record what already happened without announcing it.
      seeded.current = true;
      for (const item of pipelines) {
        if (item.last_run) processed.current[item.key] = item.last_run.id;
      }
      return;
    }
    for (const item of pipelines) {
      const last = item.last_run;
      if (!last || processed.current[item.key] === last.id) continue;
      processed.current[item.key] = last.id;
      queryClient.invalidateQueries({ queryKey: ["pipeline-runs"] });
      if (last.status === "success") {
        // New movements change positions everywhere, not just here.
        queryClient.invalidateQueries();
        toast.success(`${item.name}: coleta concluída.`, { description: resultSentence(last) });
      } else if (last.status === "cancelled") {
        toast.info(`${item.name}: coleta cancelada.`);
      } else if (last.status === "failed") {
        toast.error(`${item.name}: a coleta falhou.`, undefined, {
          description: last.error ?? undefined,
        });
      }
    }
  }, [pipelines, queryClient, toast]);

  // A run parked on a code opens the modal by itself — including a scheduled
  // run nobody started from this tab.
  const parked = pipelines
    ?.map((item) => item.active_run)
    .find((run): run is PipelineRun => run?.status === "waiting_input");
  const parkedKey = parked ? `${parked.id}:${parked.input_request?.requested_at ?? ""}` : null;
  useEffect(() => {
    if (parked && parkedKey && handledRequest.current !== parkedKey) {
      // First sighting of this prompt: open once and remember it, so a later
      // poll (or the user closing the modal) never reopens it.
      handledRequest.current = parkedKey;
      setAnswering(parked);
      setCode("");
    }
    // The run left the waiting state (answered, timed out, cancelled): close.
    if (!parked && answering) setAnswering(null);
  }, [parkedKey, parked, answering]);

  const answer = useMutation({
    mutationFn: ({ runId, value }: { runId: number; value: string }) =>
      api.answerPipelineRun(runId, value),
    onSuccess: () => {
      setAnswering(null);
      setCode("");
      queryClient.invalidateQueries({ queryKey: ["pipelines"] });
      toast.success("Código enviado.", { description: "A coleta continua em segundo plano." });
    },
    onError: (error) => toast.error("Não foi possível enviar o código.", error),
  });

  if (pipelinesQ.isLoading || !pipelines) return <Skeleton className="h-80 w-full" />;

  const runs = runsQ.data?.runs ?? [];
  const names = Object.fromEntries(pipelines.map((item) => [item.key, item.name]));

  return (
    <div className="space-y-6">
      {pipelines.map((pipeline) => (
        <PipelineCard key={pipeline.key} pipeline={pipeline} onAnswer={setAnswering} />
      ))}

      <p className="text-xs text-ink-muted">
        As credenciais ficam no banco de dados local, nunca aparecem de volta nesta tela e não
        saem no export .gumbinvest. Tudo que uma coleta baixa passa pelo mesmo importador dos
        arquivos enviados à mão — repetir uma coleta nunca duplica movimentações.
      </p>

      {runs.length ? (
        <Card className="overflow-hidden p-0">
          <div className="p-5 pb-2">
            <SectionTitle title="Coletas anteriores" subtitle="As últimas execuções, de todas as automações" />
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm">
              <thead>
                <tr className="border-b border-line text-xs text-ink-muted">
                  <th className="px-4 py-2 text-left font-medium">Início</th>
                  <th className="px-4 py-2 text-left font-medium">Automação</th>
                  <th className="hidden px-4 py-2 text-left font-medium sm:table-cell">Origem</th>
                  <th className="px-4 py-2 text-left font-medium">Resultado</th>
                  <th className="hidden px-4 py-2 text-right font-medium md:table-cell">Duração</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const meta = RUN_STATE[run.status];
                  return (
                    <tr key={run.id} className="table-row">
                      <td className="px-4 py-2">{when(run.started_at)}</td>
                      <td className="px-4 py-2">{names[run.pipeline] ?? run.pipeline}</td>
                      <td className="hidden px-4 py-2 text-ink-muted sm:table-cell">
                        {run.trigger === "scheduled" ? "Agendada" : "Manual"}
                        {run.options?.full_history ? " · completo" : ""}
                      </td>
                      <td className="px-4 py-2">
                        <Badge tone={meta.tone}>{meta.label}</Badge>
                        <span className="ml-2 text-xs text-ink-muted">{resultSentence(run)}</span>
                      </td>
                      <td className="tnum hidden px-4 py-2 text-right md:table-cell">
                        {duration(run)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {runsQ.data && runsQ.data.pages > 1 ? (
            <div className="p-4">
              <Pager
                page={runsQ.data.page}
                pages={runsQ.data.pages}
                total={runsQ.data.total}
                pageSize={runsQ.data.page_size}
                onChange={setRunsPage}
                noun="coletas"
              />
            </div>
          ) : null}
        </Card>
      ) : (
        <Card className="p-5">
          <EmptyState
            icon={Workflow}
            title="Nenhuma coleta ainda"
            description="Preencha as credenciais e execute a primeira coleta — depois disso ela roda sozinha toda semana."
          />
        </Card>
      )}

      <Modal
        open={Boolean(answering)}
        onClose={() => setAnswering(null)}
        title="Código de verificação"
        subtitle={answering?.input_request?.prompt ?? "A automação está esperando um código."}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (answering && code.trim()) answer.mutate({ runId: answering.id, value: code });
          }}
        >
          <label className="block text-sm text-ink-secondary">
            Código
            <input
              autoFocus
              inputMode="numeric"
              autoComplete="one-time-code"
              className="input tnum mt-1 w-full"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              placeholder="000000"
            />
          </label>
          <p className="text-xs text-ink-muted">
            Fechar esta janela não cancela a coleta — ela continua esperando por alguns minutos e
            o botão "Informar código" reabre aqui.
          </p>
          <div className="flex justify-end gap-2">
            <button type="button" className="btn-ghost" onClick={() => setAnswering(null)}>
              Fechar
            </button>
            <button type="submit" className="btn-primary" disabled={!code.trim() || answer.isPending}>
              {answer.isPending ? "Enviando…" : "Enviar código"}
            </button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
