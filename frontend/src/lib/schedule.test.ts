import { describe, expect, it } from "vitest";

import { daysPhrase, nextRunPhrase, scheduleLabel } from "@/lib/schedule";

describe("schedule words", () => {
  it("names the common sets, single days and lists", () => {
    expect(daysPhrase([0, 1, 2, 3, 4, 5, 6])).toBe("Todos os dias");
    expect(daysPhrase([4, 3, 2, 1, 0])).toBe("Dias úteis");
    expect(daysPhrase([5, 6])).toBe("Fins de semana");
    expect(daysPhrase([0])).toBe("Toda segunda-feira");
    expect(daysPhrase([6])).toBe("Todo domingo");
    expect(daysPhrase([4, 0, 2])).toBe("Segunda, quarta e sexta");
  });

  it("says when it is off, whatever days are kept", () => {
    expect(scheduleLabel({ enabled: false, days: [0], time: "07:30", next_run_at: null })).toBe(
      "Execução automática desativada",
    );
    expect(scheduleLabel({ enabled: true, days: [1, 3], time: "07:30", next_run_at: "x" })).toBe(
      "Terça e quinta às 07:30",
    );
  });

  it("shows the next run at its own wall-clock, whatever the browser zone", () => {
    expect(nextRunPhrase("2026-11-09T07:30:00-03:00")).toBe("seg., 09/11 às 07:30");
  });
});
