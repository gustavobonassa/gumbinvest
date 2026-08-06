/** Saved AI-analyst conversations: read them again or pick one back up.
 *
 * Continuing routes to the asset page with `?chat={id}` — the chat is about
 * that asset, and reopening it there gives the conversation (and the AI) the
 * live page as context.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessagesSquare, Sparkles, Trash2 } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { useToast } from "@/components/Toast";
import { Badge, Card, EmptyState, ErrorState, Modal, Skeleton } from "@/components/ui";
import { api } from "@/lib/api";
import { dateTime } from "@/lib/format";

export default function AiChats() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["ai-chats"],
    queryFn: api.aiChats,
  });
  const remove = useMutation({
    mutationFn: api.deleteAiChat,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["ai-chats"] });
      toast.success("Conversa excluída.");
    },
    onError: (error) => toast.error("Não foi possível excluir a conversa.", error),
  });
  const [confirmDelete, setConfirmDelete] = useState<{ id: number; label: string } | null>(null);

  if (isError) return <ErrorState error={error} retry={() => refetch()} />;

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">Analista IA</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Conversas IA</h1>
        <p className="mt-2 max-w-2xl text-sm text-ink-secondary">
          Toda conversa com o analista fica salva aqui. Abra uma para reler ou continuar de onde
          parou — conversas sobre um ativo reabrem na página dele; sobre a carteira, no dashboard.
        </p>
      </header>

      {isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 5 }).map((_, index) => (
            <Skeleton key={index} className="h-20 w-full" />
          ))}
        </div>
      ) : !data?.length ? (
        <EmptyState
          icon={MessagesSquare}
          title="Nenhuma conversa ainda"
          description="Abra um ativo e clique em “Analista IA” para começar — a conversa aparece aqui automaticamente."
        />
      ) : (
        <div className="space-y-3">
          {data.map((chat) => (
            <Card key={chat.id} className="flex items-center gap-4 p-4">
              <button
                type="button"
                // Asset chats reopen on their asset's page; portfolio chats
                // reopen on the dashboard — the panel exists on every page.
                onClick={() =>
                  navigate(chat.ticker ? `/ativos/${chat.ticker}?chat=${chat.id}` : `/?chat=${chat.id}`)
                }
                className="flex min-w-0 flex-1 items-center gap-4 text-left"
              >
                <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-accent-soft text-accent">
                  <Sparkles size={17} aria-hidden />
                </span>
                <span className="min-w-0">
                  <span className="flex items-center gap-2">
                    <Badge className="shrink-0" tone={chat.ticker ? "accent" : "neutral"}>
                      {chat.ticker ?? "Carteira"}
                    </Badge>
                    <span className="min-w-0 truncate font-medium text-ink">{chat.title}</span>
                  </span>
                  <span className="mt-0.5 block text-xs text-ink-muted">
                    {chat.message_count} mensagens · atualizado {dateTime(chat.updated_at)}
                  </span>
                </span>
              </button>
              <button
                type="button"
                onClick={() => setConfirmDelete({ id: chat.id, label: chat.title })}
                disabled={remove.isPending}
                className="btn-ghost shrink-0 px-2.5 py-2.5 text-ink-muted hover:text-negative"
                title="Excluir conversa"
                aria-label={`Excluir conversa sobre ${chat.ticker ?? "a carteira"}`}
              >
                <Trash2 size={15} />
              </button>
            </Card>
          ))}
        </div>
      )}

      <Modal
        open={confirmDelete !== null}
        title="Excluir esta conversa?"
        subtitle={confirmDelete?.label}
        onClose={() => setConfirmDelete(null)}
      >
        <p className="text-sm text-ink-secondary">
          A conversa e todas as suas mensagens serão apagadas. Esta ação não pode ser desfeita.
        </p>
        <div className="mt-4 flex flex-wrap justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={() => setConfirmDelete(null)}>
            Cancelar
          </button>
          <button
            type="button"
            className="btn-primary bg-negative hover:brightness-110"
            disabled={remove.isPending}
            onClick={() => {
              if (confirmDelete) remove.mutate(confirmDelete.id);
              setConfirmDelete(null);
            }}
          >
            Excluir conversa
          </button>
        </div>
      </Modal>
    </div>
  );
}
