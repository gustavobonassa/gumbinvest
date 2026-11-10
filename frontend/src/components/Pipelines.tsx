/**
 * Configurações → Automações: the collectors that keep the portfolio current
 * without manual imports.
 *
 * One tile per pipeline (the service's logo and its live state) acting as a
 * tab: the selected one shows its card (credentials, run button, live
 * narration) and its own run history. The selection lives in the URL
 * (`servico`), like every other tab. The list itself is the poll: while any run is live it
 * refetches every 2 s, and quietly every 15 s otherwise — quietly on purpose,
 * because a *scheduled* run can park on a 2FA challenge with nobody watching,
 * and whoever has this tab open should get the modal without having started
 * anything.
 *
 * The schedule line under each name opens the schedule modal ("Alterar"):
 * off by default, otherwise the weekdays and time it runs on its own.
 *
 * Every run keeps a timestamped log ("Ver registro", on the card and on each
 * row of the history): the narration while it works, and — when it dies on
 * something unexpected — the error and where it happened, which is what the
 * generic failure message points the user to.
 *
 * The code modal is the human half of the backend's `request_input`: the run
 * sits on `waiting_input` until the code typed here reaches it. Closing the
 * modal does not cancel the run — the card keeps a "Informar código" button
 * while the wait lasts, and the wait itself times out server-side.
 */
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  CalendarClock,
  Copy,
  FlaskConical,
  KeyRound,
  Loader2,
  Play,
  ScrollText,
  Workflow,
  X,
} from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { api, type PipelineInfo, type PipelineRun } from "@/lib/api";
import { Badge, Card, EmptyState, Modal, Pager, SectionTitle, Skeleton } from "@/components/ui";
import { useToast } from "@/components/Toast";
import SecretField from "@/components/SecretField";
import ServiceLogo from "@/components/ServiceLogo";
import PipelineScheduleModal from "@/components/PipelineSchedule";
import { scheduleLabel } from "@/lib/schedule";

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
  if (run.result?.preview) {
    const would = Number(run.result.would_import ?? 0).toLocaleString("pt-BR");
    const inside = Number(run.result.inside_covered ?? 0);
    return inside
      ? `Simulação: ${would} seriam adicionadas, ${inside.toLocaleString("pt-BR")} no período já importado`
      : `Simulação: ${would} seriam adicionadas, nenhuma no período já importado`;
  }
  const imported = run.result?.rows_imported;
  if (typeof imported !== "number") return "-";
  const duplicate = Number(run.result?.rows_duplicate ?? 0);
  return `${imported.toLocaleString("pt-BR")} novas · ${duplicate.toLocaleString("pt-BR")} repetidas`;
}

/** The one line a tile has room for: what is happening, or what it needs. */
function tileStatus(pipeline: PipelineInfo): {
  label: string;
  tone: "positive" | "negative" | "warning" | "neutral" | "accent";
  live?: boolean;
} {
  const run = pipeline.active_run;
  if (run) return { ...RUN_STATE[run.status], live: true };
  if (!pipeline.configured) return { label: "Credenciais pendentes", tone: "warning" };
  const last = pipeline.last_run;
  if (!last) return { label: "Configurada · nunca executada", tone: "neutral" };
  const day = last.finished_at ?? last.started_at;
  const date = day ? new Date(day).toLocaleDateString("pt-BR") : "";
  if (last.status === "failed") return { label: `Falhou em ${date}`, tone: "negative" };
  return { label: `Última coleta ${date}`, tone: "positive" };
}

const TONE_DOT: Record<string, string> = {
  positive: "bg-positive",
  negative: "bg-negative",
  warning: "bg-warning",
  accent: "bg-accent",
  neutral: "bg-ink-muted",
};

