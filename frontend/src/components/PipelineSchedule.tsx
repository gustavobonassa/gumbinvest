/**
 * Automações → "Alterar" next to a collector's schedule line: turn the
 * automatic run on or off, pick the weekdays and the time.
 *
 * Off by default (a collection signs into a broker unattended, so it only
 * does that once asked). Turning it off keeps the days and time, so turning it
 * back on is one click. A slot missed while the app — or the computer — was
 * off is made up as soon as it is back; the hint says so because, in the
 * desktop app, that is the difference between "every Monday" and "every
 * Monday the app happened to be open".
 */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";

import { api, type PipelineInfo } from "@/lib/api";
import { Modal } from "@/components/ui";
import { useToast } from "@/components/Toast";
import { WEEKDAYS_SHORT, nextRunPhrase, scheduleLabel } from "@/lib/schedule";

export default function PipelineScheduleModal({
  pipeline,
  onClose,
}: {
  pipeline: PipelineInfo;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [enabled, setEnabled] = useState(pipeline.schedule.enabled);
  const [days, setDays] = useState<number[]>(pipeline.schedule.days);
  const [time, setTime] = useState(pipeline.schedule.time);

  const noDays = enabled && days.length === 0;
  const badTime = !/^([01]\d|2[0-3]):[0-5]\d$/.test(time);

  const save = useMutation({
    mutationFn: () => api.savePipelineSchedule(pipeline.key, { enabled, days, time }),
    onSuccess: ({ schedule }) => {
      queryClient.invalidateQueries({ queryKey: ["pipelines"] });
      toast.success(
        schedule.enabled ? "Agendamento salvo." : "Execução automática desativada.",
        schedule.enabled && schedule.next_run_at
          ? {
              description: `${scheduleLabel(schedule)}. Próxima coleta: ${nextRunPhrase(
                schedule.next_run_at,
              )}.`,
            }
          : undefined,
      );
      onClose();
    },
    onError: (error) => toast.error("Não foi possível salvar o agendamento.", error),
  });

  const toggleDay = (day: number) =>
    setDays((current) =>
      current.includes(day)
        ? current.filter((item) => item !== day)
        : [...current, day].sort((a, b) => a - b),
    );

  return (
    <Modal open onClose={onClose} title="Agendamento" subtitle={pipeline.name}>
      <form
        className="space-y-5"
        onSubmit={(event) => {
          event.preventDefault();
          if (!noDays && !badTime) save.mutate();
        }}
      >
        <label className="flex cursor-pointer items-start gap-3">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
          />
          <span className="text-sm text-ink">
            Executar automaticamente
            <span className="mt-0.5 block text-xs text-ink-muted">
              A coleta roda sozinha nos dias e no horário escolhidos.
            </span>
          </span>
        </label>

        <fieldset disabled={!enabled} className={clsx("space-y-5", !enabled && "opacity-50")}>
          <div>
            <p id="schedule-days" className="mb-2 text-xs font-medium text-ink-secondary">
              Dias da semana
            </p>
            <div role="group" aria-labelledby="schedule-days" className="flex flex-wrap gap-1.5">
              {WEEKDAYS_SHORT.map((label, day) => {
                const on = days.includes(day);
                return (
                  <button
                    key={label}
                    type="button"
                    aria-pressed={on}
                    onClick={() => toggleDay(day)}
                    className={clsx(
                      "h-9 min-w-[3rem] rounded-xl border px-2.5 text-sm font-medium transition-colors disabled:cursor-not-allowed",
                      on
                        ? "border-accent bg-accent text-white"
                        : "border-line bg-surface-raised text-ink-secondary hover:border-line-strong hover:text-ink",
                    )}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
            {noDays ? (
              <p className="mt-2 text-xs text-negative">Escolha pelo menos um dia.</p>
            ) : null}
          </div>

          <label className="block">
            <span className="mb-2 block text-xs font-medium text-ink-secondary">Horário</span>
            <input
              type="time"
              required
              className="input w-36"
              value={time}
              onChange={(event) => setTime(event.target.value)}
            />
          </label>
        </fieldset>

        <p className="rounded-lg border border-line bg-canvas px-3 py-2 text-xs leading-relaxed text-ink-muted">
          Se o GumbInvest estiver fechado (ou o computador desligado) no horário, a coleta roda
          assim que ele for aberto de novo.
          {pipeline.configured ? null : " Preencha as credenciais para que ela possa rodar."}
        </p>

        <div className="flex flex-wrap justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={onClose}>
            Cancelar
          </button>
          <button
            type="submit"
            className="btn-primary"
            disabled={noDays || badTime || save.isPending}
          >
            Salvar
          </button>
        </div>
      </form>
    </Modal>
  );
}
