import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import Dashboard from "@/pages/Dashboard";

// Every endpoint the page touches answers with an empty list unless a test
// says otherwise; only the overview is scripted.
const overview = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  const api = new Proxy(
    {},
    { get: (_target, name) => (name === "overview" ? overview : () => Promise.resolve([])) },
  );
  return { ...actual, api };
});

function renderDashboard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

describe("Dashboard", () => {
  beforeEach(() => overview.mockReset());

  it("invites an import when nothing is tracked yet", async () => {
    overview.mockResolvedValue({ assets_tracked: 0 });
    renderDashboard();
    expect(await screen.findByText("Nenhuma transação importada")).toBeInTheDocument();
  });

  it("shows the error state when a refetch fails after a good load", async () => {
    // Regression: the error return used to sit above four useMemo hooks, so a
    // failed background refetch changed the hook count and React threw
    // ("Rendered fewer hooks than expected") instead of rendering this card.
    overview.mockResolvedValueOnce({ assets_tracked: 0 });
    const client = renderDashboard();
    await screen.findByText("Nenhuma transação importada");

    overview.mockRejectedValueOnce(new Error("backend fora do ar"));
    await act(() => client.refetchQueries({ queryKey: ["overview"] }));

    expect(await screen.findByText("Não foi possível carregar os dados")).toBeInTheDocument();
    expect(screen.getByText("backend fora do ar")).toBeInTheDocument();
  });
});
