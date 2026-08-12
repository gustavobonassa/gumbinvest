/**
 * First-run setup: a full-screen wizard shown once, over the shell, when the
 * app opens with no data — in the spirit of an OS out-of-box experience.
 * Every step is optional: each can be skipped, and the whole thing can be
 * dismissed from any screen.
 *
 * Nothing here is new machinery. The name and goals are AppSettings that the
 * AI prompts read (services/user_profile.py); the B3 step drives the same
 * pipeline as Importar → Automações; the file step posts to the same
 * importer as /importar. Finishing (or skipping) writes `onboarding_completed`
 * — the wizard never comes back, and everything it touches remains editable
 * in its usual place.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import type { LucideIcon } from "lucide-react";
import {
  ArrowLeft,
  ArrowRight,
  Bitcoin,
  CheckCircle2,
  CloudUpload,
  FileSpreadsheet,
  FileText,
  KeyRound,
  Loader2,
  Play,
  Sparkles,
  Workflow,
  XCircle,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Chip, GOAL_OPTIONS, HORIZON_OPTIONS, RISK_OPTIONS } from "@/components/InvestorProfile";
import SecretField from "@/components/SecretField";
import { useToast } from "@/components/Toast";
import { api, type PipelineRun } from "@/lib/api";

type StepId = "welcome" | "import" | "run" | "goals" | "done";
type ImportChoice = "b3-auto" | "b3-file" | "avenue" | "nomad" | "binance";

/** Dispatched by the (env-gated) dev tab in Configurações to open the wizard
 *  on demand, bypassing the first-run gate. Everything in the preview is live
 *  — imports, credentials and the final save all hit the real backend. */
export const ONBOARDING_PREVIEW_EVENT = "gumbinvest:onboarding-preview";

const STEP_ORDER: StepId[] = ["welcome", "import", "run", "goals", "done"];

const IMPORT_OPTIONS: {
  value: ImportChoice;
  icon: LucideIcon;
  title: string;
  description: string;
}[] = [
  {
    value: "b3-auto",
    icon: Workflow,
    title: "Integração com a B3",
    description:
      "Conecta na Área do Investidor com CPF e senha e baixa suas movimentações sozinha, toda semana.",
  },
  {
    value: "b3-file",
    icon: FileSpreadsheet,
    title: "Arquivo da B3",
    description: "Envie o CSV ou XLSX de movimentação baixado da Área do Investidor.",
  },
  {
    value: "avenue",
    icon: FileText,
    title: "Extratos da Avenue",
    description: "Envie os extratos mensais em PDF da corretora Avenue.",
  },
  {
    value: "nomad",
    icon: FileText,
    title: "Extratos da Nomad",
    description: "Envie os extratos mensais em PDF da Nomad.",
  },
  {
    value: "binance",
    icon: Bitcoin,
    title: "Exportação da Binance",
    description: "Envie o histórico de transações exportado da Binance em CSV.",
  },
];

const FILE_STEP_COPY: Record<
  Exclude<ImportChoice, "b3-auto">,
  { hint: string; accept: string }
