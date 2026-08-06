/** Public portfolios of famous investors, one tab each, from SEC 13F filings.
 *
 * The backend talks to EDGAR and caches for a day; this page just renders the
 * snapshot and repeats its limits honestly (quarterly, up to 45 days late,
 * US-listed longs only). Ticker links go to the asset page — watch-only
 * creation makes any of them explorable.
 */
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { ExternalLink, Landmark, TrendingDown, TrendingUp } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";

import { Badge, Card, ErrorState, SectionTitle, Skeleton, StatTile, Tabs } from "@/components/ui";
import { api, type InvestorWallet } from "@/lib/api";
import { money, percent, quantity, shortDate } from "@/lib/format";

const inUsd = (value: number, decimals?: number) =>
  money(value, { currency: "USD", compact: true, decimals: decimals ?? 1 });

/** "2026-03-31" -> "1º tri 2026". */
function quarterLabel(iso: string): string {
  const [year, month] = iso.split("-").map(Number);
  return `${Math.ceil(month / 3)}º tri ${year}`;
}

const CHANGE_BADGE: Record<
  NonNullable<InvestorWallet["holdings"][number]["change"]>,
  { label: string; tone: "accent" | "positive" | "warning" | "neutral" } | null
> = {
  new: { label: "nova", tone: "accent" },
  increased: { label: "aumentou", tone: "positive" },
  reduced: { label: "reduziu", tone: "warning" },
  unchanged: null, // holding steady is the default story; a badge would be noise
};

