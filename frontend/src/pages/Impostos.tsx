/**
 * DARF mensal — the monthly tax on sales (apuração mensal / DARF 6015).
 *
 * The page answers two questions before anything else, because they are the
 * ones that lead to an action:
 *
 *  * **"Can I still sell this month without paying tax?"** — the month in
 *    progress gets its own panel: ações sold against the R$ 20.000 exemption as
 *    a bar, what still fits, and the tax if the month closed today.
 *  * **"Do I owe anything, and how do I pay it?"** — every unpaid DARF sits
 *    beside it with the data the Receita's form asks for, copyable in one
 *    click, and a way to mark it paid (the ledger cannot see a payment).
 *
 * Everything else is history: months with activity only, each opening into the
 * regimes that actually moved. Caveats are one compact box, not banners — one
 * of them applies to every B3 user forever and must not shout. Day trade is
 * entered by hand: the B3 export does not mark it, and guessing it from
 * same-day movements would be the quiet kind of wrong.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { AlertTriangle, CheckCircle2, ChevronDown, Copy, Info, Plus, Receipt } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { useToast } from "@/components/Toast";
import { Badge, Card, ErrorState, Modal, SectionTitle, Segmented, Skeleton } from "@/components/ui";
import { api, ApiError, type TaxMonth, type TaxSheet } from "@/lib/api";
import { money, percent, shortDate } from "@/lib/format";

const n = (value: string | number | null | undefined) => Number(value ?? 0);

function monthName(period: string, style: "long" | "short" = "long"): string {
  const [year, month] = period.split("-").map(Number);
  const label = new Intl.DateTimeFormat("pt-BR", { month: style, year: "numeric" }).format(
    new Date(year, month - 1, 1),
  );
  return label.charAt(0).toUpperCase() + label.slice(1).replace(".", "");
}

/** Last day of the month: the "período de apuração" the DARF form asks for. */
function periodEnd(period: string): string {
  const [year, month] = period.split("-").map(Number);
  return shortDate(new Date(year, month, 0));
}

type Tone = "neutral" | "positive" | "negative" | "accent" | "warning";

const STATUS: Record<TaxMonth["status"], { label: string; tone: Tone }> = {
  em_andamento: { label: "Em andamento", tone: "accent" },
  a_pagar: { label: "A pagar", tone: "warning" },
  // Unconfirmed, not "late": the ledger cannot know whether it was paid.
  vencido: { label: "Venceu", tone: "negative" },
  pago: { label: "Pago", tone: "positive" },
  acumulado: { label: "Abaixo de R$ 10", tone: "neutral" },
  sem_imposto: { label: "Sem imposto", tone: "neutral" },
};

/** Whether a closed month has anything worth a row in the history. */
function hasActivity(month: TaxMonth): boolean {
  return (
    n(month.sales.acoes) + n(month.sales.outras_comuns) + n(month.sales.fii) > 0 ||
    n(month.results.acoes) !== 0 ||
    n(month.results.outras_comuns) !== 0 ||
    n(month.results.fii) !== 0 ||
    month.day_trade.informed ||
    n(month.gross_tax) > 0 ||
    n(month.carried_in) > 0
  );
}

/** "1.234,56", "-80,5" or "1234.56" → "1234.56"; null when it is not a number. */
function parseAmount(raw: string): string | null {
  const text = raw.trim().replace(/\s|R\$/g, "");
  if (!text) return null;
  const normalised = text.includes(",") ? text.replace(/\./g, "").replace(",", ".") : text;
  return /^-?\d+(\.\d{1,2})?$/.test(normalised) ? normalised : null;
}

/** The day-trade dialog's subject: a month, and what was already entered for it. */
type DayTradeTarget = { period: string; informed: boolean; result: number; irrf: number } | null;

function dayTradeTarget(month: TaxMonth): NonNullable<DayTradeTarget> {
  return {
    period: month.period,
    informed: month.day_trade.informed,
    result: n(month.results.day_trade),
    irrf: n(month.day_trade.irrf),
  };
}

