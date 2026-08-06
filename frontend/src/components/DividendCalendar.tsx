/** Month calendar of income: what was paid and what is declared to come.
 *
 * Two sources with different natures share the grid, and the styling keeps
 * them honest: paid amounts (solid, green) come from the imported ledger and
 * are facts; upcoming ones (outlined, accent, "≈") come from the schedule
 * companies declare to B3 and are estimates sized by the current position.
 */
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { Badge, Card, Modal, SectionTitle } from "@/components/ui";
import type { IncomePayment, UpcomingDividend, UpcomingDividends } from "@/lib/api";
import { dateTime, money, opLabel, shortDate } from "@/lib/format";

const WEEKDAYS = ["dom", "seg", "ter", "qua", "qui", "sex", "sáb"];

function iso(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function monthTitle(year: number, month: number): string {
  const label = new Date(year, month, 1).toLocaleDateString("pt-BR", { month: "long", year: "numeric" });
  return label.charAt(0).toUpperCase() + label.slice(1);
}

type DayBucket = { paid: number; upcoming: number; lines: string[] };

export default function DividendCalendar({
  payments,
  upcoming,
  onRefresh,
  refreshing,
}: {
  payments: IncomePayment[];
  upcoming: UpcomingDividends | undefined;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const today = new Date();
  const [year, setYear] = useState(today.getFullYear());
  const [month, setMonth] = useState(today.getMonth());
  // A day's payments used to live only in `title`, unreachable on touch —
  // tapping the day opens them instead.
  const [dayDetail, setDayDetail] = useState<{ key: string; bucket: DayBucket } | null>(null);

  const move = (delta: number) => {
    const next = new Date(year, month + delta, 1);
    setYear(next.getFullYear());
    setMonth(next.getMonth());
  };

  const { byDay, monthPaid, monthUpcoming, monthRows, pendingRows } = useMemo(() => {
    const byDay = new Map<string, DayBucket>();
    const bucket = (key: string) => {
      let entry = byDay.get(key);
      if (!entry) {
        entry = { paid: 0, upcoming: 0, lines: [] };
        byDay.set(key, entry);
      }
      return entry;
    };
    for (const payment of payments) {
      const entry = bucket(payment.date);
      entry.paid += payment.net;
      entry.lines.push(`${payment.ticker} · ${opLabel(payment.op_type)} · ${money(payment.net)}`);
    }
    const pendingRows: UpcomingDividend[] = [];
    for (const item of upcoming?.items ?? []) {
      if (!item.payment_date) {
        pendingRows.push(item);
        continue;
      }
      const entry = bucket(item.payment_date);
      entry.upcoming += item.total;
      entry.lines.push(`${item.ticker} · ${item.label ?? "provento"} · ≈ ${money(item.total)}`);
    }

    const prefix = `${year}-${String(month + 1).padStart(2, "0")}`;
    let monthPaid = 0;
    let monthUpcoming = 0;
    const monthRows: { date: string; ticker: string; label: string; value: number; isUpcoming: boolean }[] = [];
    for (const payment of payments) {
      if (!payment.date.startsWith(prefix)) continue;
      monthPaid += payment.net;
      monthRows.push({
        date: payment.date,
        ticker: payment.ticker,
        label: opLabel(payment.op_type),
        value: payment.net,
        isUpcoming: false,
      });
    }
    for (const item of upcoming?.items ?? []) {
      if (!item.payment_date?.startsWith(prefix)) continue;
      monthUpcoming += item.total;
      monthRows.push({
        date: item.payment_date,
        ticker: item.ticker,
        label: item.label ?? "provento",
        value: item.total,
        isUpcoming: true,
      });
    }
    monthRows.sort((a, b) => a.date.localeCompare(b.date));
    return { byDay, monthPaid, monthUpcoming, monthRows, pendingRows };
  }, [payments, upcoming, year, month]);

  // 6 fixed weeks starting on the Sunday at or before the 1st: every month
  // fits, and the grid never changes height while navigating.
  const first = new Date(year, month, 1);
  const gridStart = new Date(year, month, 1 - first.getDay());
  const cells = Array.from({ length: 42 }, (_, index) => {
    const date = new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + index);
    return { date, key: iso(date), inMonth: date.getMonth() === month };
  });
  const todayKey = iso(today);

  return (
    <Card className="p-5" hover={false}>
      <SectionTitle
        title="Calendário de proventos"
        subtitle="Pagamentos recebidos e proventos anunciados pelas empresas na B3"
        action={
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onRefresh}
              disabled={refreshing}
              className="btn-ghost px-2 py-1.5 text-xs"
              title="Buscar novos anúncios na B3"
            >
              <RefreshCw size={14} className={refreshing ? "animate-spin" : undefined} aria-hidden />
              <span className="hidden sm:inline">Atualizar previsões</span>
            </button>
            <button type="button" onClick={() => move(-1)} className="btn-ghost px-2 py-1.5" aria-label="Mês anterior">
              <ChevronLeft size={16} />
            </button>
            <button type="button" onClick={() => move(1)} className="btn-ghost px-2 py-1.5" aria-label="Próximo mês">
              <ChevronRight size={16} />
            </button>
          </div>
        }
      />

      <div className="grid gap-5 xl:grid-cols-[1fr_300px]">
        <div>
          <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
            <p className="font-medium text-ink">{monthTitle(year, month)}</p>
            <p className="text-xs text-ink-muted">
              <span className="text-positive">{money(monthPaid)} recebidos</span>
              {monthUpcoming > 0 ? (
                <>
                  {" · "}
                  <span className="text-accent">≈ {money(monthUpcoming)} previstos</span>
                </>
              ) : null}
            </p>
          </div>

          <div className="grid grid-cols-7 gap-1">
            {WEEKDAYS.map((day) => (
              <p key={day} className="pb-1 text-center text-[11px] font-medium uppercase tracking-wide text-ink-muted">
                {day}
              </p>
            ))}
            {cells.map((cell) => {
              const entry = byDay.get(cell.key);
              const Tag = entry ? "button" : "div";
              return (
                <Tag
                  key={cell.key}
                  {...(entry
                    ? {
                        type: "button" as const,
                        onClick: () => setDayDetail({ key: cell.key, bucket: entry }),
                        "aria-label": `Proventos de ${shortDate(cell.key)}`,
                      }
                    : {})}
                  title={entry?.lines.join("\n")}
                  className={[
                    "min-h-[40px] rounded-lg border p-1.5 text-left text-xs transition-colors sm:min-h-[64px]",
                    cell.inMonth ? "border-line bg-surface-raised/40" : "border-transparent opacity-40",
                    cell.key === todayKey ? "ring-1 ring-accent/60" : "",
                    entry ? "cursor-pointer hover:border-line-strong hover:bg-surface-hover/60" : "",
                  ].join(" ")}
                >
                  <p className={cell.key === todayKey ? "font-semibold text-accent" : "text-ink-muted"}>
                    {cell.date.getDate()}
                  </p>
                  {/* A phone-width cell (~37px) cannot carry a money chip, so
                      below `sm` a day shows dots and the amounts live in the
                      "No mês" list right under the calendar. */}
                  {entry && entry.paid > 0 ? (
                    <>
                      <p className="tnum mt-1 hidden truncate rounded bg-positive/10 px-1 py-0.5 text-[11px] font-medium text-positive sm:block">
                        {money(entry.paid, { compact: true })}
                      </p>
                      <span className="mt-1 inline-block h-1.5 w-1.5 rounded-full bg-positive sm:hidden" aria-hidden />
                    </>
                  ) : null}
                  {entry && entry.upcoming > 0 ? (
                    <>
                      <p className="tnum mt-1 hidden truncate rounded border border-accent/40 bg-accent-soft px-1 py-0.5 text-[11px] font-medium text-accent sm:block">
                        ≈ {money(entry.upcoming, { compact: true })}
                      </p>
                      <span className="mt-1 inline-block h-1.5 w-1.5 rounded-full bg-accent sm:hidden" aria-hidden />
                    </>
                  ) : null}
                </Tag>
              );
            })}
          </div>

          <p className="mt-3 text-xs text-ink-muted">
            Verde: creditado na conta. Azul: anunciado na B3, estimado pela posição atual —{" "}
            {upcoming?.updated_at ? `previsões atualizadas ${dateTime(upcoming.updated_at)}` : "sem previsões carregadas"}
            {upcoming?.missing.length ? ` · ${upcoming.missing.length} ativo(s) ainda sem consulta` : ""}.
          </p>
        </div>

        <div className="min-w-0">
          <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">No mês</p>
          {!monthRows.length ? (
            <p className="py-4 text-sm text-ink-muted">Nenhum provento neste mês.</p>
          ) : (
            <div className="max-h-[320px] space-y-0.5 overflow-auto pr-1">
              {monthRows.map((row, index) => (
                <p
                  key={`${row.date}-${row.ticker}-${index}`}
                  className="flex items-baseline justify-between gap-3 py-1"
                >
                  <span className="min-w-0 truncate">
                    <span className="tnum mr-2 text-xs text-ink-muted">{shortDate(row.date).slice(0, 5)}</span>
                    <Link to={`/ativos/${row.ticker}`} className="font-medium text-ink hover:text-accent">
                      {row.ticker}
                    </Link>
                    <span className="ml-2 text-xs text-ink-muted">{row.label}</span>
                  </span>
                  <span className={`tnum shrink-0 text-sm ${row.isUpcoming ? "text-accent" : "text-positive"}`}>
                    {row.isUpcoming ? "≈ " : ""}
                    {money(row.value)}
                  </span>
                </p>
              ))}
            </div>
          )}

          {pendingRows.length ? (
            <>
              <p className="mb-2 mt-4 text-xs font-semibold uppercase tracking-wide text-ink-muted">
                Anunciados, data a definir
              </p>
              <div className="space-y-0.5">
                {pendingRows.map((row, index) => (
                  <p key={`${row.ticker}-${index}`} className="flex items-baseline justify-between gap-3 py-1">
                    <span className="min-w-0 truncate">
                      <Link to={`/ativos/${row.ticker}`} className="font-medium text-ink hover:text-accent">
                        {row.ticker}
                      </Link>
                      <span className="ml-2 text-xs text-ink-muted">{row.label ?? "provento"}</span>
                      <Badge className="ml-2">a definir</Badge>
                    </span>
                    <span className="tnum shrink-0 text-sm text-accent">≈ {money(row.total)}</span>
                  </p>
                ))}
              </div>
            </>
          ) : null}
        </div>
      </div>

      <Modal
        open={dayDetail !== null}
        title={dayDetail ? `Proventos de ${shortDate(dayDetail.key)}` : ""}
        subtitle={
          dayDetail
            ? [
                dayDetail.bucket.paid > 0 ? `${money(dayDetail.bucket.paid)} recebidos` : null,
                dayDetail.bucket.upcoming > 0 ? `≈ ${money(dayDetail.bucket.upcoming)} previstos` : null,
              ]
                .filter(Boolean)
                .join(" · ")
            : undefined
        }
        onClose={() => setDayDetail(null)}
      >
        <ul className="space-y-1.5 text-sm text-ink-secondary">
          {dayDetail?.bucket.lines.map((line) => (
            <li key={line} className="tnum">
              {line}
            </li>
          ))}
        </ul>
      </Modal>
    </Card>
  );
}
