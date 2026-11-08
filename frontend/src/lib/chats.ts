/** Saved AI conversations: what the chat panel and the Conversas screen share. */
import type { AiChatSummary } from "@/lib/api";

export const CHATS_QUERY_KEY = ["ai-chats"] as const;

/** Whether a saved chat belongs to the panel scoped to `ticker` (null: the
 *  portfolio panel). Asset chats reopen on their asset's page only. */
export function inScope(chat: AiChatSummary, ticker: string | null): boolean {
  return ticker ? chat.ticker?.toUpperCase() === ticker.toUpperCase() : chat.ticker === null;
}