export default function Impostos() {
  const sheetQ = useQuery({ queryKey: ["monthly-taxes"], queryFn: api.monthlyTaxes });
  const sheet = sheetQ.data;
  const [dayTrade, setDayTrade] = useState<DayTradeTarget>(null);

  if (sheetQ.isError) return <ErrorState error={sheetQ.error} retry={() => sheetQ.refetch()} />;

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">Impostos</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">DARF mensal</h1>
        <p className="mt-2 max-w-2xl text-sm text-ink-secondary">
          O imposto de renda sobre as suas vendas, mês a mês: quanto ainda dá para vender isento, o
          que pagar no DARF e até quando. Uma estimativa para conferir, montada a partir das suas
          movimentações.
        </p>
      </header>

      {sheetQ.isLoading || !sheet ? (
        <Skeleton className="h-96 w-full" />
      ) : (
        <>
          <div className="grid gap-4 lg:grid-cols-2">
            <ThisMonth sheet={sheet} />
            <Pending sheet={sheet} />
          </div>
          <Attention sheet={sheet} onDayTrade={setDayTrade} />
          <History sheet={sheet} onDayTrade={setDayTrade} />
          <About sheet={sheet} />
        </>
      )}

      <DayTradeModal target={dayTrade} onClose={() => setDayTrade(null)} />
    </div>
  );
}

/* --------------------------------------------------------------- this month */