> = {
  "b3-file": {
    hint:
      "Na Área do Investidor da B3: Extratos → Movimentação → filtre o período e use \"Extrair dados\". Pode enviar vários arquivos de uma vez.",
    accept:
      ".csv,text/csv,.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  },
  avenue: {
    hint: "No site ou app da Avenue, baixe os extratos mensais em PDF. Pode enviar vários meses de uma vez.",
    accept: ".pdf,application/pdf",
  },
  nomad: {
    hint: "No app da Nomad, baixe os extratos mensais em PDF. Pode enviar vários meses de uma vez.",
    accept: ".pdf,application/pdf",
  },
  binance: {
    hint: "Na Binance: Carteira → Histórico de transações → Exportar. Envie o CSV gerado.",
    accept: ".csv,text/csv",
  },
};

function StepHeading({
  icon: Icon,
  title,
  subtitle,
  tone = "accent",
}: {
  icon: LucideIcon;
  title: string;
  subtitle: string;
  tone?: "accent" | "positive";
}) {
  return (
    <div className="text-center">
      <span
        className={clsx(
          "mx-auto grid h-16 w-16 place-items-center rounded-3xl",
          tone === "positive" ? "bg-positive/15 text-positive" : "bg-accent-soft text-accent",
        )}
      >
        <Icon size={30} />
      </span>
      <h1 className="mt-6 text-3xl font-semibold tracking-tight">{title}</h1>
      <p className="mx-auto mt-3 max-w-lg text-sm text-ink-secondary">{subtitle}</p>
    </div>
  );
}

/** The B3 collector, inline: credentials, first run, live status and the 2FA
 *  code — so the user never has to leave the wizard to watch it work. */
function B3PipelineStep() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [code, setCode] = useState("");

  const pipelinesQ = useQuery({
    queryKey: ["pipelines"],
    queryFn: api.pipelines,
    refetchInterval: (query) =>
      query.state.data?.pipelines.some((item) => item.active_run) ? 2_000 : 15_000,
  });
  const pipeline = pipelinesQ.data?.pipelines.find((item) => item.key === "b3");
  const run = pipeline?.active_run ?? null;
  const waiting = run?.status === "waiting_input";

  const start = useMutation({
    mutationFn: () => api.startPipeline("b3", { full_history: true }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["pipelines"] }),
    onError: (error) => toast.error("Não foi possível iniciar a coleta.", error),
  });

  const answer = useMutation({
    mutationFn: (value: string) => api.answerPipelineRun((run as PipelineRun).id, value),
    onSuccess: () => {
      setCode("");
      queryClient.invalidateQueries({ queryKey: ["pipelines"] });
      toast.success("Código enviado.", { description: "A coleta continua em segundo plano." });
    },
    onError: (error) => toast.error("Não foi possível enviar o código.", error),
  });

  if (!pipeline) {
    return <div className="skeleton mx-auto mt-8 h-48 max-w-md" />;
  }

  const lastLog = run?.log[run.log.length - 1]?.message ?? "Trabalhando…";
  const finished = !run ? pipeline.last_run : null;

  return (
    <div className="mx-auto mt-8 max-w-md space-y-4 text-left">
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

      <button
        type="button"
        className="btn-primary w-full"
        onClick={() => start.mutate()}
        disabled={Boolean(run) || start.isPending || !pipeline.configured}
      >
        {run ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
        {run ? "Coletando…" : "Baixar histórico completo agora"}
      </button>

      {run ? (
        <p className="flex items-center gap-2 text-sm text-ink-secondary">
          <span className="inline-block h-2 w-2 shrink-0 animate-pulse rounded-full bg-accent" />
          {lastLog}
        </p>
      ) : null}

      {waiting && run ? (
        <form
          className="rounded-xl border border-warning/40 bg-warning/5 p-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (code.trim()) answer.mutate(code.trim());
          }}
        >
          <p className="flex items-center gap-2 text-sm font-medium">
            <KeyRound size={15} className="text-warning" />
            {run.input_request?.prompt ?? "A B3 pediu um código de verificação."}
          </p>
          <div className="mt-3 flex gap-2">
            <input
              autoFocus
              inputMode="numeric"
              autoComplete="one-time-code"
              className="input tnum flex-1"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              placeholder="000000"
            />
            <button type="submit" className="btn-primary" disabled={!code.trim() || answer.isPending}>
              {answer.isPending ? "Enviando…" : "Enviar"}
            </button>
          </div>
        </form>
      ) : null}

      {finished?.status === "success" ? (
        <p className="flex items-center gap-2 text-sm text-positive">
          <CheckCircle2 size={15} className="shrink-0" />
          Coleta concluída — {Number(finished.result?.rows_imported ?? 0)} movimentações novas.
        </p>
      ) : finished?.status === "failed" ? (
        <p className="text-sm text-negative">{finished.error ?? "A coleta falhou."}</p>
      ) : null}

      <p className="text-xs text-ink-muted">
        A coleta roda em segundo plano — pode continuar a configuração enquanto ela trabalha. As
        credenciais ficam só no banco local e nunca aparecem de volta na tela. Depois, tudo isso
        vive em Importar → Automações.
      </p>
    </div>
  );
}

