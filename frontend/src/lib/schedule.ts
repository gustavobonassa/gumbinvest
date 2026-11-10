/** Words for a collector's schedule (Automações). Weekdays follow the
 *  backend's convention: 0 = Monday … 6 = Sunday. */
import type { PipelineSchedule } from "@/lib/api";

/** Chip labels, in the week's order. */
export const WEEKDAYS_SHORT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"] as const;

const WEEKDAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"];
const WEEKDAYS_FULL = [
  "segunda-feira",
  "terça-feira",
  "quarta-feira",
  "quinta-feira",
  "sexta-feira",
  "sábado",
  "domingo",
];

function joinPt(items: string[]): string {
  return items.length < 2 ? items.join("") : `${items.slice(0, -1).join(", ")} e ${items.at(-1)}`;
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Which days, in words: "Toda segunda-feira", "Dias úteis", "Segunda, quarta e sexta". */
export function daysPhrase(days: number[]): string {
  const sorted = [...new Set(days)].sort((a, b) => a - b);
  const key = sorted.join(",");
  if (key === "0,1,2,3,4,5,6") return "Todos os dias";
  if (key === "0,1,2,3,4") return "Dias úteis";
  if (key === "5,6") return "Fins de semana";
  if (sorted.length === 1) {
    const day = sorted[0];
    return `${day >= 5 ? "Todo" : "Toda"} ${WEEKDAYS_FULL[day]}`;
  }
  return capitalize(joinPt(sorted.map((day) => WEEKDAYS[day])));
}

/** The card's one-liner: "Toda segunda-feira às 07:30", or that it is off. */
export function scheduleLabel(schedule: PipelineSchedule): string {
  if (!schedule.enabled || !schedule.days.length) return "Execução automática desativada";
  return `${daysPhrase(schedule.days)} às ${schedule.time}`;
}

/** "seg., 09/11 às 07:30" — the next slot, short enough for a hint line.
 *  Read off the string's own wall-clock (the backend sends it in the app
 *  timezone) rather than through `Date`, which would move it into the
 *  browser's zone and contradict the time the user just picked. */
export function nextRunPhrase(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value);
  if (!match) return value;
  const [, year, month, day, hour, minute] = match;
  const weekday = new Date(Date.UTC(+year, +month - 1, +day)).toLocaleDateString("pt-BR", {
    weekday: "short",
    timeZone: "UTC",
  });
  return `${weekday}, ${day}/${month} às ${hour}:${minute}`;
}
