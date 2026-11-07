import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Pipelines from "@/components/Pipelines";
import { ToastProvider } from "@/components/Toast";
import { api, type PipelineInfo } from "@/lib/api";

const pipeline = (
  key: string,
  name: string,
  configured: boolean,
  supportsPreview = false,
): PipelineInfo => ({
  key,
  name,
  description: `${name} description`,
  schedule: "Semanal",
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
