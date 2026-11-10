import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import StatementCoverage from "@/components/StatementCoverage";
import { api, type CoverageAccount } from "@/lib/api";

const account = (overrides: Partial<CoverageAccount>): CoverageAccount => ({
  broker: "Avenue",
  account_ref: "123",
  currency: "USD",
  first_month: "2026-05",
  last_month: "2026-06",
  statements: 2,
  is_complete: true,
  months: [
    {
      month: "2026-05",
      files: ["mai.pdf"],
      formats: [],
      opening_balance: null,
      closing_balance: null,
      transactions: 1,
    },
    {
      month: "2026-06",
      files: ["jun.pdf"],
      formats: [],
      opening_balance: null,
      closing_balance: null,
      transactions: 1,
    },
  ],
  missing_months: [],
  balance_breaks: [],
  position_drift: [],
  overdue_months: [],
  ...overrides,
});

const renderWith = (accounts: CoverageAccount[]) => {
  vi.spyOn(api, "coverage").mockResolvedValue({ accounts } as Awaited<
    ReturnType<typeof api.coverage>
  >);
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <StatementCoverage />
    </QueryClientProvider>,
  );
};

describe("Cobertura dos extratos", () => {
  afterEach(() => vi.restoreAllMocks());

  it("lists the months still to import since the last statement", async () => {
    renderWith([account({ overdue_months: ["2026-07", "2026-08", "2026-09", "2026-10"] })]);

    expect(await screen.findByText("4 meses pendentes")).toBeInTheDocument();
    expect(screen.getByText(/Desde o último extrato/)).toBeInTheDocument();
    expect(screen.getAllByTitle(/ainda não importado/)).toHaveLength(4);
  });

  it("says nothing when the account is up to date", async () => {
    renderWith([account({})]);

    expect(await screen.findByText("Avenue")).toBeInTheDocument();
    expect(screen.queryByText(/pendente/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Desde o último extrato/)).not.toBeInTheDocument();
  });
});