function ThisMonth({ sheet }: { sheet: TaxSheet }) {
  const month = sheet.months.find((row) => row.status === "em_andamento");
  const limit = n(sheet.rules.exemption_limit);
  const sold = n(month?.sales.acoes);
  const ratio = limit ? Math.min(sold / limit, 1) : 0;
  const over = sold > limit;
  const near = !over && ratio >= 0.8;
  const result = n(month?.results.acoes) + n(month?.results.outras_comuns) + n(month?.results.fii);
  const taxNow = n(month?.gross_tax);

  return (
    <Card className="p-5">
      <p className="text-[13px] font-medium text-ink-muted">
        {month ? monthName(month.period) : "Este mês"} · em andamento
      </p>
      <p className="mt-3 text-xs text-ink-muted">Vendas de ações no mês</p>
      <p className="tnum mt-0.5 text-2xl font-semibold tracking-tight">{money(sold)}</p>

      <div
        className="mt-3 h-2 overflow-hidden rounded-full bg-surface-hover"
        role="progressbar"
        aria-label="Vendas de ações em relação ao limite de isenção"
        aria-valuemin={0}
        aria-valuemax={limit}
        aria-valuenow={sold}
      >
        <div
          className={clsx(
            "h-full rounded-full transition-[width] duration-500",
            over ? "bg-negative" : near ? "bg-warning" : "bg-accent",
          )}
          style={{ width: `${Math.max(ratio * 100, sold > 0 ? 2 : 0)}%` }}
        />
      </div>
      <div className="mt-1.5 flex justify-between text-xs text-ink-muted">
        <span>R$ 0</span>
        <span>limite de isenção {money(limit)}</span>
      </div>

      <p
        className={clsx(
          "mt-3 text-sm",
          over ? "text-negative" : near ? "text-warning" : "text-ink-secondary",
        )}
      >
        {over
          ? "Limite ultrapassado: o lucro em ações deste mês é tributável."
          : `Ainda cabem ${money(limit - sold)} em vendas de ações com lucro isento.`}
      </p>

      <dl className="mt-4 grid grid-cols-2 gap-3 border-t border-line pt-4 text-sm">
        <div>
          <dt className="text-xs text-ink-muted">Resultado até agora</dt>
          <dd
            className={clsx(
              "tnum mt-0.5 font-medium",
              result > 0 ? "text-positive" : result < 0 ? "text-negative" : "text-ink",
            )}
          >
            {money(result)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-ink-muted">Imposto se o mês fechasse hoje</dt>
          <dd className="tnum mt-0.5 font-medium">{money(taxNow)}</dd>
        </div>
      </dl>
      <p className="mt-3 text-xs text-ink-muted">
        A isenção vale só para ações e units. ETFs, BDRs e FIIs pagam sobre qualquer lucro.
      </p>
    </Card>
  );
}

/* ------------------------------------------------------------------ pending */

function darfText(month: TaxMonth): string {
  return [
    `Código da receita: 6015`,
    `Período de apuração: ${periodEnd(month.period)}`,
    `Vencimento: ${shortDate(month.due_date)}`,
    `Valor principal: ${money(month.darf).replace(/\u00a0/g, " ")}`,
  ].join("\n");
}

function Pending({ sheet }: { sheet: TaxSheet }) {
  // Overdue and unconfirmed counts as owed: a page that quietly dropped a
  // past-due DARF would say "nothing to pay" when it may not be true.
  const due = sheet.months
    .filter((month) => month.status === "a_pagar" || month.status === "vencido")
    .sort((a, b) => a.due_date.localeCompare(b.due_date));
  const total = due.reduce((sum, month) => sum + n(month.darf), 0);
  const latest = sheet.months[0]?.losses_after;
  const losses = latest ? n(latest.common) + n(latest.fii) + n(latest.day_trade) : 0;

  return (
    <Card className="flex flex-col p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-[13px] font-medium text-ink-muted">DARF a pagar</p>
          <p
            className={clsx(
              "tnum mt-2 text-2xl font-semibold tracking-tight",
              total > 0 ? "text-ink" : "text-positive",
            )}
          >
            {money(total)}
          </p>
        </div>
        <span
          className={clsx(
            "rounded-lg p-1.5",
            total > 0 ? "bg-warning/12 text-warning" : "bg-positive/10 text-positive",
          )}
        >
          {total > 0 ? <Receipt size={16} aria-hidden /> : <CheckCircle2 size={16} aria-hidden />}
        </span>
      </div>

      {due.length ? (
        <ul className="mt-3 space-y-2">
          {due.map((month) => (
            <PendingRow key={month.period} month={month} />
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-sm text-ink-secondary">
          Nada pendente. Quando um mês fechar com imposto, o DARF aparece aqui com os dados para
          pagar.
        </p>
      )}

      <div className="mt-auto border-t border-line pt-4">
        <p className="mt-4 text-xs text-ink-muted">Prejuízo a compensar em vendas futuras</p>
        <p className="tnum mt-0.5 font-medium">{money(losses)}</p>
        {losses > 0 && latest ? (
          <p className="mt-0.5 text-xs text-ink-muted">
            {[
              n(latest.common) ? `ações, ETFs e BDRs ${money(latest.common)}` : null,
              n(latest.fii) ? `FIIs ${money(latest.fii)}` : null,
              n(latest.day_trade) ? `day trade ${money(latest.day_trade)}` : null,
            ]
              .filter(Boolean)
              .join(" · ")}
          </p>
        ) : null}
      </div>
    </Card>
  );
}

function PendingRow({ month }: { month: TaxMonth }) {
  const toast = useToast();
  const overdue = month.status === "vencido";
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(darfText(month));
      toast.success("Dados do DARF copiados.", {
        description: "Cole no Sicalc ou no app do banco.",
      });
    } catch (error) {
      toast.error("Não foi possível copiar.", error);
    }
  };
  return (
    <li className="rounded-xl border border-line p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="font-medium">{monthName(month.period)}</span>
        <span className="tnum font-semibold">{money(month.darf)}</span>
      </div>
      <p className={clsx("mt-0.5 text-xs", overdue ? "text-negative" : "text-ink-muted")}>
        {overdue
          ? `Venceu em ${shortDate(month.due_date)} — se ainda não pagou, gere o DARF no Sicalc (com multa e juros).`
          : `Vence em ${shortDate(month.due_date)} · código 6015`}
      </p>
      <div className="mt-2 flex flex-wrap gap-2">
        <button type="button" className="btn-ghost px-2.5 py-1.5 text-xs" onClick={copy}>
          <Copy size={13} aria-hidden /> Copiar dados
        </button>
        <PaidToggle month={month} />
      </div>
    </li>
  );
}

function PaidToggle({ month, compact }: { month: TaxMonth; compact?: boolean }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const paid = month.status === "pago";
  const mark = useMutation({
    mutationFn: () => api.setTaxPaid(month.period, !paid),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["monthly-taxes"] });
      toast.success(paid ? "Pagamento desmarcado." : "DARF marcado como pago.", {
        description: monthName(month.period),
      });
    },
    onError: (error) =>
      toast.error("Não foi possível atualizar.", error instanceof ApiError ? error : undefined),
  });
  return (
    <button
      type="button"
      className={clsx("px-2.5 py-1.5 text-xs", paid || compact ? "btn-ghost" : "btn-primary")}
      disabled={mark.isPending}
      onClick={(event) => {
        event.stopPropagation();
        mark.mutate();
      }}
    >
      {paid ? (
        "Desmarcar pagamento"
      ) : (
        <>
          <CheckCircle2 size={13} aria-hidden /> Marcar como pago
        </>
      )}
    </button>
  );
}

