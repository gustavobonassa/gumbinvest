/**
 * The wallet's full audit trail: every change, and which model decided it.
 * Own tab so the log can grow long without burying the overview.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Badge, Card, Pager } from "@/components/ui";
import type { AiWalletDetail, AiWalletEvent } from "@/lib/api";
import { api } from "@/lib/api";
import { dateTime, money } from "@/lib/format";

const EVENT_LABELS: Record<string, string> = {
  "wallet.created": "Carteira criada",
  "category.generated": "Categoria gerada",
  "position.buy": "Compra",
  "position.settled": "Compra concluída",
  "position.increase": "Reforço",
  "position.reduce": "Redução",
  "position.sell": "Venda total",
  "position.rebalance": "Rebalanceamento",
  "suggestion.batch": "Sugestões geradas",
  "suggestion.accepted": "Sugestão aceita",
  "suggestion.declined": "Sugestão recusada",
};

const PAGE_SIZE = 20;

function eventSummary(event: AiWalletEvent): string {
  const detail = event.detail as Record<string, unknown>;
  const amount = detail.amount_brl !== undefined ? money(Number(detail.amount_brl)) : null;
  switch (event.action) {
    case "wallet.created":
      return String(detail.name ?? "");
    case "category.generated": {
      const positions = Array.isArray(detail.positions) ? detail.positions.length : 0;
      const deferred = Array.isArray(detail.deferred) && detail.deferred.length > 0 ? ` · ${detail.deferred.length} aguardando cotação` : "";
      const skipped = Array.isArray(detail.skipped) && detail.skipped.length > 0 ? ` · ${detail.skipped.length} ignorados` : "";
      const search = detail.used_search === false ? " · sem busca na web" : "";
      return `${positions} posições${deferred}${skipped}${search}`;
    }
    case "position.settled": {
      if (detail.refunded_brl !== undefined) {
        return `${detail.ticker ?? "?"} · ${money(Number(detail.refunded_brl))} devolvidos ao caixa (${detail.reason ?? ""})`;
      }
      return [detail.ticker, amount].filter(Boolean).join(" · ");
    }
    case "position.rebalance": {
      const target = detail.to_ticker ?? `caixa de ${detail.to_category ?? "?"}`;
      return [`${detail.from_ticker ?? "?"} → ${target}`, amount].filter(Boolean).join(" · ");
    }
    case "suggestion.batch":
      return `${detail.count ?? 0} sugestões${detail.used_search === false ? " · sem busca na web" : ""}`;
    case "suggestion.accepted":
    case "suggestion.declined":
      return [detail.action, detail.ticker ?? detail.from_ticker].filter(Boolean).join(" · ");
    default:
      return [detail.ticker, amount].filter(Boolean).join(" · ");
  }
}

export default function HistoryTab({ wallet }: { wallet: AiWalletDetail }) {
  const [page, setPage] = useState(1);
  const eventsQ = useQuery({
    queryKey: ["ai-wallet-events", wallet.id, page],
    queryFn: () => api.aiWalletEvents(wallet.id, page),
  });

  const events = eventsQ.data;
  const pages = events ? Math.max(1, Math.ceil(events.total / PAGE_SIZE)) : 1;
  const categoryLabel = (code: string | null) =>
    wallet.categories.find((block) => block.category === code)?.label ?? code;

  return (
    <Card className="p-5">
      <h2 className="text-base font-semibold tracking-tight text-ink">Histórico de mudanças</h2>
      <p className="mt-0.5 text-sm text-ink-muted">
        Tudo que aconteceu nesta carteira, e qual modelo decidiu cada mudança.
      </p>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[680px] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-ink-muted">
              <th className="py-2.5 pr-3 font-medium">Quando</th>
              <th className="px-3 py-2.5 font-medium">Evento</th>
              <th className="px-3 py-2.5 font-medium">Categoria</th>
              <th className="px-3 py-2.5 font-medium">Detalhe</th>
              <th className="px-3 py-2.5 font-medium">Modelo</th>
            </tr>
          </thead>
          <tbody>
            {(events?.items ?? []).map((event) => (
              <tr key={event.id} className="table-row border-b border-line/60">
                <td className="tnum whitespace-nowrap py-3 pr-3 text-ink-secondary">{dateTime(event.at)}</td>
                <td className="px-3 py-3 font-medium text-ink">
                  {EVENT_LABELS[event.action] ?? event.action}
                </td>
                <td className="px-3 py-3 text-ink-secondary">{categoryLabel(event.category) ?? "-"}</td>
                <td className="max-w-[320px] truncate px-3 py-3 text-ink-secondary" title={eventSummary(event)}>
                  {eventSummary(event) || "-"}
                </td>
                <td className="px-3 py-3">
                  {event.model ? <Badge tone="accent">{event.model}</Badge> : "-"}
                </td>
              </tr>
            ))}
            {events && events.items.length === 0 ? (
              <tr>
                <td colSpan={5} className="py-8 text-center text-sm text-ink-muted">
                  Nenhum evento ainda, gere a primeira categoria.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      {events ? (
        <Pager page={page} pages={pages} total={events.total} pageSize={PAGE_SIZE} onChange={setPage} noun="eventos" />
      ) : null}
    </Card>
  );
}
