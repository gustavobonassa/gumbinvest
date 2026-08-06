/**
 * Pending AI suggestions for one category — each accepted or declined alone.
 *
 * "failed" rows (targets the backend refused, e.g. an unknown ticker) ride
 * along disabled with their reason: the user must see what the model proposed
 * and why it was blocked, not wonder about a gap in the list.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Check, X } from "lucide-react";

import { useToast } from "@/components/Toast";
import { Badge, Card } from "@/components/ui";
import type { AiSuggestionAction, AiWalletDetail, AiWalletSuggestion } from "@/lib/api";
import { api, ApiError } from "@/lib/api";
import { money } from "@/lib/format";
import { useState } from "react";

const ACTION_LABELS: Record<AiSuggestionAction, string> = {
  buy_new: "Comprar",
  increase: "Reforçar",
  reduce: "Reduzir",
  sell_all: "Zerar",
  rebalance: "Rebalancear",
};

const ACTION_TONES: Record<AiSuggestionAction, "positive" | "negative" | "accent"> = {
  buy_new: "positive",
  increase: "positive",
  reduce: "negative",
  sell_all: "negative",
  rebalance: "accent",
};

export default function SuggestionList({
  wallet,
  category,
}: {
  wallet: AiWalletDetail;
  category: string;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [errors, setErrors] = useState<Record<number, string>>({});

  const suggestionsQ = useQuery({
    queryKey: ["ai-wallet-suggestions", wallet.id, category],
    queryFn: () => api.aiWalletSuggestions(wallet.id, category),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["ai-wallet-suggestions", wallet.id] });
    queryClient.invalidateQueries({ queryKey: ["ai-wallet", wallet.id] });
    queryClient.invalidateQueries({ queryKey: ["ai-wallets"] });
    queryClient.invalidateQueries({ queryKey: ["ai-wallet-events", wallet.id] });
    queryClient.invalidateQueries({ queryKey: ["ai-wallet-compare"] });
  };

  const accept = useMutation({
    mutationFn: (id: number) => api.acceptAiSuggestion(wallet.id, id),
    onSuccess: () => {
      invalidate();
      toast.success("Sugestão aplicada à carteira.");
    },
    // The reason a suggestion was refused stays on its own row — it belongs to
    // that line, not to the app — and the toast only says it did not go through.
    onError: (error, id) => {
      setErrors((current) => ({
        ...current,
        [id]: error instanceof ApiError ? error.message : "Não foi possível aplicar.",
      }));
      toast.error("Não foi possível aplicar a sugestão.", error);
    },
  });

  const decline = useMutation({
    mutationFn: (id: number) => api.declineAiSuggestion(wallet.id, id),
    onSuccess: () => {
      invalidate();
      toast.info("Sugestão recusada.");
    },
    onError: (error) => toast.error("Não foi possível recusar a sugestão.", error),
  });

  const items = suggestionsQ.data ?? [];
  if (!items.length) return null;

  const categoryLabel = (code: string | null) =>
    wallet.categories.find((block) => block.category === code)?.label ?? code ?? "";

  return (
    <Card className="p-5">
      <h2 className="text-base font-semibold tracking-tight text-ink">
        Sugestões de {wallet.provider_label}
      </h2>
      <p className="mt-0.5 text-sm text-ink-muted">
        Aceite ou recuse cada sugestão individualmente — os valores são aplicados a preço de
        mercado no momento do aceite.
      </p>
      <ul className="mt-4 space-y-3">
        {items.map((item) => (
          <SuggestionCard
            key={item.id}
            item={item}
            categoryLabel={categoryLabel}
            error={errors[item.id]}
            busy={accept.isPending || decline.isPending}
            onAccept={() => accept.mutate(item.id)}
            onDecline={() => decline.mutate(item.id)}
          />
        ))}
      </ul>
    </Card>
  );
}

function SuggestionCard({
  item,
  categoryLabel,
  error,
  busy,
  onAccept,
  onDecline,
}: {
  item: AiWalletSuggestion;
  categoryLabel: (code: string | null) => string;
  error?: string;
  busy: boolean;
  onAccept: () => void;
  onDecline: () => void;
}) {
  const failed = item.status === "failed";
  const subject = item.ticker ?? item.name ?? "";
  return (
    <li
      className={
        failed
          ? "rounded-xl border border-line bg-surface-hover/40 p-4 opacity-70"
          : "rounded-xl border border-line bg-surface-raised p-4"
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={failed ? "neutral" : ACTION_TONES[item.action]}>{ACTION_LABELS[item.action]}</Badge>
        <span className="font-medium text-ink">{subject}</span>
        {item.amount_brl !== null ? (
          <span className="tnum text-sm text-ink-secondary">{money(item.amount_brl)}</span>
        ) : null}
        {item.action === "rebalance" ? (
          <span className="flex items-center gap-1 text-sm text-ink-secondary">
            <ArrowRight size={14} aria-hidden />
            {item.to_ticker ?? `caixa de ${categoryLabel(item.to_category)}`}
            {item.to_ticker && item.to_category && item.to_category !== item.category
              ? ` (${categoryLabel(item.to_category)})`
              : null}
          </span>
        ) : null}
        {failed ? <Badge tone="warning">recusada pelo sistema</Badge> : null}
      </div>
      {item.rationale ? <p className="mt-2 max-w-3xl text-sm text-ink-secondary">{item.rationale}</p> : null}
      {failed && item.detail ? <p className="mt-2 text-xs text-warning">{item.detail}</p> : null}
      {error ? <p className="mt-2 text-xs text-negative">{error}</p> : null}
      {!failed ? (
        <div className="mt-3 flex gap-2">
          <button type="button" className="btn-primary px-3 py-1.5 text-sm" disabled={busy} onClick={onAccept}>
            <Check size={14} /> Aceitar
          </button>
          <button type="button" className="btn-ghost px-3 py-1.5 text-sm" disabled={busy} onClick={onDecline}>
            <X size={14} /> Recusar
          </button>
        </div>
      ) : null}
    </li>
  );
}