/* ---------------------------------------------------------------- attention */

function Attention({
  sheet,
  onDayTrade,
}: {
  sheet: TaxSheet;
  onDayTrade: (target: DayTradeTarget) => void;
}) {
  if (!sheet.warnings.length) return null;
  const byPeriod = new Map(sheet.months.map((month) => [month.period, month]));
  const trips = sheet.warnings.find((warning) => warning.kind === "possible_day_trade");
  const costs = sheet.warnings.find((warning) => warning.kind === "missing_costs");

  return (
    <Card className="p-4">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-warning/12 text-warning">
          <AlertTriangle size={15} aria-hidden />
        </span>
        <div className="min-w-0 flex-1 space-y-3">
          <p className="text-sm font-medium text-ink">Pontos de atenção</p>
          {trips ? (
            <div>
              <p className="text-sm text-ink-secondary">
                Possíveis day trades: o mesmo ativo foi comprado e vendido no dia. Se foram, informe
                o resultado do mês (20 %) — toque numa data.
              </p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {trips.items?.map((item) => {
                  const month = byPeriod.get(item.date.slice(0, 7));
                  return (
                    <button
                      key={`${item.date}-${item.ticker}`}
                      type="button"
                      onClick={() =>
                        onDayTrade(
                          month
                            ? dayTradeTarget(month)
                            : {
                                period: item.date.slice(0, 7),
                                informed: false,
                                result: 0,
                                irrf: 0,
                              },
                        )
                      }
                      className="rounded-md bg-warning/12 px-2 py-1 text-[11px] font-medium text-warning transition-colors hover:bg-warning/20"
                    >
                      {shortDate(item.date)} · {item.ticker}
                      {month?.day_trade.informed ? " ✓" : ""}
                    </button>
                  );
                })}
              </div>
            </div>
          ) : null}
          {costs ? (
            <p className="text-xs text-ink-muted">
              O extrato da B3 não traz corretagem e emolumentos, que reduzem o lucro — o imposto
              aqui pode estar um pouco acima do devido.
            </p>
          ) : null}
        </div>
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ history */

function History({
  sheet,
  onDayTrade,
}: {
  sheet: TaxSheet;
  onDayTrade: (target: DayTradeTarget) => void;
}) {
  const closed = useMemo(
    () => sheet.months.filter((month) => month.status !== "em_andamento" && hasActivity(month)),
    [sheet],
  );
  const years = useMemo(
    () => [...new Set(closed.map((month) => month.period.slice(0, 4)))],
    [closed],
  );
  const [year, setYear] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const shownYear = year && years.includes(year) ? year : years[0];
  const months = closed.filter((month) => month.period.startsWith(shownYear ?? ""));
  const yearTax = months.reduce((sum, month) => sum + n(month.gross_tax), 0);
  const yearDarf = months.reduce((sum, month) => sum + n(month.darf), 0);

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <SectionTitle
          title="Histórico"
          subtitle={
            shownYear
              ? `${shownYear}: imposto apurado ${money(yearTax)} · DARFs ${money(yearDarf)}`
              : "Os meses com vendas aparecem aqui"
          }
        />
        <div className="flex flex-wrap items-center gap-2">
          {years.length > 1 ? (
            <Segmented
              value={shownYear ?? ""}
              onChange={setYear}
              options={years.map((option) => ({ value: option, label: option }))}
            />
          ) : null}
          <button
            type="button"
            className="btn-ghost px-2.5 py-1.5 text-xs"
            onClick={() => onDayTrade({ period: "", informed: false, result: 0, irrf: 0 })}
          >
            <Plus size={13} aria-hidden /> Day trade
          </button>
        </div>
      </div>

      {months.length ? (
        <ul className="mt-3 divide-y divide-line">
          {months.map((month) => (
            <HistoryRow
              key={month.period}
              month={month}
              limit={n(sheet.rules.exemption_limit)}
              expanded={open === month.period}
              onToggle={() => setOpen(open === month.period ? null : month.period)}
              onDayTrade={() => onDayTrade(dayTradeTarget(month))}
            />
          ))}
        </ul>
      ) : (
        <p className="mt-4 text-sm text-ink-muted">
          Nenhum mês fechado com vendas ainda. Quando houver, cada um mostra a conta do imposto e o
          DARF.
        </p>
      )}
    </Card>
  );
}

function HistoryRow({
  month,
  limit,
  expanded,
  onToggle,
  onDayTrade,
}: {
  month: TaxMonth;
  limit: number;
  expanded: boolean;
  onToggle: () => void;
  onDayTrade: () => void;
}) {
  const status = STATUS[month.status];
  const sold = n(month.sales.acoes);
  return (
    <li>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex w-full items-center gap-3 py-3 text-left"
      >
        <ChevronDown
          size={15}
          className={clsx("shrink-0 text-ink-muted transition-transform", expanded && "rotate-180")}
          aria-hidden
        />
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium">{monthName(month.period)}</span>
            <Badge tone={status.tone}>{status.label}</Badge>
          </span>
          <span className="mt-0.5 block text-xs text-ink-muted">
            {[
              sold ? `Ações ${money(sold)}${sold <= limit ? " (isento)" : ""}` : null,
              n(month.sales.outras_comuns) ? `ETFs/BDRs ${money(month.sales.outras_comuns)}` : null,
              n(month.sales.fii) ? `FIIs ${money(month.sales.fii)}` : null,
              month.day_trade.informed ? "day trade informado" : null,
            ]
              .filter(Boolean)
              .join(" · ") || "Sem vendas"}
          </span>
        </span>
        <span className="text-right">
          <span className="tnum block font-medium">{n(month.darf) ? money(month.darf) : "—"}</span>
          <span className="block text-xs text-ink-muted">
            {n(month.darf) ? `vence ${shortDate(month.due_date)}` : "DARF"}
          </span>
        </span>
      </button>
      {expanded ? <MonthDetail month={month} limit={limit} onDayTrade={onDayTrade} /> : null}
    </li>
  );
}

function Line({
  label,
  value,
  strong,
  muted,
}: {
  label: string;
  value: string;
  strong?: boolean;
  muted?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <span className={clsx("text-xs", muted ? "text-ink-muted" : "text-ink-secondary")}>
        {label}
      </span>
      <span className={clsx("tnum text-sm", strong ? "font-semibold text-ink" : "text-ink")}>
        {value}
      </span>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-muted">{title}</p>
      {children}
    </div>
  );
}

function MonthDetail({
  month,
  limit,
  onDayTrade,
}: {
  month: TaxMonth;
  limit: number;
  onDayTrade: () => void;
}) {
  const showCommon =
    n(month.sales.acoes) ||
    n(month.sales.outras_comuns) ||
    n(month.results.acoes) ||
    n(month.results.outras_comuns) ||
    n(month.common.loss_used);
  const showFii = n(month.sales.fii) || n(month.results.fii) || n(month.fii.loss_used);
  const sections = [showCommon, showFii, month.day_trade.informed].filter(Boolean).length + 1;

  return (
    <div
      className={clsx(
        "mb-4 grid gap-4 rounded-xl border border-line bg-surface-raised/40 p-4",
        sections >= 3 ? "md:grid-cols-3" : "md:grid-cols-2",
      )}
    >
      {showCommon ? (
        <Section title="Ações, ETFs e BDRs · 15 %">
          <Line label="Vendas de ações" value={money(month.sales.acoes)} />
          <Line label="Resultado em ações" value={money(month.results.acoes)} />
          {month.exempt && n(month.exempt_gain) > 0 ? (
            <Line
              label={`Isento (vendas ≤ ${money(limit)})`}
              value={money(month.exempt_gain)}
              muted
            />
          ) : null}
          {n(month.sales.outras_comuns) || n(month.results.outras_comuns) ? (
            <Line label="ETFs, BDRs e outros" value={money(month.results.outras_comuns)} />
          ) : null}
          {n(month.common.loss_used) ? (
            <Line label="Prejuízo compensado" value={`− ${money(month.common.loss_used)}`} muted />
          ) : null}
          <Line label="Imposto" value={money(month.common.tax)} strong />
        </Section>
      ) : null}

      {showFii ? (
        <Section title="FIIs · 20 %">
          <Line label="Vendas" value={money(month.sales.fii)} />
          <Line label="Resultado" value={money(month.results.fii)} />
          {n(month.fii.loss_used) ? (
            <Line label="Prejuízo compensado" value={`− ${money(month.fii.loss_used)}`} muted />
          ) : null}
          <Line label="Imposto" value={money(month.fii.tax)} strong />
        </Section>
      ) : null}

      {month.day_trade.informed ? (
        <Section title="Day trade · 20 %">
          <Line label="Resultado informado" value={money(month.results.day_trade)} />
          {n(month.day_trade.loss_used) ? (
            <Line
              label="Prejuízo compensado"
              value={`− ${money(month.day_trade.loss_used)}`}
              muted
            />
          ) : null}
          <Line label="Imposto" value={money(month.day_trade.tax)} strong />
        </Section>
      ) : null}

      <Section title="DARF">
        <Line label="Imposto do mês" value={money(month.gross_tax)} />
        {n(month.irrf_used) ? (
          <Line label="IRRF compensado (estimado)" value={`− ${money(month.irrf_used)}`} muted />
        ) : null}
        {n(month.carried_in) ? (
          <Line label="Saldo de meses anteriores" value={money(month.carried_in)} muted />
        ) : null}
        <Line label="A pagar" value={money(month.darf)} strong />
        <p className="mt-1.5 text-xs text-ink-muted">
          {month.status === "acumulado"
            ? "Abaixo de R$ 10: não se emite DARF; o valor soma ao mês seguinte."
            : n(month.darf)
              ? `Código 6015 · apuração ${periodEnd(month.period)} · vence ${shortDate(month.due_date)}`
              : "Nada a pagar neste mês."}
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          {n(month.darf) ? <PaidToggle month={month} compact /> : null}
          <button type="button" className="btn-ghost px-2.5 py-1.5 text-xs" onClick={onDayTrade}>
            {month.day_trade.informed ? "Editar day trade" : "Informar day trade"}
          </button>
        </div>
      </Section>
    </div>
  );
}

/* ---------------------------------------------------------------- day trade */

function DayTradeModal({ target, onClose }: { target: DayTradeTarget; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [period, setPeriod] = useState("");
  const [result, setResult] = useState("");
  const [irrf, setIrrf] = useState("");
  const [seenFor, setSeenFor] = useState<DayTradeTarget>(null);

  // Seed the inputs once per opening (no effect needed).
  if (target && seenFor !== target) {
    setSeenFor(target);
    setPeriod(target.period);
    setResult(target.informed ? String(target.result).replace(".", ",") : "");
    setIrrf(target.informed && target.irrf ? String(target.irrf).replace(".", ",") : "");
  }
  if (!target && seenFor) setSeenFor(null);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["monthly-taxes"] });
  const save = useMutation({
    mutationFn: () =>
      api.saveDayTrade(period, {
        result: parseAmount(result) ?? "0",
        irrf: parseAmount(irrf) ?? "0",
      }),
    onSuccess: () => {
      refresh();
      toast.success("Day trade salvo.", { description: monthName(period) });
      onClose();
    },
    onError: (error) =>
      toast.error("Não foi possível salvar.", error instanceof ApiError ? error : undefined),
  });
  const clear = useMutation({
    mutationFn: () => api.clearDayTrade(period),
    onSuccess: () => {
      refresh();
      toast.success("Day trade removido.");
      onClose();
    },
    onError: (error) =>
      toast.error("Não foi possível remover.", error instanceof ApiError ? error : undefined),
  });

  const fixedMonth = Boolean(target?.period);
  const periodValid = /^\d{4}-(0[1-9]|1[0-2])$/.test(period);
  const resultValid = parseAmount(result) !== null;
  const irrfParsed = irrf.trim() ? parseAmount(irrf) : "0";
  const irrfValid = irrfParsed !== null && !irrfParsed.startsWith("-");

  return (
    <Modal
      open={target !== null}
      onClose={onClose}
      title="Day trade"
      subtitle={fixedMonth && periodValid ? monthName(period) : "Resultado de um mês"}
    >
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (periodValid && resultValid && irrfValid) save.mutate();
        }}
      >
        <p className="text-sm text-ink-secondary">
          O extrato da B3 não identifica day trades. Informe o resultado líquido do mês que consta
          no relatório da corretora (negativo se teve prejuízo) e o IRRF de 1 % retido.
        </p>
        {!fixedMonth ? (
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-ink-muted">Mês</span>
            <input
              type="month"
              className="input w-full"
              value={period}
              onChange={(event) => setPeriod(event.target.value)}
            />
          </label>
        ) : null}
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">
            Resultado do mês (R$)
          </span>
          <input
            className="input w-full"
            inputMode="decimal"
            value={result}
            onChange={(event) => setResult(event.target.value)}
            placeholder="Ex.: 1.250,00 ou -300,00"
            autoFocus={fixedMonth}
          />
        </label>
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">IRRF retido (R$)</span>
          <input
            className="input w-full"
            inputMode="decimal"
            value={irrf}
            onChange={(event) => setIrrf(event.target.value)}
            placeholder="0,00"
          />
        </label>
        {result && !resultValid ? (
          <p className="text-xs text-negative">Use um valor como 1.250,00 ou -300,00.</p>
        ) : null}
        <div className="flex flex-wrap items-center justify-end gap-2">
          {target?.informed ? (
            <button
              type="button"
              className="btn-ghost mr-auto text-negative"
              disabled={clear.isPending}
              onClick={() => clear.mutate()}
            >
              Remover
            </button>
          ) : null}
          <button type="button" className="btn-ghost" onClick={onClose}>
            Cancelar
          </button>
          <button
            type="submit"
            className="btn-primary"
            disabled={!periodValid || !resultValid || !irrfValid || save.isPending}
          >
            Salvar
          </button>
        </div>
      </form>
    </Modal>
  );
}