function WalletView({ slug }: { slug: string }) {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["investor-wallet", slug],
    queryFn: () => api.investorWallet(slug),
    staleTime: 60 * 60_000,
    retry: 1,
  });

  if (isError) return <ErrorState error={error} retry={() => refetch()} />;
  if (isLoading || !data) {
    return (
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <Skeleton key={index} className="h-28" />
          ))}
        </div>
        <Skeleton className="h-96 w-full" />
        <p className="text-center text-xs text-ink-muted">
          Baixando o 13F na SEC — a primeira visita a cada gestor leva alguns segundos…
        </p>
      </div>
    );
  }

  const top = data.holdings.slice(0, 40);
  const maxPct = top[0]?.pct ?? 0;

  return (
    <div className="space-y-6">
      <p className="max-w-3xl text-sm text-ink-secondary">{data.description}</p>

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile label="Carteira divulgada" value={inUsd(data.total_value)} tone="accent" hint="ações listadas nos EUA" />
        <StatTile label="Posições" value={String(data.positions)} hint={`${data.exits.length} saídas no trimestre`} />
        <StatTile label="Trimestre" value={quarterLabel(data.quarter)} hint={`retrato de ${shortDate(data.quarter)}`} />
        <StatTile label="Enviado à SEC" value={shortDate(data.filed_at)} hint="formulário 13F-HR" />
      </section>

      <Card className="p-5">
        <SectionTitle
          title="Posições"
          subtitle={
            data.previous_quarter
              ? `Mudanças comparadas ao trimestre anterior (${quarterLabel(data.previous_quarter)})`
              : "Primeiro trimestre disponível — sem base de comparação"
          }
        />
        <div className="-mx-2 overflow-x-auto">
          <table className="w-full min-w-[680px] text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-ink-muted">
              <tr>
                <th className="w-8 px-3 py-2">#</th>
                <th className="px-3 py-2">Empresa</th>
                <th className="w-[260px] px-3 py-2">% da carteira</th>
                <th className="px-3 py-2 text-right">Valor</th>
                <th className="px-3 py-2 text-right">Ações</th>
                <th className="px-3 py-2">Mudança</th>
              </tr>
            </thead>
            <tbody>
              {top.map((holding, index) => {
                const badge = holding.change ? CHANGE_BADGE[holding.change] : null;
                return (
                  <tr key={holding.cusip} className="table-row border-t border-line/60">
                    <td className="px-3 py-2 text-ink-muted">{index + 1}</td>
                    <td className="px-3 py-2">
                      <span className="flex items-center gap-2">
                        {holding.ticker ? (
                          <Link
                            to={`/ativos/${holding.ticker}`}
                            className="font-medium text-ink hover:text-accent"
                            title={`Abrir ${holding.ticker}`}
                          >
                            {holding.ticker}
                          </Link>
                        ) : null}
                        <span className={clsx("truncate", holding.ticker ? "text-xs text-ink-muted" : "font-medium text-ink")}>
                          {holding.issuer}
                        </span>
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <span className="flex items-center gap-2">
                        <span className="h-1.5 w-full max-w-[170px] overflow-hidden rounded-full bg-surface-hover">
                          <span
                            className="block h-full rounded-full bg-accent"
                            style={{ width: `${maxPct ? (holding.pct / maxPct) * 100 : 0}%` }}
                            aria-hidden
                          />
                        </span>
                        <span className="tnum w-14 shrink-0 text-right text-ink-secondary">
                          {percent(holding.pct, 1)}
                        </span>
                      </span>
                    </td>
                    <td className="tnum px-3 py-2 text-right text-ink-secondary">{inUsd(holding.value)}</td>
                    <td className="tnum px-3 py-2 text-right text-ink-muted">{quantity(holding.shares)}</td>
                    <td className="px-3 py-2">{badge ? <Badge tone={badge.tone}>{badge.label}</Badge> : null}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {data.holdings.length > top.length ? (
          <p className="mt-3 text-xs text-ink-muted">
            Mostrando as {top.length} maiores de {data.holdings.length} posições.
          </p>
        ) : null}
      </Card>

      {data.exits.length ? (
        <Card className="p-5">
          <SectionTitle
            title="Saíram da carteira"
            subtitle="Posições do trimestre anterior zeradas neste"
          />
          <div className="flex flex-wrap gap-2">
            {data.exits.slice(0, 24).map((exit) => (
              <span key={exit.issuer} className="chip">
                <TrendingDown size={12} className="text-negative" aria-hidden />
                {exit.ticker ?? exit.issuer}
              </span>
            ))}
          </div>
        </Card>
      ) : null}

      <p className="flex flex-wrap items-center gap-x-2 text-xs text-ink-muted">
        <span>{data.caveats}</span>
        <a
          href={`https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${data.cik}&type=13F&dateb=&owner=include&count=10`}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 text-accent hover:underline"
        >
          Fonte: SEC EDGAR <ExternalLink size={11} aria-hidden />
        </a>
      </p>
    </div>
  );
}

export default function CarteirasPublicas() {
  const registry = useQuery({ queryKey: ["investors"], queryFn: api.investors, staleTime: 24 * 60 * 60_000 });
  const [params, setParams] = useSearchParams();
  const investors = registry.data ?? [];
  const requested = params.get("investidor");
  const active = investors.some((item) => item.slug === requested)
    ? (requested as string)
    : investors[0]?.slug;

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">Ferramentas</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Carteiras públicas</h1>
        <p className="mt-2 max-w-3xl text-sm text-ink-secondary">
          O que os maiores investidores do mundo têm em carteira, direto dos formulários 13F que
          todo gestor com mais de US$ 100 mi é obrigado a entregar à SEC a cada trimestre.
        </p>
      </header>

      {registry.isError ? (
        <ErrorState error={registry.error} retry={() => registry.refetch()} />
      ) : registry.isLoading || !active ? (
        <Skeleton className="h-10 w-full" />
      ) : (
        <>
          <Tabs
            value={active}
            onChange={(slug) => setParams(slug === investors[0]?.slug ? {} : { investidor: slug }, { replace: true })}
            options={investors.map((item) => ({ value: item.slug, label: item.manager }))}
          />
          <p className="-mt-3 flex items-center gap-1.5 text-xs text-ink-muted">
            <Landmark size={12} aria-hidden />
            {investors.find((item) => item.slug === active)?.fund}
            <TrendingUp size={12} className="ml-2" aria-hidden />
            atualizado a cada trimestre
          </p>
          <WalletView slug={active} />
        </>
      )}
    </div>
  );
}
