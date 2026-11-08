/** The chat panel's history dropdown: past conversations, one click to reopen.
 *
 * Conversations about what the panel is scoped to (this asset, or the
 * portfolio) come first; the rest follow under "Outras conversas", since
 * opening one of those moves to where it belongs. Deleting stays on the
 * Conversas screen, linked at the bottom — a misclick in a dropdown should
 * never cost a conversation.
 */
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { ArrowRight } from "lucide-react";

import { AnchoredPanel, Badge } from "@/components/ui";
import { api, type AiChatSummary } from "@/lib/api";
import { CHATS_QUERY_KEY, inScope } from "@/lib/chats";
import { dateTime } from "@/lib/format";

export default function ChatHistory({
  anchor,
  ticker,
  currentId,
  onPick,
  onSeeAll,
  onClose,
}: {
  anchor: HTMLElement | null;
  ticker: string | null;
  currentId: number | null;
  onPick: (chat: AiChatSummary) => void;
  onSeeAll: () => void;
  onClose: () => void;
}) {
  const { data, isLoading, isError } = useQuery({
    queryKey: CHATS_QUERY_KEY,
    queryFn: api.aiChats,
    // Opening the dropdown is asking "what's there now".
    staleTime: 0,
  });
  const mine = (data ?? []).filter((chat) => inScope(chat, ticker));
  const others = (data ?? []).filter((chat) => !inScope(chat, ticker));

  const group = (title: string, chats: AiChatSummary[]) =>
    chats.length ? (
      <section>
        <p className="px-3 pb-1 pt-2.5 text-[11px] font-medium uppercase tracking-wide text-ink-muted">
          {title}
        </p>
        <ul>
          {chats.map((chat) => (
            <li key={chat.id}>
              <button
                type="button"
                onClick={() => onPick(chat)}
                aria-current={chat.id === currentId ? "true" : undefined}
                className={clsx(
                  "flex w-full flex-col gap-0.5 rounded-lg px-3 py-2 text-left transition-colors hover:bg-surface-hover",
                  chat.id === currentId && "bg-accent-soft",
                )}
              >
                <span className="flex min-w-0 items-center gap-2">
                  {inScope(chat, ticker) ? null : (
                    // Capped: a Tesouro or fixed-income name can be as wide
                    // as the row, and the title is what the row is for.
                    <Badge
                      className="min-w-0 max-w-[45%] shrink-0"
                      tone={chat.ticker ? "accent" : "neutral"}
                    >
                      <span className="truncate" title={chat.ticker ?? undefined}>
                        {chat.ticker ?? "Carteira"}
                      </span>
                    </Badge>
                  )}
                  <span className="min-w-0 truncate text-sm text-ink">{chat.title}</span>
                </span>
                <span className="text-[11px] text-ink-muted">
                  {dateTime(chat.updated_at)} · {chat.message_count} mensagens
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>
    ) : null;

  return (
    <AnchoredPanel anchor={anchor} onClose={onClose} matchWidth={false} minWidth={320}>
      <div className="max-h-[60vh] overflow-y-auto overflow-x-clip">
        {isLoading ? (
          <p className="px-3 py-6 text-center text-xs text-ink-muted">Carregando…</p>
        ) : isError ? (
          <p className="px-3 py-6 text-center text-xs text-negative">
            Não foi possível carregar as conversas.
          </p>
        ) : !data?.length ? (
          <p className="px-3 py-6 text-center text-xs text-ink-muted">
            Nenhuma conversa salva ainda.
          </p>
        ) : (
          <>
            {group(ticker ? `Sobre ${ticker}` : "Sobre a carteira", mine)}
            {group(mine.length ? "Outras conversas" : "Conversas", others)}
          </>
        )}
      </div>
      <button
        type="button"
        onClick={onSeeAll}
        className="mt-1 flex w-full items-center justify-center gap-1.5 rounded-lg border-t border-line px-3 py-2 text-xs text-ink-secondary transition-colors hover:bg-surface-hover hover:text-ink"
      >
        Gerenciar conversas <ArrowRight size={13} aria-hidden />
      </button>
    </AnchoredPanel>
  );
}
