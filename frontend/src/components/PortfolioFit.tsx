/**
 * "Como me comparo": the real portfolio against the market it was picked from.
 *
 * Three questions the screener alone cannot answer — which sectors this
 * portfolio over- and under-weights, where each holding ranks among its own
 * sector peers, and which sectors it has no exposure to at all.
 *
 * Holdings the universe does not cover (renda fixa, Tesouro, cripto, corretoras
 * estrangeiras) are named rather than dropped, so the percentages always say
 * what they are a percentage of.
 */
import { useQuery } from "@tanstack/react-query";
import { Scale } from "lucide-react";

import { api } from "@/lib/api";
import { percent } from "@/lib/format";
import { Badge, Card, EmptyState, ErrorState, SectionTitle, Skeleton } from "@/components/ui";

/** A percentile among sector peers, coloured only at the extremes. */
function Percentile({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) {
    return <span className="text-ink-muted">—</span>;
  }
  const tone = value >= 75 ? "text-positive" : value <= 25 ? "text-negative" : "text-ink-secondary";
  return <span className={`tnum ${tone}`}>{value}º</span>;
}

export default function PortfolioFitPanel() {
  const fitQ = useQuery({ queryKey: ["portfolio-fit"], queryFn: api.portfolioFit });

  if (fitQ.isError) return <ErrorState error={fitQ.error} retry={() => fitQ.refetch()} />;
  if (fitQ.isLoading || !fitQ.data) return <Skeleton className="h-80 w-full" />;

  const fit = fitQ.data;
  if (!fit.enabled || !fit.holdings?.length) {
    return (
      <Card className="p-5">
        <EmptyState
          icon={Scale}
          title="Nada para comparar ainda"
          description="A comparação cruza suas posições com o universo local. Baixe o universo e tenha ao menos uma posição em ação ou FII listado."
        />
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <Card className="p-5">
        <SectionTitle
          title="Peso por setor"
          subtitle="Sua carteira contra o mercado, ponderado por valor de mercado"
          action={
            fit.coberto_pct !== null && fit.coberto_pct !== undefined ? (
              <Badge tone="neutral">{percent(fit.coberto_pct, 0)} da carteira comparada</Badge>
            ) : null
          }
        />
        <div className="overflow-x-auto">
          <table className="w-full min-w-[520px] text-sm">
            <thead>
              <tr className="border-b border-line text-xs text-ink-muted">
                <th className="px-3 py-2 text-left font-medium">Setor</th>
                <th className="px-3 py-2 text-right font-medium">Meu peso</th>
                <th className="px-3 py-2 text-right font-medium">Mercado</th>
                <th className="px-3 py-2 text-right font-medium">Diferença</th>
              </tr>
            </thead>
            <tbody>
              {fit.sectors?.map((row) => (
                <tr key={row.setor} className="table-row">
                  <td className="px-3 py-2">{row.setor}</td>
                  <td className="tnum px-3 py-2 text-right">{percent(row.meu_peso_pct ?? 0, 1)}</td>
                  <td className="tnum px-3 py-2 text-right text-ink-secondary">
                    {percent(row.peso_de_mercado_pct ?? 0, 1)}
                  </td>
                  <td
                    className={`tnum px-3 py-2 text-right ${
                      (row.diferenca_pp ?? 0) > 0 ? "text-positive" : "text-negative"
                    }`}
                  >
                    {(row.diferenca_pp ?? 0) > 0 ? "+" : ""}
                    {(row.diferenca_pp ?? 0).toFixed(1)} pp
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {fit.sem_setor?.length ? (
          <p className="mt-3 text-xs text-ink-muted">
            Sem setor comparável (FIIs, ETFs e BDRs não têm setor na CVM, e os pesos de mercado
            acima são de companhias):{" "}
            <span className="text-ink-secondary">{fit.sem_setor.join(", ")}</span>. Os indicadores
            deles aparecem na tabela abaixo.
          </p>
        ) : null}
        {fit.gaps?.length ? (
          <p className="mt-3 text-xs text-ink-muted">
            Sem exposição a: <span className="text-ink-secondary">{fit.gaps.join(", ")}</span>. Não é
            uma recomendação — um setor pode estar fora da carteira de propósito.
          </p>
        ) : null}
      </Card>

      <Card className="overflow-hidden p-0">
        <div className="p-5 pb-0">
          <SectionTitle
            title="Suas posições entre os pares"
            subtitle="Percentil dentro do próprio setor — 100º é o melhor do setor naquele indicador. UDM = últimos doze meses até o trimestre indicado."
          />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[700px] text-sm">
            <thead>
              <tr className="border-b border-line text-xs text-ink-muted">
                <th className="px-4 py-3 text-left font-medium">Ativo</th>
                <th className="px-4 py-3 text-right font-medium">Peso</th>
                <th className="px-4 py-3 text-right font-medium">P/L</th>
                <th className="px-4 py-3 text-right font-medium">P/VP</th>
                <th className="px-4 py-3 text-right font-medium">ROE</th>
                <th className="px-4 py-3 text-right font-medium">DY</th>
              </tr>
            </thead>
            <tbody>
              {fit.holdings.map((row) => (
                <tr key={row.ticker} className="table-row">
                  <td className="px-4 py-3">
                    <span className="font-medium">{row.ticker}</span>
                    <span className="mt-0.5 block truncate text-xs text-ink-muted">
                      {row.setor ?? "—"}
                      {row.exercicio_dos_fundamentos ? ` · ${row.exercicio_dos_fundamentos}` : ""}
                    </span>
                  </td>
                  <td className="tnum px-4 py-3 text-right">{percent(row.peso_pct ?? 0, 1)}</td>
                  {(["p_l", "p_vp", "roe_pct", "dividend_yield_pct"] as const).map((key) => (
                    <td key={key} className="px-4 py-3 text-right">
                      <span className="tnum block">
                        {row[key] === null ? (
                          <span className="text-ink-muted">sem dado</span>
                        ) : key === "roe_pct" || key === "dividend_yield_pct" ? (
                          percent(row[key], 1)
                        ) : (
                          (row[key] as number).toFixed(2)
                        )}
                      </span>
                      <span className="block text-[10px]">
                        <Percentile value={row.percentis_no_setor?.[key]} />
                      </span>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {fit.outside?.length ? (
        <Card className="p-5">
          <SectionTitle
            title="Fora do universo"
            subtitle="Posições que a comparação acima não cobre"
          />
          <p className="text-sm text-ink-secondary">{fit.outside.join(", ")}</p>
          <p className="mt-2 text-xs text-ink-muted">
            Renda fixa, Tesouro, cripto e papéis negociados fora da B3 não têm equivalente no
            universo local, então ficam de fora dos pesos e percentis — e não do seu patrimônio.
          </p>
        </Card>
      ) : null}
    </div>
  );
}
