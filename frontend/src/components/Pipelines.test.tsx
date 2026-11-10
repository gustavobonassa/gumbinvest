import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Pipelines from "@/components/Pipelines";
import { ToastProvider } from "@/components/Toast";
import { api, type PipelineInfo, type PipelineRun } from "@/lib/api";

const pipeline = (
  key: string,
  name: string,
  configured: boolean,
  supportsPreview = false,
): PipelineInfo => ({
  key,
  name,
  description: `${name} description`,
  schedule: { enabled: false, days: [0], time: "07:30", next_run_at: null },
  supports_preview: supportsPreview,
  configured,
  credentials: [{ key: `${key}_secret`, label: `${name} secret`, configured }],
  active_run: null,
  last_run: null,
});

let search = "";
function LocationSpy() {
  search = useLocation().search;
  return null;
}

const renderAt = (url: string) =>
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ToastProvider>
        <MemoryRouter initialEntries={[url]}>
          <Pipelines />
          <LocationSpy />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );

describe("Automações tabs", () => {
  beforeEach(() => {
    vi.spyOn(api, "pipelines").mockResolvedValue({
      pipelines: [pipeline("b3", "B3", true), pipeline("binance", "Binance", false, true)],
    });
    vi.spyOn(api, "pipelineRuns").mockResolvedValue({
      runs: [],
      total: 0,
      page: 1,
      pages: 1,
      page_size: 10,
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("shows one automation at a time, the first by default", async () => {
    renderAt("/importar?aba=automacoes");

    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual([
      expect.stringContaining("B3"),
      expect.stringContaining("Binance"),
    ]);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    const panel = screen.getByRole("tabpanel");
    expect(within(panel).getByText("B3 description")).toBeInTheDocument();
    expect(screen.queryByText("Binance description")).not.toBeInTheDocument();
    expect(api.pipelineRuns).toHaveBeenCalledWith(1, 10, "b3");
  });

  it("switches by click, keeps the other tab params and filters the history", async () => {
    renderAt("/importar?aba=automacoes");
    const binance = await screen.findByRole("tab", { name: /Binance/ });
    expect(binance).toHaveTextContent("Credenciais pendentes");

    await userEvent.click(binance);

    expect(binance).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Binance description")).toBeInTheDocument();
    expect(search).toContain("aba=automacoes");
    expect(search).toContain("servico=binance");
    expect(api.pipelineRuns).toHaveBeenLastCalledWith(1, 10, "binance");
  });

  it("opens the tab named in the URL and moves with the arrow keys", async () => {
    renderAt("/importar?aba=automacoes&servico=binance");
    const binance = await screen.findByRole("tab", { name: /Binance/ });
    expect(binance).toHaveAttribute("aria-selected", "true");

    binance.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: /B3/ })).toHaveAttribute("aria-selected", "true");
  });

  it("offers Simular only where the automation supports it, and sends preview", async () => {
    vi.mocked(api.pipelines).mockResolvedValue({
      pipelines: [pipeline("b3", "B3", true), pipeline("binance", "Binance", true, true)],
    });
    const start = vi.spyOn(api, "startPipeline").mockResolvedValue({ run_id: 1 });
    renderAt("/importar?aba=automacoes");

    await screen.findAllByRole("tab");
    expect(screen.queryByRole("button", { name: /Simular/ })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: /Binance/ }));
    await userEvent.click(screen.getByRole("button", { name: /Simular/ }));
    expect(start).toHaveBeenCalledWith("binance", { full_history: false, preview: true });
  });
});