/* -------------------------------------------------------------------- about */

function About({ sheet }: { sheet: TaxSheet }) {
  const { rules } = sheet;
  return (
    <Card className="p-5">
      <details className="group">
        <summary className="flex cursor-pointer list-none items-center gap-3">
          <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-surface-hover text-ink-secondary">
            <Info size={15} aria-hidden />
          </span>
          <span className="flex-1 text-sm font-medium text-ink">
            Como a conta é feita e o que fica de fora
          </span>
          <ChevronDown
            size={15}
            className="text-ink-muted transition-transform group-open:rotate-180"
            aria-hidden
          />
        </summary>
        <div className="mt-4 space-y-4 text-xs leading-relaxed text-ink-muted">
          <ul className="list-disc space-y-1 pl-4">
            <li>
              Ações, units, ETFs, BDRs e direitos: {percent(n(rules.rate_common) * 100, 0)} sobre o
              lucro. O lucro em ações é isento no mês em que as vendas de ações somam até{" "}
              {money(rules.exemption_limit)}; ETFs e BDRs nunca são isentos.
            </li>
            <li>
              FIIs: {percent(n(rules.rate_fii) * 100, 0)}, sem isenção. Day trade:{" "}
              {percent(n(rules.rate_day_trade) * 100, 0)}, com o resultado informado por você.
            </li>
            <li>
              Prejuízos são compensados sem prazo, cada um no seu grupo: operações comuns, FIIs e
              day trade não se misturam. Prejuízo em mês isento também conta.
            </li>
            <li>
              O IRRF de {percent(n(rules.irrf_rate) * 100, 3)} sobre as vendas (o
              &quot;dedo-duro&quot;) é estimado e descontado dentro do mesmo ano. DARF código{" "}
              {rules.darf_code}, até o último dia útil do mês seguinte; abaixo de{" "}
              {money(rules.darf_minimum)}, o valor soma ao mês seguinte.
            </li>
            <li>
              O lucro usa o preço médio do GumbInvest e os custos que constam no extrato. É uma
              estimativa para conferência — não substitui o seu contador.
            </li>
          </ul>
          <div>
            <p className="mb-1 font-medium text-ink-secondary">Fora desta apuração</p>
            <p>
              Ativos no exterior são apurados uma vez por ano, na declaração (Lei 14.754/2023), com
              alíquota de 15 %. Criptoativos têm limite e regras próprios, que dependem de onde
              estão custodiados. Renda fixa e Tesouro têm o imposto retido na fonte.
            </p>
            {sheet.excluded_sales.length ? (
              <ul className="mt-2 space-y-0.5">
                {sheet.excluded_sales.slice(0, 12).map((row) => (
                  <li key={row.period} className="flex justify-between gap-3">
                    <span>{monthName(row.period, "short")}</span>
                    <span className="tnum">
                      {[
                        row.exterior ? `exterior ${money(row.exterior)}` : null,
                        row.cripto ? `cripto ${money(row.cripto)}` : null,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        </div>
      </details>
    </Card>
  );
}
