/** Compound interest calculator seeded with the portfolio's own numbers.
 *
 * The starting value defaults to the current net worth and the monthly
 * contribution to the actual average of the last twelve months, so the first
 * projection on screen is "my portfolio, if I keep going" — every field stays
 * editable for what-if scenarios.
 */
import { useQuery } from "@tanstack/react-query";
import { Coins, PiggyBank, Sparkles, TrendingUp, Wallet } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { ChartFrame, ProjectionBreakdownBars, ProjectionChart, type ProjectionPoint } from "@/components/charts";
import { Card, Select, StatTile } from "@/components/ui";
import { api } from "@/lib/api";
import { money, percent } from "@/lib/format";

type RateUnit = "aa" | "am";
type PeriodUnit = "anos" | "meses";

/** Parse a pt-BR or plain decimal: "1.234,56" → 1234.56, "10.5" → 10.5. */
function parseNumber(raw: string): number {
  const text = raw.trim();
  if (!text) return 0;
  const normalized = text.includes(",") ? text.replace(/\./g, "").replace(",", ".") : text;
  const value = Number(normalized);
  return Number.isFinite(value) && value >= 0 ? value : 0;
}

/** Format a number for an editable field (comma decimal, no grouping). */
function fieldValue(value: number): string {
  return (Math.round(value * 100) / 100).toString().replace(".", ",");
}

function Field({
  id,
  label,
  value,
  onChange,
  placeholder,
  prefix,
  suffix,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  prefix?: string;
  suffix?: ReactNode;
}) {
  return (
    <div>
      <label className="mb-1.5 block text-xs font-medium text-ink-muted" htmlFor={id}>
        {label}
      </label>
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          {prefix ? (
            <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-sm text-ink-muted">
              {prefix}
            </span>
          ) : null}
          <input
            id={id}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            inputMode="decimal"
            placeholder={placeholder}
            className={prefix ? "input pl-9" : "input"}
          />
        </div>
        {suffix}
      </div>
    </div>
  );
}