/** The service picker: one tile per automation, behaving as a tab list. */
function ServiceTabs({
  pipelines,
  selected,
  onSelect,
}: {
  pipelines: PipelineInfo[];
  selected: string;
  onSelect: (key: string) => void;
}) {
  // Arrow keys move between tiles, as in any tab list.
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    event.preventDefault();
    const index = pipelines.findIndex((item) => item.key === selected);
    const step = event.key === "ArrowRight" ? 1 : -1;
    const next = pipelines[(index + step + pipelines.length) % pipelines.length];
    onSelect(next.key);
    document.getElementById(`service-tab-${next.key}`)?.focus();
  };

  return (
    <div
      role="tablist"
      aria-label="Automações"
      className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"
      onKeyDown={onKeyDown}
    >
      {pipelines.map((pipeline) => {
        const active = pipeline.key === selected;
        const status = tileStatus(pipeline);
        return (
          <button
            key={pipeline.key}
            id={`service-tab-${pipeline.key}`}
            type="button"
            role="tab"
            aria-selected={active}
            aria-controls={`service-panel-${pipeline.key}`}
            tabIndex={active ? 0 : -1}
            onClick={() => onSelect(pipeline.key)}
            className={clsx(
              "flex min-w-0 items-center gap-3 rounded-2xl border p-4 text-left transition-colors",
              active
                ? "border-accent/60 bg-accent-soft/40 ring-1 ring-accent/30"
                : "border-line bg-surface hover:border-line-strong hover:bg-surface-hover",
            )}
          >
            <ServiceLogo service={pipeline.key} />
            <span className="min-w-0">
              <span className="block truncate font-medium text-ink">{pipeline.name}</span>
              <span className="mt-0.5 flex items-center gap-1.5 text-xs text-ink-muted">
                <span
                  className={clsx(
                    "inline-block h-1.5 w-1.5 shrink-0 rounded-full",
                    TONE_DOT[status.tone],
                    status.live && "animate-pulse",
                  )}
                  aria-hidden
                />
                <span className="truncate">{status.label}</span>
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function clock(value: string): string {
  return new Date(value).toLocaleTimeString("pt-BR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

const LEVEL_LABEL: Record<PipelineRun["log"][number]["level"], string> = {
  info: "",
  warning: "aviso",
  error: "erro",
};

/** The log as plain text — what "Copiar registro" puts on the clipboard, for
 *  pasting into a message when asking for help. */
function logText(run: PipelineRun, name: string): string {
  const lines = [
    `${name} · execução #${run.id}`,
    `Início: ${when(run.started_at)} · ${RUN_STATE[run.status].label} · ${
      run.trigger === "scheduled" ? "Agendada" : "Manual"
    }`,
  ];
  if (run.error) lines.push(`Erro: ${run.error}`);
  lines.push("");
  for (const entry of run.log) {
    const level = LEVEL_LABEL[entry.level];
    lines.push(`${clock(entry.at)}  ${level ? `[${level}] ` : ""}${entry.message}`);
  }
  return lines.join("\n");
}

/** One run's log. Fed the freshest copy of the run on every poll, so a live
 *  run's narration grows here as it works. */
function RunLogModal({
  run,
  name,
  onClose,
}: {
  run: PipelineRun | null;
  name: string;
  onClose: () => void;
}) {
  const toast = useToast();
  const listRef = useRef<HTMLOListElement>(null);
  const lines = run?.log.length ?? 0;
  // A live run's newest line is the interesting one: keep it in view.
  useLayoutEffect(() => {
    const el = listRef.current;
    if (el && run?.active) el.scrollTop = el.scrollHeight;
  }, [lines, run?.active]);

  const copy = async () => {
    if (!run) return;
    try {
      await navigator.clipboard.writeText(logText(run, name));
      toast.success("Registro copiado.");
    } catch (error) {
      toast.error("Não foi possível copiar o registro.", error);
    }
  };

  return (
    <Modal
      open={Boolean(run)}
      onClose={onClose}
      wide
      title="Registro da execução"
      subtitle={run ? `${name} · ${when(run.started_at)}` : undefined}
    >
      {run ? (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
            <Badge tone={RUN_STATE[run.status].tone}>{RUN_STATE[run.status].label}</Badge>
            <span>{run.trigger === "scheduled" ? "Agendada" : "Manual"}</span>
            {run.options?.preview ? <span>Simulação</span> : null}
            {run.options?.full_history ? <span>Histórico completo</span> : null}
            {run.finished_at ? <span>Duração {duration(run)}</span> : null}
          </div>
          {run.error ? (
            <p className="rounded-lg border border-negative/30 bg-negative/10 px-3 py-2 text-sm text-negative">
              {run.error}
            </p>
          ) : null}
          {lines ? (
            <ol
              ref={listRef}
              aria-label="Linhas do registro"
              className="max-h-[45vh] space-y-1 overflow-y-auto rounded-lg border border-line bg-canvas p-3 font-mono text-xs leading-relaxed"
            >
              {run.log.map((entry, index) => (
                <li
                  key={index}
                  className={clsx(
                    "flex gap-3",
                    entry.level === "error"
                      ? "text-negative"
                      : entry.level === "warning"
                        ? "text-warning"
                        : "text-ink-secondary",
                  )}
                >
                  <time className="shrink-0 text-ink-muted" dateTime={entry.at}>
                    {clock(entry.at)}
                  </time>
                  <span className="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere]">
                    {entry.message}
                  </span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-sm text-ink-muted">Esta execução não registrou nenhuma linha.</p>
          )}
          {run.active ? (
            <p className="flex items-center gap-2 text-xs text-ink-muted">
              <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" />
              Em andamento — o registro se atualiza sozinho.
            </p>
          ) : null}
          <div className="flex flex-wrap justify-end gap-2">
            <button type="button" className="btn-ghost" onClick={copy}>
              <Copy size={15} /> Copiar registro
            </button>
            <button type="button" className="btn-primary" onClick={onClose}>
              Fechar
            </button>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}

function lastLogLine(run: PipelineRun): string {
  const entry = run.log[run.log.length - 1];
  return entry?.message ?? "Trabalhando…";
}

function PipelineCard({
  pipeline,
  onAnswer,
  onShowLog,
}: {
  pipeline: PipelineInfo;
  onAnswer: (run: PipelineRun) => void;
  onShowLog: (run: PipelineRun) => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [fullHistory, setFullHistory] = useState(false);
  const [editingSchedule, setEditingSchedule] = useState(false);
  const run = pipeline.active_run;
  const waiting = run?.status === "waiting_input";

  const start = useMutation({
    mutationFn: (preview: boolean) =>
      api.startPipeline(pipeline.key, { full_history: fullHistory, preview }),
    onSuccess: (_, preview) => {
      queryClient.invalidateQueries({ queryKey: ["pipelines"] });
      toast.info(
        preview
          ? "Simulação iniciada."
          : fullHistory
            ? "Coleta do histórico completo iniciada."
            : "Coleta iniciada.",
        {
          description: preview
            ? "Nada é gravado. O resultado aparece no registro desta automação."
            : "Roda em segundo plano, pode fechar esta página.",
        },
      );
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
        subtitle={
          <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <span>{scheduleLabel(pipeline.schedule)}</span>
            <button
              type="button"
              onClick={() => setEditingSchedule(true)}
              className="inline-flex items-center gap-1 text-accent hover:underline"
              aria-label={`Alterar agendamento de ${pipeline.name}`}
            >
              <CalendarClock size={14} aria-hidden /> Alterar
            </button>
          </span>
        }
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
          onClick={() => start.mutate(false)}
          disabled={Boolean(run) || start.isPending || !pipeline.configured}
        >
          {run ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
          {run
            ? run.options?.preview
              ? "Simulando…"
              : "Coletando…"
            : fullHistory
              ? "Baixar histórico completo"
              : "Executar agora"}
        </button>
        {pipeline.supports_preview && !run ? (
          <button
            type="button"
            className="btn-ghost"
            onClick={() => start.mutate(true)}
            disabled={start.isPending || !pipeline.configured}
            title="Coleta tudo e mostra o que seria importado, sem gravar nada"
          >
            <FlaskConical size={15} /> Simular
          </button>
        ) : null}
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
        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1">
          <p className="flex min-w-0 items-center gap-2 text-sm text-ink-secondary">
            <span className="inline-block h-2 w-2 shrink-0 animate-pulse rounded-full bg-accent" />
            {lastLogLine(run)}
          </p>
          <LogLink run={run} onShowLog={onShowLog} />
        </div>
      ) : pipeline.last_run?.status === "failed" ? (
        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1">
          <p className="text-sm text-negative">{pipeline.last_run.error}</p>
          <LogLink run={pipeline.last_run} onShowLog={onShowLog} />
        </div>
      ) : null}

      {editingSchedule ? (
        <PipelineScheduleModal pipeline={pipeline} onClose={() => setEditingSchedule(false)} />
      ) : null}
    </Card>
  );
}

function LogLink({ run, onShowLog }: { run: PipelineRun; onShowLog: (run: PipelineRun) => void }) {
  return (
    <button
      type="button"
      onClick={() => onShowLog(run)}
      className="inline-flex items-center gap-1.5 text-sm text-accent hover:underline"
    >
      <ScrollText size={14} aria-hidden /> Ver registro
    </button>
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
  // The run whose log is open. Only its id matters: the copy shown is looked
  // up fresh from each poll below, so a live run's log keeps growing.
  const [logRunId, setLogRunId] = useState<number | null>(null);

  const pipelinesQ = useQuery({
    queryKey: ["pipelines"],
    queryFn: api.pipelines,
    refetchInterval: (query) =>
      query.state.data?.pipelines.some((item) => item.active_run) ? 2_000 : 15_000,
  });
  const pipelines = pipelinesQ.data?.pipelines;

  const [params, setParams] = useSearchParams();
  const requested = params.get("servico");
  const selected =
    pipelines?.find((item) => item.key === requested)?.key ?? pipelines?.[0]?.key ?? "";
  const [runsPage, setRunsPage] = useState(1);
  const select = (key: string) => {
    setRunsPage(1);
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.set("servico", key);
        return next;
      },
      { replace: true },
    );
  };

  const runsQ = useQuery({
    queryKey: ["pipeline-runs", selected, runsPage],
    queryFn: () => api.pipelineRuns(runsPage, 10, selected),
    enabled: Boolean(selected),
  });

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
  const current = pipelines.find((item) => item.key === selected);
  const logRun =
    logRunId === null
      ? null
      : ([...pipelines.flatMap((item) => [item.active_run, item.last_run]), ...runs].find(
          (run) => run?.id === logRunId,
        ) ?? null);
  const logPipeline = pipelines.find((item) => item.key === logRun?.pipeline);

  return (
    <div className="space-y-6">
      <ServiceTabs pipelines={pipelines} selected={selected} onSelect={select} />

      {current ? (
        <div
          role="tabpanel"
          id={`service-panel-${current.key}`}
          aria-labelledby={`service-tab-${current.key}`}
        >
          <PipelineCard
            key={current.key}
            pipeline={current}
            onAnswer={setAnswering}
            onShowLog={(run) => setLogRunId(run.id)}
          />
        </div>
      ) : null}

      <p className="text-xs text-ink-muted">
        As credenciais ficam no banco de dados local, nunca aparecem de volta nesta tela e não saem
        no export .gumbinvest. Tudo que uma coleta baixa passa pelo mesmo importador dos arquivos
        enviados à mão — repetir uma coleta nunca duplica movimentações.
      </p>

      {runs.length ? (
        <Card className="overflow-hidden p-0">
          <div className="p-5 pb-2">
            <SectionTitle
              title="Coletas anteriores"
              subtitle={`As últimas execuções da automação ${current?.name ?? ""}`}
            />
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm">
              <thead>
                <tr className="border-b border-line text-xs text-ink-muted">
                  <th className="px-4 py-2 text-left font-medium">Início</th>
                  <th className="hidden px-4 py-2 text-left font-medium sm:table-cell">Origem</th>
                  <th className="px-4 py-2 text-left font-medium">Resultado</th>
                  <th className="hidden px-4 py-2 text-right font-medium md:table-cell">Duração</th>
                  <th className="w-px px-2 py-2">
                    <span className="sr-only">Registro</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const meta = RUN_STATE[run.status];
                  return (
                    <tr key={run.id} className="table-row">
                      <td className="px-4 py-2">{when(run.started_at)}</td>
                      <td className="hidden px-4 py-2 text-ink-muted sm:table-cell">
                        {run.trigger === "scheduled" ? "Agendada" : "Manual"}
                        {run.options?.full_history ? " · completo" : ""}
                        {run.options?.preview ? " · simulação" : ""}
                      </td>
                      <td className="px-4 py-2">
                        <Badge tone={meta.tone}>{meta.label}</Badge>
                        <span className="ml-2 text-xs text-ink-muted">{resultSentence(run)}</span>
                      </td>
                      <td className="tnum hidden px-4 py-2 text-right md:table-cell">
                        {duration(run)}
                      </td>
                      <td className="px-2 py-1 text-right">
                        <button
                          type="button"
                          onClick={() => setLogRunId(run.id)}
                          className="btn-ghost px-2 py-1.5 text-ink-muted hover:text-ink"
                          title="Ver registro"
                          aria-label={`Ver registro da coleta de ${when(run.started_at)}`}
                        >
                          <ScrollText size={15} />
                        </button>
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
            title={`Nenhuma coleta de ${current?.name ?? "automação"} ainda`}
            description="Preencha as credenciais e execute a primeira coleta — depois disso ela roda sozinha toda semana."
          />
        </Card>
      )}

      <RunLogModal
        run={logRun}
        name={logPipeline?.name ?? logRun?.pipeline ?? ""}
        onClose={() => setLogRunId(null)}
      />

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
            Fechar esta janela não cancela a coleta — ela continua esperando por alguns minutos e o
            botão "Informar código" reabre aqui.
          </p>
          <div className="flex justify-end gap-2">
            <button type="button" className="btn-ghost" onClick={() => setAnswering(null)}>
              Fechar
            </button>
            <button
              type="submit"
              className="btn-primary"
              disabled={!code.trim() || answer.isPending}
            >
              {answer.isPending ? "Enviando…" : "Enviar código"}
            </button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