/** One-file-at-a-time upload against the same endpoint as /importar, with a
 *  running list of what each file produced. */
function FileImportStep({ choice }: { choice: Exclude<ImportChoice, "b3-auto"> }) {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [results, setResults] = useState<{ name: string; ok: boolean; detail: string }[]>([]);
  const copy = FILE_STEP_COPY[choice];

  const upload = useMutation({
    mutationFn: (file: File) => api.upload(file),
    onSuccess: (result, file) => {
      queryClient.invalidateQueries();
      setResults((prev) => [
        ...prev,
        {
          name: file.name,
          ok: true,
          detail: result.rows_imported
            ? `${result.rows_imported} de ${result.rows_total} linhas novas`
            : "nenhuma linha nova, já estava tudo importado",
        },
      ]);
    },
    onError: (error, file) =>
      setResults((prev) => [
        ...prev,
        { name: file.name, ok: false, detail: error instanceof Error ? error.message : "erro desconhecido" },
      ]),
  });

  // Same rule as /importar: one at a time, in name order, so repeated imports
  // stay reproducible.
  const handleFiles = async (files: FileList | null) => {
    const list = Array.from(files ?? []).sort((a, b) => a.name.localeCompare(b.name));
    for (const file of list) {
      try {
        await upload.mutateAsync(file);
      } catch {
        /* already recorded in `results`; keep going */
      }
    }
  };

  return (
    <div className="mx-auto mt-8 max-w-md space-y-4 text-left">
      <div
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          handleFiles(event.dataTransfer.files);
        }}
        className={clsx(
          "card flex flex-col items-center gap-3 border-2 border-dashed p-8 text-center transition-all duration-300 ease-premium",
          dragging ? "border-accent bg-accent-soft" : "border-line",
        )}
      >
        <span className="grid h-12 w-12 place-items-center rounded-2xl bg-surface-raised text-accent">
          {upload.isPending ? <Loader2 size={22} className="animate-spin" /> : <CloudUpload size={22} />}
        </span>
        <p className="text-sm font-medium">
          {upload.isPending ? "Processando arquivo…" : "Arraste os arquivos aqui ou clique para selecionar"}
        </p>
        <button
          type="button"
          className="btn-primary"
          onClick={() => inputRef.current?.click()}
          disabled={upload.isPending}
        >
          Selecionar arquivos
        </button>
        <input
          ref={inputRef}
          type="file"
          accept={copy.accept}
          multiple
          className="hidden"
          onChange={(event) => handleFiles(event.target.files)}
        />
      </div>

      {results.length ? (
        <ul className="space-y-1.5">
          {results.map((item, index) => (
            <li key={`${item.name}-${index}`} className="flex items-start gap-2 text-sm">
              {item.ok ? (
                <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-positive" />
              ) : (
                <XCircle size={15} className="mt-0.5 shrink-0 text-negative" />
              )}
              <span className="min-w-0">
                <span className="font-medium">{item.name}</span>
                <span className={clsx("ml-1.5", item.ok ? "text-ink-secondary" : "text-negative")}>
                  {item.detail}
                </span>
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      <p className="text-xs text-ink-muted">
        {copy.hint} Reenviar um arquivo nunca duplica nada — movimentos já importados são
        ignorados automaticamente.
      </p>
    </div>
  );
}

export default function Onboarding() {
  const queryClient = useQueryClient();
  const toast = useToast();

  const settingsQ = useQuery({ queryKey: ["settings"], queryFn: api.settings, staleTime: 5 * 60_000 });
  const completed = settingsQ.data ? settingsQ.data.values.onboarding_completed === true : null;
  // Only a maybe-fresh install pays for the overview call; a configured one
  // renders nothing without ever asking.
  const overviewQ = useQuery({ queryKey: ["overview"], queryFn: api.overview, enabled: completed === false });

  // The decision to show is taken once, when the queries first land: the
  // wizard's own imports make `assets_tracked` move mid-flow, and that must
  // not dismiss the screen under the user.
  const [active, setActive] = useState(false);
  const decided = useRef(false);
  useEffect(() => {
    if (decided.current || completed === null) return;
    if (completed) {
      decided.current = true;
      return;
    }
    if (!overviewQ.data) return;
    decided.current = true;
    if (overviewQ.data.assets_tracked === 0) setActive(true);
  }, [completed, overviewQ.data]);

  const [step, setStep] = useState<StepId>("welcome");
  const [name, setName] = useState("");
  const [choice, setChoice] = useState<ImportChoice | null>(null);
  const [goals, setGoals] = useState<string[]>([]);
  const [horizon, setHorizon] = useState("");
  const [risk, setRisk] = useState("");
  const [notes, setNotes] = useState("");

  // Dev preview: reopen from the first step regardless of the gate above.
  useEffect(() => {
    const open = () => {
      decided.current = true;
      setStep("welcome");
      setActive(true);
    };
    window.addEventListener(ONBOARDING_PREVIEW_EVENT, open);
    return () => window.removeEventListener(ONBOARDING_PREVIEW_EVENT, open);
  }, []);

  // Skipping still keeps whatever was already filled in — skipping the rest of
  // the setup shouldn't throw away a name the user just typed.
  const finish = useMutation({
    mutationFn: (_mode: "done" | "skip") => {
      const values: Record<string, unknown> = { onboarding_completed: true };
      if (name.trim()) values.user_name = name.trim();
      if (goals.length || horizon || risk || notes.trim()) {
        values.investor_profile = {
          objetivos: goals,
          horizonte: horizon,
          risco: risk,
          notas: notes.trim(),
        };
      }
      return api.updateSettings(values);
    },
    onSuccess: (_data, mode) => {
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      setActive(false);
      if (mode === "done") {
        toast.success("Configuração concluída.", {
          description: "Tudo pode ser ajustado depois, em Configurações.",
        });
      } else {
        toast.info("Configuração pulada.", {
          description: "Importe seus dados quando quiser, na página Importar.",
        });
      }
    },
    onError: (error) => toast.error("Não foi possível salvar a configuração.", error),
  });

  if (!active) return null;

  const next = () => {
    if (step === "welcome") setStep("import");
    else if (step === "import") setStep(choice ? "run" : "goals");
    else if (step === "run") setStep("goals");
    else if (step === "goals") setStep("done");
  };
  const back = () => {
    if (step === "import") setStep("welcome");
    else if (step === "run") setStep("import");
    else if (step === "goals") setStep(choice ? "run" : "import");
    else if (step === "done") setStep("goals");
  };
  const skipStep = () => {
    if (step === "welcome") setStep("import");
    else if (step === "import" || step === "run") setStep("goals");
    else if (step === "goals") setStep("done");
  };

  const chosen = IMPORT_OPTIONS.find((option) => option.value === choice);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Configuração inicial"
      className="desktop-shell-anchored fixed inset-0 z-[70] overflow-y-auto bg-canvas"
      // The wizard covers the page, so it repaints the page's own two washes
      // rather than letting the body's show through.
      style={{ backgroundImage: "var(--page-glow)" }}
    >
      <div className="flex min-h-full flex-col px-6 py-5">
        <div className="flex h-8 justify-end">
          {step !== "done" ? (
            <button
              type="button"
              className="text-sm text-ink-muted transition-colors hover:text-ink"
              onClick={() => finish.mutate("skip")}
              disabled={finish.isPending}
            >
              Pular configuração
            </button>
          ) : null}
        </div>

        <div className="flex flex-1 flex-col items-center justify-center py-8">
          <div key={step} className="w-full max-w-2xl animate-fade-up">
            {step === "welcome" ? (
              <>
                <StepHeading
                  icon={Sparkles}
                  title="Bem-vindo ao GumbInvest"
                  subtitle="Seu gestor de carteira pessoal e privado. Vamos deixar tudo pronto em alguns passos rápidos - todos opcionais."
                />
                <div className="mx-auto mt-8 max-w-sm text-left">
                  <label className="block text-sm text-ink-secondary">
                    Como você quer ser chamado?
                    <input
                      autoFocus
                      className="input mt-1.5"
                      value={name}
                      onChange={(event) => setName(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") next();
                      }}
                      placeholder="Seu nome (opcional)"
                    />
                  </label>
                  <p className="mt-1.5 text-xs text-ink-muted">
                    A IA usa seu nome nas conversas sobre a carteira.
                  </p>
                </div>
              </>
            ) : null}

            {step === "import" ? (
              <>
                <StepHeading
                  icon={CloudUpload}
                  title="Traga suas movimentações"
                  subtitle="Escolha por onde começar. Sem pressa: tudo isso continua disponível depois, a qualquer momento, na página Importar."
                />
                <div className="mt-8 grid gap-3 sm:grid-cols-2">
                  {IMPORT_OPTIONS.map((option) => {
                    const selected = choice === option.value;
                    return (
                      <button
                        key={option.value}
                        type="button"
                        onClick={() => setChoice(selected ? null : option.value)}
                        className={clsx(
                          "card p-4 text-left transition-all duration-200 ease-premium",
                          selected
                            ? "border-accent ring-1 ring-accent/50"
                            : "hover:border-line-strong",
                        )}
                      >
                        <span
                          className={clsx(
                            "grid h-9 w-9 place-items-center rounded-xl",
                            selected ? "bg-accent-soft text-accent" : "bg-surface-raised text-ink-secondary",
                          )}
                        >
                          <option.icon size={18} />
                        </span>
                        <p className="mt-3 text-sm font-medium">{option.title}</p>
                        <p className="mt-1 text-xs text-ink-muted">{option.description}</p>
                      </button>
                    );
                  })}
                </div>
              </>
            ) : null}

            {step === "run" && chosen ? (
              <>
                <StepHeading
                  icon={chosen.icon}
                  title={chosen.title}
                  subtitle={
                    choice === "b3-auto"
                      ? "Informe as credenciais da Área do Investidor e dispare a primeira coleta — ela baixa o histórico completo."
                      : "Envie os arquivos para importar suas primeiras movimentações."
                  }
                />
                {choice === "b3-auto" ? (
                  <B3PipelineStep />
                ) : choice ? (
                  <FileImportStep choice={choice} />
                ) : null}
              </>
            ) : null}

            {step === "goals" ? (
              <>
                <StepHeading
                  icon={Sparkles}
                  title="Conte seus objetivos"
                  subtitle="A IA usa isso para dar conselhos mais adequados a você — no chat da carteira e no aporte inteligente. Opcional, como tudo aqui."
                />
                <div className="mx-auto mt-8 max-w-lg space-y-6 text-left">
                  <div>
                    <p className="mb-2 text-sm font-medium text-ink-secondary">
                      O que você busca? <span className="text-xs text-ink-muted">(quantos quiser)</span>
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {GOAL_OPTIONS.map((goal) => (
                        <Chip
                          key={goal}
                          selected={goals.includes(goal)}
                          onClick={() =>
                            setGoals((prev) =>
                              prev.includes(goal) ? prev.filter((item) => item !== goal) : [...prev, goal],
                            )
                          }
                        >
                          {goal}
                        </Chip>
                      ))}
                    </div>
                  </div>
                  <div>
                    <p className="mb-2 text-sm font-medium text-ink-secondary">Horizonte de investimento</p>
                    <div className="flex flex-wrap gap-2">
                      {HORIZON_OPTIONS.map((option) => (
                        <Chip
                          key={option}
                          selected={horizon === option}
                          onClick={() => setHorizon(horizon === option ? "" : option)}
                        >
                          {option}
                        </Chip>
                      ))}
                    </div>
                  </div>
                  <div>
                    <p className="mb-2 text-sm font-medium text-ink-secondary">Perfil de risco</p>
                    <div className="flex flex-wrap gap-2">
                      {RISK_OPTIONS.map((option) => (
                        <Chip
                          key={option}
                          selected={risk === option}
                          onClick={() => setRisk(risk === option ? "" : option)}
                        >
                          {option}
                        </Chip>
                      ))}
                    </div>
                  </div>
                  <label className="block text-sm text-ink-secondary">
                    Algo mais que a IA deva saber?
                    <textarea
                      className="input mt-1.5 resize-none"
                      rows={2}
                      value={notes}
                      onChange={(event) => setNotes(event.target.value)}
                      placeholder="Ex.: prefiro fundos imobiliários, evito alavancagem… (opcional)"
                    />
                  </label>
                </div>
              </>
            ) : null}

            {step === "done" ? (
              <>
                <StepHeading
                  icon={CheckCircle2}
                  tone="positive"
                  title={name.trim() ? `Tudo pronto, ${name.trim()}!` : "Tudo pronto!"}
                  subtitle="O GumbInvest está configurado. Alguns lugares que valem conhecer:"
                />
                <ul className="mx-auto mt-8 max-w-md space-y-3 text-left text-sm text-ink-secondary">
                  <li className="flex items-start gap-2.5">
                    <CloudUpload size={16} className="mt-0.5 shrink-0 text-accent" />
                    <span>
                      <span className="font-medium text-ink">Importar</span> — envie novos extratos
                      quando quiser; reenviar nunca duplica nada.
                    </span>
                  </li>
                  <li className="flex items-start gap-2.5">
                    <Workflow size={16} className="mt-0.5 shrink-0 text-accent" />
                    <span>
                      <span className="font-medium text-ink">Importar → Automações</span> — a
                      coleta automática da B3 e suas credenciais; seu nome e objetivos ficam em
                      Configurações → Geral.
                    </span>
                  </li>
                  <li className="flex items-start gap-2.5">
                    <Sparkles size={16} className="mt-0.5 shrink-0 text-accent" />
                    <span>
                      <span className="font-medium text-ink">Chat da carteira</span> — o botão no
                      topo conversa com a IA sobre seus ativos e sua alocação.
                    </span>
                  </li>
                </ul>
                <div className="mt-10 text-center">
                  <button
                    type="button"
                    className="btn-primary px-8"
                    onClick={() => finish.mutate("done")}
                    disabled={finish.isPending}
                  >
                    {finish.isPending ? <Loader2 size={15} className="animate-spin" /> : null}
                    Começar a usar
                  </button>
                </div>
              </>
            ) : null}

            {step !== "done" ? (
              <div className="mx-auto mt-10 flex max-w-lg items-center justify-between">
                <div>
                  {step !== "welcome" ? (
                    <button type="button" className="btn-ghost" onClick={back}>
                      <ArrowLeft size={15} /> Voltar
                    </button>
                  ) : null}
                </div>
                <div className="flex gap-2">
                  <button type="button" className="btn-ghost" onClick={skipStep}>
                    Pular etapa
                  </button>
                  <button type="button" className="btn-primary" onClick={next}>
                    Continuar <ArrowRight size={15} />
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        </div>

        <div className="flex justify-center gap-2 pb-3" aria-hidden="true">
          {STEP_ORDER.map((item) => (
            <span
              key={item}
              className={clsx(
                "h-1.5 rounded-full transition-all duration-300 ease-premium",
                item === step
                  ? "w-6 bg-accent"
                  : STEP_ORDER.indexOf(item) < STEP_ORDER.indexOf(step)
                    ? "w-1.5 bg-accent/50"
                    : "w-1.5 bg-line-strong",
              )}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