export default function Calculadora() {
  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview });
  const contributions = useQuery({ queryKey: ["contributions"], queryFn: () => api.contributions("month") });

  const [initial, setInitial] = useState<string | null>(null);
  const [monthly, setMonthly] = useState<string | null>(null);
  const [rate, setRate] = useState("10");
  const [rateUnit, setRateUnit] = useState<RateUnit>("aa");
  const [period, setPeriod] = useState("10");
  const [periodUnit, setPeriodUnit] = useState<PeriodUnit>("anos");

  // Seed each field once from the portfolio; after that the user owns it.
  useEffect(() => {
    if (initial === null && overview.data) setInitial(fieldValue(overview.data.market_value ?? 0));
  }, [overview.data, initial]);
  useEffect(() => {
    if (monthly !== null || !contributions.data) return;
    const recent = contributions.data.slice(-12).map((point) => point.net);
    const average = recent.length ? recent.reduce((sum, value) => sum + value, 0) / recent.length : 0;
    setMonthly(fieldValue(Math.max(Math.round(average), 0)));
  }, [contributions.data, monthly]);

  const simulation = useMemo(() => {
    const start = parseNumber(initial ?? "");
    const contribution = parseNumber(monthly ?? "");
    const rateValue = parseNumber(rate);
    const monthlyRate = rateUnit === "aa" ? Math.pow(1 + rateValue / 100, 1 / 12) - 1 : rateValue / 100;
    const requested = Math.floor(parseNumber(period)) * (periodUnit === "anos" ? 12 : 1);
    // 100-year cap: enough for any plan, and keeps absurd input from freezing the page.
    const months = Math.min(requested, 1200);

    const base = new Date();
    const periodOf = (offset: number) => {
      const stamp = new Date(base.getFullYear(), base.getMonth() + offset, 1);
      return `${stamp.getFullYear()}-${String(stamp.getMonth() + 1).padStart(2, "0")}`;
    };

    let total = start;
    let invested = start;
    const points: ProjectionPoint[] = [{ period: periodOf(0), total, invested, interest: 0 }];
    for (let month = 1; month <= months; month += 1) {
      total = total * (1 + monthlyRate) + contribution;
      invested += contribution;
      points.push({ period: periodOf(month), total, invested, interest: total - invested });
    }

    // One row per completed year (plus the final month when it isn't a full
    // year) — the digestible view; the chart already shows every month.
    const yearly = points.filter((_, index) => index > 0 && index % 12 === 0);
    const last = points[points.length - 1];
    if (months > 0 && months % 12 !== 0) yearly.push(last);
    const yearlyRows = yearly.map((point) => ({ ...point, period: point.period.slice(0, 4) }));

    return { points, yearlyRows, last, monthlyRate, months };
  }, [initial, monthly, rate, rateUnit, period, periodUnit]);

  const { points, yearlyRows, last, monthlyRate, months } = simulation;
  const interestShare = last.total > 0 ? (last.interest / last.total) * 100 : 0;

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">Simulação</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Calculadora de juros compostos</h1>
      </header>

      <Card className="p-5" hover={false}>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <Field
            id="calc-initial"
            label="Valor inicial"
            prefix="R$"
            value={initial ?? ""}
            onChange={setInitial}
            placeholder={overview.isLoading ? "carregando…" : "0,00"}
          />
          <Field
            id="calc-monthly"
            label="Aporte mensal"
            prefix="R$"
            value={monthly ?? ""}
            onChange={setMonthly}
            placeholder={contributions.isLoading ? "carregando…" : "0,00"}
          />
          <Field
            id="calc-rate"
            label="Taxa de juros"
            value={rate}
            onChange={setRate}
            placeholder="10"
            suffix={
              <Select
                ariaLabel="Período da taxa"
                className="w-auto min-w-[92px]"
                value={rateUnit}
                onChange={(next) => setRateUnit(next as RateUnit)}
                options={[
                  { value: "aa", label: "% a.a." },
                  { value: "am", label: "% a.m." },
                ]}
              />
            }
          />
          <Field
            id="calc-period"
            label="Período"
            value={period}
            onChange={setPeriod}
            placeholder="10"
            suffix={
              <Select
                ariaLabel="Unidade do período"
                className="w-auto min-w-[92px]"
                value={periodUnit}
                onChange={(next) => setPeriodUnit(next as PeriodUnit)}
                options={[
                  { value: "anos", label: "anos" },
                  { value: "meses", label: "meses" },
                ]}
              />
            }
          />
        </div>
        <p className="mt-3 text-xs text-ink-muted">
          Valor inicial e aporte começam com os números da sua carteira (patrimônio atual e média de aportes dos
          últimos 12 meses) — edite à vontade. Taxa anual convertida por equivalência: {percent(monthlyRate * 100, 3)}{" "}
          ao mês.
        </p>
      </Card>

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile label="Patrimônio final" value={money(last.total)} icon={Sparkles} tone="accent" hint={`em ${months} meses`} />
        <StatTile label="Total investido" value={money(last.invested)} icon={Wallet} hint="valor inicial + aportes" />
        <StatTile
          label="Total em juros"
          value={money(last.interest)}
          icon={TrendingUp}
          tone="positive"
          hint={`${percent(interestShare, 1)} do patrimônio final`}
        />
        <StatTile
          label="Renda mensal ao final"
          value={money(last.total * monthlyRate)}
          icon={Coins}
          tone="positive"
          hint="um mês de rendimento na taxa informada"
        />
      </section>

      <ChartFrame
        title="Projeção do patrimônio"
        subtitle="Crescimento mês a mês comparado ao capital investido"
        height={340}
        table={
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-surface text-left text-xs uppercase tracking-wide text-ink-muted">
              <tr>
                <th className="px-2 py-2">Ano</th>
                <th className="px-2 py-2 text-right">Total investido</th>
                <th className="px-2 py-2 text-right">Juros acumulados</th>
                <th className="px-2 py-2 text-right">Patrimônio</th>
              </tr>
            </thead>
            <tbody>
              {yearlyRows.map((row) => (
                <tr key={row.period} className="table-row">
                  <td className="px-2 py-1.5">{row.period}</td>
                  <td className="tnum px-2 py-1.5 text-right">{money(row.invested)}</td>
                  <td className="tnum px-2 py-1.5 text-right">{money(row.interest)}</td>
                  <td className="tnum px-2 py-1.5 text-right font-medium">{money(row.total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        }
      >
        <ProjectionChart data={points} height={340} />
      </ChartFrame>

      <ChartFrame
        title="Composição ano a ano"
        subtitle="Quanto do patrimônio é aporte e quanto são juros — o efeito bola de neve"
        height={300}
        footer={
          <span>
            <PiggyBank size={13} className="mr-1 inline-block" aria-hidden />
            Juros compostos sobre {money(parseNumber(initial ?? ""))} iniciais e aportes de {money(parseNumber(monthly ?? ""))}
            /mês — sem considerar inflação nem imposto de renda.
          </span>
        }
      >
        <ProjectionBreakdownBars data={yearlyRows} height={300} />
      </ChartFrame>
    </div>
  );
}
