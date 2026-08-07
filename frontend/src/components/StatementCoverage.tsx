/**
 * Which broker statements are held, and what looks missing.
 *
 * The month grid is the point of this component: a year of statements is a row
 * of squares, and a hole in the row is instantly visible in a way that a list
 * of dates is not. The two subtler checks — balances that do not carry from one
 * month to the next, and positions that disagree with the broker's own figures
 * — are spelled out underneath, because they need an explanation to act on.
 */
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, FileWarning, Scale } from "lucide-react";

import { Badge, Card, EmptyState, ErrorState, SectionTitle, Skeleton } from "@/components/ui";
import { api, type CoverageAccount } from "@/lib/api";
import { periodLabel } from "@/lib/format";

/** Every month from `first` to `last`, inclusive, as "YYYY-MM". */
function monthRange(first: string, last: string): string[] {
  const months: string[] = [];
  const [startYear, startMonth] = first.split("-").map(Number);
  const [endYear, endMonth] = last.split("-").map(Number);
  let year = startYear;
  let month = startMonth;
  while (year < endYear || (year === endYear && month <= endMonth)) {
    months.push(`${year}-${String(month).padStart(2, "0")}`);
    if (month === 12) {
      year += 1;
      month = 1;
    } else {
      month += 1;
    }
  }
  return months;
}

function MonthGrid({ account }: { account: CoverageAccount }) {
  if (!account.first_month || !account.last_month) return null;
  const present = new Map(account.months.map((month) => [month.month, month]));
  const months = monthRange(account.first_month, account.last_month);

  return (
    <div className="mt-3 flex flex-wrap gap-1">
      {months.map((month) => {
        const entry = present.get(month);
        return (
          <span
            key={month}
            title={
              entry
                ? `${periodLabel(month)} · ${entry.transactions} movimento(s) · ${entry.files.join(", ")}`
                : `${periodLabel(month)} · extrato não importado`
            }
            className={
              entry
                ? "grid h-7 w-14 place-items-center rounded-md bg-positive/15 text-[10px] font-medium text-positive"
                : "grid h-7 w-14 place-items-center rounded-md border border-dashed border-negative/50 bg-negative/5 text-[10px] font-medium text-negative"
            }
          >
            {periodLabel(month)}
          </span>
        );
      })}
    </div>
  );
}

function AccountCard({ account }: { account: CoverageAccount }) {
  return (
    <li className="rounded-xl border border-line bg-surface-raised/50 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-2 font-medium">
          {account.is_complete ? (
            <CheckCircle2 size={15} className="text-positive" aria-hidden />
          ) : (
            <AlertTriangle size={15} className="text-warning" aria-hidden />
          )}
          {account.broker}
          <Badge>{account.currency}</Badge>
          <span className="text-xs font-normal text-ink-muted">conta {account.account_ref}</span>
        </span>
        <span className="text-xs text-ink-muted">
          {account.statements} extrato(s) · {periodLabel(account.first_month ?? "")} →{" "}
          {periodLabel(account.last_month ?? "")}
        </span>
      </div>

      <MonthGrid account={account} />

      {account.missing_months.length ? (
        <p className="mt-3 flex items-start gap-2 text-sm text-negative">
          <FileWarning size={15} className="mt-0.5 shrink-0" aria-hidden />
          <span>
            Faltam {account.missing_months.length} extrato(s):{" "}
            <span className="font-medium">{account.missing_months.map(periodLabel).join(", ")}</span>
          </span>
        </p>
      ) : null}

      {account.balance_breaks.length ? (
        <div className="mt-3 space-y-1 text-sm text-warning">
          {account.balance_breaks.map((issue) => (
            <p key={issue.month} className="flex items-start gap-2">
              <Scale size={15} className="mt-0.5 shrink-0" aria-hidden />
              <span>
                {periodLabel(issue.previous_month)} fechou em {issue.previous_closing} e{" "}
                {periodLabel(issue.month)} abriu em {issue.opening}, diferença de {issue.difference}.
                Provavelmente há um extrato faltando entre os dois.
              </span>
            </p>
          ))}
        </div>
      ) : null}

      {account.position_drift.length ? (
        <div className="mt-3 text-sm text-warning">
          <p className="flex items-start gap-2">
            <AlertTriangle size={15} className="mt-0.5 shrink-0" aria-hidden />
            <span>
              Posições calculadas não batem com as informadas no último extrato, indício de
              movimentação não importada:
            </span>
          </p>
          <ul className="mt-1.5 space-y-0.5 pl-6 text-ink-secondary">
            {account.position_drift.slice(0, 8).map((drift) => (
              <li key={drift.ticker} className="tnum">
                <span className="font-medium text-ink">{drift.ticker}</span>: extrato {drift.reported} ·
                calculado {drift.computed} ({drift.difference})
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </li>
  );
}

export default function StatementCoverage() {
  const coverage = useQuery({ queryKey: ["import-coverage"], queryFn: api.coverage });
  if (coverage.isLoading) return <Skeleton className="h-48 w-full" />;
  // Distinct from "no statements": a fetch failure must not claim the archive
  // is empty.
  if (coverage.isError) {
    return (
      <Card className="p-5">
        <SectionTitle
          title="Cobertura dos extratos"
          subtitle="Meses importados por conta, com indicação do que parece faltar"
        />
        <ErrorState error={coverage.error} retry={() => coverage.refetch()} />
      </Card>
    );
  }
  const accounts = coverage.data?.accounts ?? [];

  return (
    <Card className="p-5">
      <SectionTitle
        title="Cobertura dos extratos"
        subtitle="Meses importados por conta, com indicação do que parece faltar"
      />
      {!accounts.length ? (
        <EmptyState
          icon={FileWarning}
          title="Nenhum extrato de corretora importado"
          description="Envie os PDFs mensais da Avenue ou da Nomad para acompanhar a cobertura."
        />
      ) : (
        <ul className="space-y-3">
          {accounts.map((account) => (
            <AccountCard key={`${account.broker}-${account.account_ref}`} account={account} />
          ))}
        </ul>
      )}
    </Card>
  );
}
