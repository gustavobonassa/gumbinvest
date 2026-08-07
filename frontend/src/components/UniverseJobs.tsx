/**
 * Universo de ativos → Sincronização: start the download, watch it, clear it.
 *
 * The universe is empty on a fresh install and stays empty until someone asks
 * for it — this tab is that ask, and the only place the work is visible. Each
 * stage is one job: what it is doing, how far along, how long it took last
 * time.
 *
 * The remaining-time estimate comes from the backend and is null until a stage
 * has actually been measured. It is rendered as "—" in that case rather than
 * as a guess: a countdown invented before the work has ever run once is
 * decoration, and this one is watched precisely by people deciding whether to
 * wait.
 */
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  Clock,
  Download,
  Loader2,
  MinusCircle,
  Play,
  Trash2,
  X,
} from "lucide-react";

import { api, type UniverseJob, type UniverseStatus } from "@/lib/api";
import { Badge, Card, EmptyState, Modal, SectionTitle, Skeleton } from "@/components/ui";
import { useToast } from "@/components/Toast";

/** Seconds as "1 min 20 s" — the scale these jobs actually run at. */
function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
}

function when(value: string | null): string {
  if (!value) return "-";
  return new Date(value).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

const STATUS: Record<UniverseJob["status"], { label: string; icon: typeof Check; className: string }> = {
  pending: { label: "Na fila", icon: Clock, className: "text-ink-muted" },
  running: { label: "Em andamento", icon: Loader2, className: "text-accent" },
  done: { label: "Concluída", icon: Check, className: "text-positive" },
  skipped: { label: "Ignorada", icon: MinusCircle, className: "text-ink-muted" },
  failed: { label: "Falhou", icon: AlertTriangle, className: "text-negative" },
};

const RUN_STATE: Record<string, { label: string; tone: "positive" | "negative" | "warning" | "neutral" }> = {
  done: { label: "Concluída", tone: "positive" },
  error: { label: "Falhou", tone: "negative" },
  cancelled: { label: "Cancelada", tone: "warning" },
  paused: { label: "Pausada", tone: "warning" },
  idle: { label: "Nunca executada", tone: "neutral" },
  running: { label: "Em andamento", tone: "neutral" },
};

function ProgressBar({ value, muted }: { value: number; muted?: boolean }) {
  return (
    <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-surface-hover">
      <div
        className={`h-full rounded-full transition-[width] duration-500 ${muted ? "bg-ink-muted" : "bg-accent"}`}
        style={{ width: `${Math.min(Math.max(value, 0), 100)}%` }}
      />
    </div>
  );
}

function JobRow({ job }: { job: UniverseJob }) {
  const meta = STATUS[job.status];
  const Icon = meta.icon;
  const pct =
    job.status === "running" && job.total && job.total > 0
      ? ((job.processed ?? 0) / job.total) * 100
      : null;

  return (
    <li className="flex gap-3 px-4 py-3">
      <Icon
        size={16}
        className={`mt-0.5 shrink-0 ${meta.className} ${job.status === "running" ? "animate-spin" : ""}`}
        aria-hidden
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3">
          <span className="text-sm font-medium">{job.label}</span>
          <span className={`text-xs ${meta.className}`}>{meta.label}</span>
        </div>

        {job.status === "running" ? (
          <>
            <p className="mt-0.5 text-xs text-ink-secondary">{job.message ?? "Trabalhando…"}</p>
            {pct !== null ? (
              <>
                <ProgressBar value={pct} />
                <p className="tnum mt-1 text-[11px] text-ink-muted">
                  {(job.processed ?? 0).toLocaleString("pt-BR")} de{" "}
                  {(job.total ?? 0).toLocaleString("pt-BR")}
                  {job.expected_seconds ? ` · normalmente ${duration(job.expected_seconds)}` : ""}
                </p>
              </>
            ) : (
              <ProgressBar value={100} muted />
            )}
          </>
        ) : (
          <p className="tnum mt-0.5 text-xs text-ink-muted">
            {job.status === "done"
              ? `${job.rows.toLocaleString("pt-BR")} registros · ${duration(job.seconds)}`
              : job.status === "pending"
                ? job.expected_seconds
                  ? `estimativa ${duration(job.expected_seconds)}`
                  : "ainda não executada"
                : duration(job.seconds)}
          </p>
        )}
      </div>
    </li>
  );
}

export default function UniverseJobs() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [confirmClear, setConfirmClear] = useState(false);
  const watched = useRef<string | null>(null);
  const processed = useRef<string | null>(null);

  const statusQ = useQuery({
    queryKey: ["universe-status"],
    queryFn: api.universeStatus,
    refetchInterval: (query) => (query.state.data?.active ? 2_000 : false),
  });
  const status: UniverseStatus | undefined = statusQ.data;
  const running = Boolean(status?.active);

  useEffect(() => {
    if (!status || status.active || !status.run_id) return;
    if (processed.current === status.run_id) return;
    processed.current = status.run_id;
    if (watched.current !== status.run_id) return;
    queryClient.invalidateQueries({ queryKey: ["universe"] });
    queryClient.invalidateQueries({ queryKey: ["portfolio-fit"] });
    if (status.state === "done") toast.success(status.message ?? "Universo atualizado.");
    else if (status.state === "cancelled") toast.info("Atualização cancelada.");
    else
      toast.error("A atualização do universo não terminou.", undefined, {
        description: status.message ?? undefined,
      });
  }, [status?.run_id, status?.active, status?.state]);

  const start = useMutation({
    mutationFn: () => api.startUniverseIngest(),
    onSuccess: (started) => {
      watched.current = started.run_id;
      processed.current = null;
      queryClient.invalidateQueries({ queryKey: ["universe-status"] });
      toast.info("Sincronização iniciada.", {
        description: "Roda em segundo plano, pode fechar esta página.",
      });
    },
    onError: (error) => toast.error("Não foi possível iniciar a sincronização.", error),
  });

  const cancel = useMutation({
    mutationFn: api.cancelUniverseIngest,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["universe-status"] }),
    onError: (error) => toast.error("Não foi possível cancelar.", error),
  });

  const clear = useMutation({
    mutationFn: api.clearUniverse,
    onSuccess: (result) => {
      setConfirmClear(false);
      queryClient.invalidateQueries({ queryKey: ["universe-status"] });
      queryClient.invalidateQueries({ queryKey: ["universe"] });
      queryClient.invalidateQueries({ queryKey: ["portfolio-fit"] });
      toast.success(`${result.removed.toLocaleString("pt-BR")} ativos removidos.`);
    },
    onError: (error) => toast.error("Não foi possível apagar os dados.", error),
  });

  if (statusQ.isLoading || !status) return <Skeleton className="h-96 w-full" />;

  const enabled = Boolean(status.settings?.enabled);
  const total = status.coverage?.total ?? 0;
  const runState = RUN_STATE[status.state] ?? RUN_STATE.idle;

  return (
    <div className="space-y-6">
      <Card className="p-5">
        <SectionTitle
          title="Sincronização"
          subtitle="Baixa os arquivos públicos da B3 e da CVM e monta a tabela local"
          action={<Badge tone={running ? "accent" : runState.tone}>{running ? "Em andamento" : runState.label}</Badge>}
        />

        {!enabled ? (
          <p className="text-sm text-ink-secondary">
            O universo de ativos está desativado. Ative-o no cartão acima para poder sincronizar.
          </p>
        ) : (
          <>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className="btn-primary"
                onClick={() => start.mutate()}
                disabled={running || start.isPending}
              >
                {running ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
                {running ? "Sincronizando…" : total ? "Sincronizar novamente" : "Iniciar sincronização"}
              </button>
              {running ? (
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => cancel.mutate()}
                  disabled={cancel.isPending}
                >
                  <X size={15} /> Cancelar
                </button>
              ) : null}
              {total > 0 && !running ? (
                <button
                  type="button"
                  className="btn-ghost text-negative"
                  onClick={() => setConfirmClear(true)}
                >
                  <Trash2 size={15} /> Apagar dados
                </button>
              ) : null}
            </div>

            <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
              <div>
                <dt className="text-xs text-ink-muted">Ativos</dt>
                <dd className="tnum">{total.toLocaleString("pt-BR")}</dd>
              </div>
              <div>
                <dt className="text-xs text-ink-muted">Com fundamentos</dt>
                <dd className="tnum">
                  {(status.coverage?.with_fundamentals ?? 0).toLocaleString("pt-BR")}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-ink-muted">{running ? "Decorrido" : "Última duração"}</dt>
                <dd className="tnum">{duration(status.elapsed_seconds)}</dd>
              </div>
              <div>
                <dt className="text-xs text-ink-muted">Tempo restante</dt>
                <dd className="tnum">
                  {running ? duration(status.eta_seconds) : "-"}
                  {running && status.eta_seconds === null ? (
                    <span className="ml-1 text-[11px] text-ink-muted">(medindo)</span>
                  ) : null}
                </dd>
              </div>
            </dl>

            {status.stale && !running ? (
              <p className="mt-3 text-sm text-warning">
                A última sincronização foi interrompida. Iniciar de novo retoma a partir da etapa
                seguinte à última concluída.
              </p>
            ) : null}

            {status.warnings?.length ? (
              <ul className="mt-3 space-y-1 text-xs text-warning">
                {status.warnings.map((warning) => (
                  <li key={warning}>• {warning}</li>
                ))}
              </ul>
            ) : null}
          </>
        )}
      </Card>

      {enabled ? (
        <Card className="overflow-hidden p-0">
          <div className="p-5 pb-2">
            <SectionTitle
              title="Etapas"
              subtitle="Cada etapa lê uma fonte pública diferente e preenche colunas distintas"
            />
          </div>
          <ul className="divide-y divide-line">
            {status.jobs?.map((job) => <JobRow key={job.name} job={job} />)}
          </ul>
        </Card>
      ) : null}

      {status.history?.length ? (
        <Card className="overflow-hidden p-0">
          <div className="p-5 pb-2">
            <SectionTitle title="Execuções anteriores" subtitle="As últimas sincronizações" />
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm">
              <thead>
                <tr className="border-b border-line text-xs text-ink-muted">
                  <th className="px-4 py-2 text-left font-medium">Início</th>
                  <th className="px-4 py-2 text-left font-medium">Resultado</th>
                  <th className="px-4 py-2 text-right font-medium">Registros</th>
                  <th className="px-4 py-2 text-right font-medium">Duração</th>
                </tr>
              </thead>
              <tbody>
                {status.history.map((run) => {
                  const meta = RUN_STATE[run.state] ?? RUN_STATE.idle;
                  return (
                    <tr key={run.run_id ?? run.started_at} className="table-row">
                      <td className="px-4 py-2">{when(run.started_at)}</td>
                      <td className="px-4 py-2">
                        <Badge tone={meta.tone}>{meta.label}</Badge>
                        {run.warnings?.length ? (
                          <span className="ml-2 text-xs text-warning">
                            {run.warnings.length} aviso(s)
                          </span>
                        ) : null}
                      </td>
                      <td className="tnum px-4 py-2 text-right">
                        {run.rows.toLocaleString("pt-BR")}
                      </td>
                      <td className="tnum px-4 py-2 text-right">{duration(run.seconds)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      ) : enabled && total === 0 && !running ? (
        <Card className="p-5">
          <EmptyState
            icon={Download}
            title="Nenhuma sincronização ainda"
            description="O universo começa vazio. A primeira sincronização baixa cerca de 130 MB da B3 e da CVM e leva poucos minutos."
          />
        </Card>
      ) : null}

      <Modal
        open={confirmClear}
        onClose={() => setConfirmClear(false)}
        title="Apagar o universo de ativos?"
        subtitle="Remove os dados públicos baixados. Nada da sua carteira é afetado."
      >
        <p className="text-sm text-ink-secondary">
          Serão apagados {total.toLocaleString("pt-BR")} ativos com preços, fundamentos e
          indicadores, tudo baixado da B3 e da CVM. Suas posições, movimentações, proventos e
          configurações não são tocados, e o screener volta a ficar vazio até a próxima
          sincronização.
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={() => setConfirmClear(false)}>
            Cancelar
          </button>
          <button
            type="button"
            className="btn-primary bg-negative hover:bg-negative"
            onClick={() => clear.mutate()}
            disabled={clear.isPending}
          >
            <Trash2 size={15} /> {clear.isPending ? "Apagando…" : "Apagar dados"}
          </button>
        </div>
      </Modal>
    </div>
  );
}
