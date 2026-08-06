/**
 * Wallet overview: headline numbers and the competition chart.
 *
 * The chart draws every AI wallet (not only the selected one) against CDI and
 * IBOV — the whole point is comparing the models — with the selected wallet's
 * line emphasized. The full audit trail lives in its own tab (HistoryTab).
 */
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { ChartFrame, ReturnLinesChart, type ReturnPoint, type ReturnSeries } from "@/components/charts";
import { Segmented, StatTile } from "@/components/ui";
import type { AiWalletDetail, AiWalletRange } from "@/lib/api";
import { api } from "@/lib/api";
import { BENCHMARK_STYLE, SERIES } from "@/lib/colors";
import { money, percent } from "@/lib/format";

export default function OverviewTab({ wallet }: { wallet: AiWalletDetail }) {
  const [range, setRange] = useState<AiWalletRange>("max");
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  const compareQ = useQuery({
    queryKey: ["ai-wallet-compare", range],
    queryFn: () => api.aiWalletCompare(range),
  });

  const { series, rows } = useMemo(() => {
    const data = compareQ.data;
    if (!data) return { series: [] as ReturnSeries[], rows: [] as ReturnPoint[] };
    let walletIndex = 0;
    const mapped: ReturnSeries[] = data.series.map((item) => {
      if (item.benchmark) {
        const style = BENCHMARK_STYLE[item.key] ?? { color: "#9aa3b2", dash: "4 4" };
        return { key: item.key, label: item.label, color: style.color, dash: style.dash };
      }
      const color = SERIES[walletIndex % SERIES.length];
      walletIndex += 1;
      return {
        key: item.key,
        label: item.model ? `${item.label} (${item.model})` : item.label,
        color,
        emphasis: item.wallet_id === wallet.id,
      };
    });
    return { series: mapped, rows: data.rows as ReturnPoint[] };
  }, [compareQ.data, wallet.id]);

  const totals = wallet.totals;
  const result = totals.value - totals.invested;

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile label="Valor da carteira" value={<span className="tnum">{money(totals.value)}</span>} hint={`caixa ${money(totals.cash)}`} />
        <StatTile label="Investido (virtual)" value={<span className="tnum">{money(totals.invested)}</span>} hint="R$ 10.000 por categoria gerada" />
        <StatTile
          label="Resultado"
          value={<span className="tnum">{money(result)}</span>}
          tone={result > 0 ? "positive" : result < 0 ? "negative" : "neutral"}
        />
        <StatTile
          label="Retorno"
          value={<span className="tnum">{totals.return_pct === null ? "—" : percent(totals.return_pct, 2, true)}</span>}
          tone={(totals.return_pct ?? 0) > 0 ? "positive" : (totals.return_pct ?? 0) < 0 ? "negative" : "neutral"}
          hint={totals.unpriced.length ? `${totals.unpriced.length} sem cotação (ao custo)` : undefined}
        />
      </div>

      <ChartFrame
        title="Competição entre carteiras"
        subtitle="Retorno acumulado de cada carteira IA contra CDI e Ibovespa"
        error={compareQ.isError ? compareQ.error : undefined}
        retry={() => compareQ.refetch()}
        height={340}
        action={
          <Segmented
            value={range}
            onChange={(next) => setRange(next)}
            options={[
              { value: "3m", label: "3m" },
              { value: "6m", label: "6m" },
              { value: "1y", label: "1a" },
              { value: "max", label: "Máx" },
            ]}
            size="sm"
          />
        }
        footer="Snapshots diários; a linha de cada carteira começa no dia da primeira geração."
      >
        <ReturnLinesChart
          data={rows}
          series={series}
          hidden={hidden}
          onToggle={(key) =>
            setHidden((current) => {
              const next = new Set(current);
              if (next.has(key)) next.delete(key);
              else next.add(key);
              return next;
            })
          }
          height={340}
        />
      </ChartFrame>
    </div>
  );
}
