/**
 * The owner's name and declared goals — the AI prompts read them (asset and
 * portfolio chat, aporte inteligente). First written by the onboarding wizard,
 * edited here ever after: `InvestorProfileCard` lives on Configurações → Geral,
 * and the wizard imports the shared options/Chip so both screens stay in sync.
 *
 * Stored as two AppSettings (`user_name`, `investor_profile`), with the
 * profile's fields kept as the pt-BR labels themselves — the only consumer is
 * a prompt, which wants human words, not codes.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useEffect, useState, type ReactNode } from "react";

import { useToast } from "@/components/Toast";
import { Card, SectionTitle } from "@/components/ui";
import { api } from "@/lib/api";

export const GOAL_OPTIONS = [
  "Aposentadoria",
  "Renda passiva com dividendos",
  "Crescimento de patrimônio",
  "Reserva de emergência",
  "Aprender a investir",
];
export const HORIZON_OPTIONS = [
  "Curto prazo (até 2 anos)",
  "Médio prazo (2 a 10 anos)",
  "Longo prazo (mais de 10 anos)",
];
export const RISK_OPTIONS = ["Conservador", "Moderado", "Arrojado"];

export function Chip({
  selected,
  onClick,
  children,
}: {
  selected: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "rounded-full border px-3.5 py-1.5 text-sm transition-colors duration-200 ease-premium",
        selected
          ? "border-accent bg-accent-soft text-ink"
          : "border-line bg-surface-raised text-ink-secondary hover:border-line-strong hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

/** Configurações → Geral. No save button, like the rest of the tab: every
 *  change autosaves after a short pause, so a burst of chip clicks or a typed
 *  name lands as one write and one toast. */
export default function InvestorProfileCard({ values }: { values: Record<string, unknown> }) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const stored = (values.investor_profile ?? {}) as Record<string, unknown>;
  const [name, setName] = useState(String(values.user_name ?? ""));
  const [goals, setGoals] = useState<string[]>(
    Array.isArray(stored.objetivos) ? stored.objetivos.map(String) : [],
  );
  const [horizon, setHorizon] = useState(typeof stored.horizonte === "string" ? stored.horizonte : "");
  const [risk, setRisk] = useState(typeof stored.risco === "string" ? stored.risco : "");
  const [notes, setNotes] = useState(typeof stored.notas === "string" ? stored.notas : "");
  const [dirty, setDirty] = useState(false);

  const save = useMutation({
    mutationFn: () =>
      api.updateSettings({
        user_name: name.trim(),
        investor_profile: {
          objetivos: goals,
          horizonte: horizon,
          risco: risk,
          notas: notes.trim(),
        },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      toast.success("Perfil salvo.");
    },
    onError: (error) => toast.error("Não foi possível salvar o perfil.", error),
  });
  const { mutate } = save;

  useEffect(() => {
    if (!dirty) return undefined;
    const timer = setTimeout(() => {
      setDirty(false);
      mutate();
    }, 900);
    return () => clearTimeout(timer);
  }, [dirty, name, goals, horizon, risk, notes, mutate]);

  const touch = <T,>(setter: (next: T) => void) => (next: T) => {
    setter(next);
    setDirty(true);
  };
  const setNameTouched = touch(setName);
  const setGoalsTouched = touch(setGoals);
  const setHorizonTouched = touch(setHorizon);
  const setRiskTouched = touch(setRisk);
  const setNotesTouched = touch(setNotes);

  return (
    <Card className="p-5">
      <SectionTitle
        title="Perfil do investidor"
        subtitle="Seu nome e objetivos — a IA usa isso nas conversas e no aporte inteligente"
        action={save.isPending ? <span className="text-xs text-ink-muted">salvando…</span> : undefined}
      />
      <div className="space-y-5">
        <div className="max-w-sm">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Como você quer ser chamado</span>
          <input
            className="input"
            value={name}
            onChange={(event) => setNameTouched(event.target.value)}
            placeholder="Seu nome (opcional)"
          />
        </div>
        <div>
          <span className="mb-2 block text-xs font-medium text-ink-muted">
            Objetivos (quantos quiser)
          </span>
          <div className="flex flex-wrap gap-2">
            {GOAL_OPTIONS.map((goal) => (
              <Chip
                key={goal}
                selected={goals.includes(goal)}
                onClick={() =>
                  setGoalsTouched(
                    goals.includes(goal) ? goals.filter((item) => item !== goal) : [...goals, goal],
                  )
                }
              >
                {goal}
              </Chip>
            ))}
          </div>
        </div>
        <div>
          <span className="mb-2 block text-xs font-medium text-ink-muted">Horizonte de investimento</span>
          <div className="flex flex-wrap gap-2">
            {HORIZON_OPTIONS.map((option) => (
              <Chip
                key={option}
                selected={horizon === option}
                onClick={() => setHorizonTouched(horizon === option ? "" : option)}
              >
                {option}
              </Chip>
            ))}
          </div>
        </div>
        <div>
          <span className="mb-2 block text-xs font-medium text-ink-muted">Perfil de risco</span>
          <div className="flex flex-wrap gap-2">
            {RISK_OPTIONS.map((option) => (
              <Chip
                key={option}
                selected={risk === option}
                onClick={() => setRiskTouched(risk === option ? "" : option)}
              >
                {option}
              </Chip>
            ))}
          </div>
        </div>
        <div>
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">
            Algo mais que a IA deva saber?
          </span>
          <textarea
            className="input resize-none"
            rows={2}
            value={notes}
            onChange={(event) => setNotesTouched(event.target.value)}
            placeholder="Ex.: prefiro fundos imobiliários, evito alavancagem… (opcional)"
          />
        </div>
      </div>
      <p className="mt-4 text-xs text-ink-muted">
        Tudo opcional. Deixar em branco só faz a IA responder sem personalização, nada além dos
        prompts de IA lê estes campos.
      </p>
    </Card>
  );
}