describe("Automações run log", () => {
  const failed: PipelineRun = {
    id: 27,
    pipeline: "b3",
    trigger: "manual",
    status: "failed",
    active: false,
    started_at: "2026-11-08T20:12:00Z",
    finished_at: "2026-11-08T20:14:09Z",
    log: [
      { at: "2026-11-08T20:12:01Z", level: "info", message: "Coleta B3 (build x)." },
      {
        at: "2026-11-08T20:14:09Z",
        level: "error",
        message: "Erro inesperado — Error: new-line character seen in unquoted field",
      },
    ],
    input_request: null,
    options: {},
    result: {},
    error: "A coleta falhou de forma inesperada. Veja o registro da execução.",
  };

  beforeEach(() => {
    vi.spyOn(api, "pipelines").mockResolvedValue({
      pipelines: [{ ...pipeline("b3", "B3", true), last_run: failed }],
    });
    vi.spyOn(api, "pipelineRuns").mockResolvedValue({
      runs: [failed],
      total: 1,
      page: 1,
      pages: 1,
      page_size: 10,
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("opens the log from the failure on the card", async () => {
    renderAt("/importar?aba=automacoes");

    await userEvent.click(await screen.findByRole("button", { name: "Ver registro" }));

    const dialog = screen.getByRole("dialog", { name: "Registro da execução" });
    const lines = within(dialog).getByRole("list", { name: "Linhas do registro" });
    expect(within(lines).getAllByRole("listitem")).toHaveLength(2);
    expect(lines).toHaveTextContent("new-line character seen in unquoted field");
    expect(dialog).toHaveTextContent("Veja o registro da execução.");
  });

  it("opens it from a row of the history, and copies it as text", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderAt("/importar?aba=automacoes");

    await userEvent.click(await screen.findByRole("button", { name: /Ver registro da coleta de/ }));
    await userEvent.click(screen.getByRole("button", { name: /Copiar registro/ }));

    const text = writeText.mock.calls[0][0] as string;
    expect(text).toContain("B3 · execução #27");
    expect(text).toContain("[erro] Erro inesperado");
    expect(await screen.findByText("Registro copiado.")).toBeInTheDocument();
  });
});

describe("Automações schedule", () => {
  beforeEach(() => {
    vi.spyOn(api, "pipelines").mockResolvedValue({ pipelines: [pipeline("b3", "B3", true)] });
    vi.spyOn(api, "pipelineRuns").mockResolvedValue({
      runs: [],
      total: 0,
      page: 1,
      pages: 1,
      page_size: 10,
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("is off by default, and the modal turns it on with the chosen days and time", async () => {
    const save = vi.spyOn(api, "savePipelineSchedule").mockResolvedValue({
      schedule: {
        enabled: true,
        days: [0, 2],
        time: "18:15",
        next_run_at: "2026-11-09T21:15:00Z",
      },
    });
    const user = userEvent.setup();
    renderAt("/");

    expect(await screen.findByText("Execução automática desativada")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Alterar agendamento de B3" }));
    const dialog = await screen.findByRole("dialog");

    // Off: the days and time are kept but cannot be edited.
    const wednesday = within(dialog).getByRole("button", { name: "Qua" });
    expect(wednesday).toBeDisabled();

    await user.click(within(dialog).getByRole("checkbox", { name: /Executar automaticamente/ }));
    await user.click(wednesday);
    const time = within(dialog).getByLabelText("Horário");
    await user.clear(time);
    await user.type(time, "18:15");
    await user.click(within(dialog).getByRole("button", { name: "Salvar" }));

    expect(save).toHaveBeenCalledWith("b3", { enabled: true, days: [0, 2], time: "18:15" });
    expect(await screen.findByText("Agendamento salvo.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("will not save an enabled schedule with no weekday", async () => {
    const save = vi.spyOn(api, "savePipelineSchedule");
    const user = userEvent.setup();
    renderAt("/");

    await user.click(await screen.findByRole("button", { name: "Alterar agendamento de B3" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("checkbox", { name: /Executar automaticamente/ }));
    await user.click(within(dialog).getByRole("button", { name: "Seg" }));

    expect(within(dialog).getByText("Escolha pelo menos um dia.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Salvar" })).toBeDisabled();
    expect(save).not.toHaveBeenCalled();
  });
});
