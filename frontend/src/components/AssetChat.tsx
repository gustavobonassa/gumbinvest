/** AI analyst: per-asset on asset pages, portfolio-wide everywhere else.
 *
 * A top-bar button opens a right-side panel. `ticker` scopes the conversation:
 * a string chats about that asset (company data + the owner's position); null
 * chats about the whole portfolio (totals + every open position). The backend
 * assembles the context and streams the answer over SSE; this component only
 * keeps the conversation and renders the stream as it arrives.
 */
import clsx from "clsx";
import { RotateCcw, Send, Sparkles, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useSearchParams } from "react-router-dom";

import { api, API_BASE } from "@/lib/api";

type ChatMessage = { role: "user" | "assistant"; content: string };

/**
 * Where the button that opens this panel goes.
 *
 * The app shell renders an empty element with this id in its top bar and the
 * trigger portals into it. It used to float over the bottom-right corner,
 * where it sat on top of whatever the page put there — pagers, most visibly.
 * A slot keeps the button in one predictable place while letting either mount
 * site (the shell for the portfolio chat, the asset page for its own) own the
 * conversation state.
 */
export const AI_CHAT_SLOT_ID = "ai-chat-trigger";

const SUGGESTIONS_ASSET = [
  "Vale a pena aumentar minha posição?",
  "Resuma os últimos resultados da empresa",
  "Quais os principais riscos deste ativo?",
  "Alguma notícia relevante recente?",
];

const SUGGESTIONS_PORTFOLIO = [
  "Como está a diversificação da minha carteira?",
  "Qual é o meu maior risco hoje?",
  "Onde investir meu próximo aporte?",
  "Resuma o desempenho da carteira",
];

/** The model sometimes quotes web results verbatim, escapes included, so
 *  "preço" arrives as the literal text "pre\\u00e7o". Decoded at render time,
 *  which also heals conversations saved before this fix existed. */
function decodeEscapes(text: string): string {
  if (!text.includes("\\u")) return text;
  return text.replace(/\\u([0-9a-fA-F]{4})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
}

/** Minimal renderer: paragraphs, "- " lists and **bold** — nothing else. */
function MessageText({ text }: { text: string }) {
  const parts = decodeEscapes(text).split(/\*\*(.+?)\*\*/g);
  return (
    // break-words: answers cite URLs longer than the bubble is wide.
    <span className="whitespace-pre-wrap break-words">
      {parts.map((part, index) =>
        index % 2 === 1 ? (
          <strong key={index} className="font-semibold text-ink">
            {part}
          </strong>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </span>
  );
}

export default function AssetChat({ ticker }: { ticker: string | null }) {
  const scopeLabel = ticker ?? "sua carteira";
  const suggestions = ticker ? SUGGESTIONS_ASSET : SUGGESTIONS_PORTFOLIO;
  const [open, setOpen] = useState(false);
  // The panel outlives `open` by one exit animation: it stays mounted while it
  // slides out and unmounts on `animationend`, so closing is a movement rather
  // than a disappearance.
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    if (open) setMounted(true);
  }, [open]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  //: The saved conversation this chat continues; assigned by the backend on
  //: the first answer, so every conversation ends up on the Conversas screen.
  const [chatId, setChatId] = useState<number | null>(null);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // A conversation is about one asset; navigating to another starts fresh.
  useEffect(() => {
    setMessages([]);
    setChatId(null);
    setError(null);
    setStatus(null);
    abortRef.current?.abort();
    setStreaming(false);
  }, [ticker]);

  // ?chat={id} (from the Conversas screen) reopens a saved conversation here.
  // The portfolio panel lives in the Layout and survives navigation, so it has
  // to react to the param itself, not just to mounting.
  const [params, setParams] = useSearchParams();
  const requested = Number(params.get("chat"));
  useEffect(() => {
    if (!requested) return;
    let cancelled = false;
    api
      .aiChatDetail(requested)
      .then((chat) => {
        // A saved chat only reopens in its own scope: asset chats on their
        // asset's page, portfolio chats in the portfolio panel.
        const matches = ticker
          ? chat.ticker?.toUpperCase() === ticker.toUpperCase()
          : chat.ticker === null;
        if (cancelled || !matches) return;
        setMessages(chat.messages);
        setChatId(chat.id);
        setOpen(true);
      })
      .catch(() => setError("Não foi possível carregar a conversa salva."));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticker, requested]);

  const startNewConversation = () => {
    setMessages([]);
    setChatId(null);
    setError(null);
    if (params.has("chat")) {
      const next = new URLSearchParams(params);
      next.delete("chat");
      setParams(next, { replace: true });
    }
  };

  // Follow the stream only while the reader is already at the bottom: if they
  // scrolled up to reread something, new tokens must not yank them back down.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, status]);

  // Opening the panel always lands on the latest message.
  useEffect(() => {
    if (open) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [open]);

  useEffect(() => () => abortRef.current?.abort(), []);

  // Resolved in an effect rather than read during render: on the first mount
  // the shell's top bar is not in the document yet.
  const [triggerSlot, setTriggerSlot] = useState<HTMLElement | null>(null);
  useEffect(() => setTriggerSlot(document.getElementById(AI_CHAT_SLOT_ID)), []);

  // Docked, not modal: on desktop the page content is pushed aside (styles.css
  // pads <main> when this class is set) so tabs and charts stay usable while
  // the conversation is open. Small screens keep the overlay — no room to push.
  useEffect(() => {
    document.documentElement.classList.toggle("asset-chat-open", open);
    return () => document.documentElement.classList.remove("asset-chat-open");
  }, [open]);

  const send = async (raw: string) => {
    const question = raw.trim();
    if (!question || streaming) return;
    const history: ChatMessage[] = [...messages, { role: "user", content: question }];
    setMessages([...history, { role: "assistant", content: "" }]);
    setInput("");
    setError(null);
    setStreaming(true);
    setStatus("Analisando…");

    const appendToAnswer = (chunk: string) =>
      setMessages((current) => {
        const next = [...current];
        const last = next[next.length - 1];
        next[next.length - 1] = { ...last, content: last.content + chunk };
        return next;
      });

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const response = await fetch(`${API_BASE}/ai/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker, messages: history, chat_id: chatId }),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        const detail = await response.json().catch(() => null);
        throw new Error(detail?.detail ?? `Erro ${response.status}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop() ?? "";
        for (const event of events) {
          const line = event.trim();
          if (!line.startsWith("data: ")) continue;
          const data = JSON.parse(line.slice(6)) as {
            text?: string;
            status?: string | null;
            error?: string;
            done?: boolean;
            chat_id?: number | null;
          };
          if (data.text) {
            setStatus(null);
            appendToAnswer(data.text);
          }
          if (data.status !== undefined) setStatus(data.status);
          if (data.error) setError(data.error);
          if (data.done) {
            setStatus(null);
            if (data.chat_id) setChatId(data.chat_id);
          }
        }
      }
    } catch (failure) {
      if ((failure as Error).name !== "AbortError") {
        setError((failure as Error).message || "Não foi possível falar com o assistente.");
      }
    } finally {
      setStreaming(false);
      setStatus(null);
      // An empty assistant bubble (errored before any text) is just noise.
      setMessages((current) =>
        current.length && current[current.length - 1].content === "" ? current.slice(0, -1) : current,
      );
    }
  };

  return (
    <>
      {/* In the top bar the button is a toggle, not a launcher: it stays put
          while the panel is open, so the same control closes it again. */}
      {triggerSlot
        ? createPortal(
            <button
              type="button"
              onClick={() => setOpen((current) => !current)}
              aria-expanded={open}
              className={clsx("btn-ghost", open && "bg-accent-soft text-accent")}
              title={`Conversar com a IA sobre ${scopeLabel}`}
            >
              <Sparkles size={15} aria-hidden />
              <span className="hidden sm:inline">Analista IA</span>
            </button>,
            triggerSlot,
          )
        : null}

      {/* Portal to <body>: the page wraps its content in `space-y-6`, whose
          child margins would push these fixed elements 24px down — outside the
          page tree no layout class can touch them. */}
      {mounted
        ? createPortal(
            <>
          {/* Mobile only: below lg there is no space to push the page aside. */}
          <button
            type="button"
            aria-label="Fechar chat"
            className={clsx(
              "desktop-shell-below-topbar fixed inset-x-0 bottom-0 top-16 z-30 cursor-default bg-black/40 backdrop-blur-[2px] lg:hidden",
              open ? "animate-fade-in" : "pointer-events-none animate-fade-out",
            )}
            onClick={() => setOpen(false)}
          />
          {/* top-16 = below the sticky top bar: the header (search, Atualizar)
              stays visible and clickable while the chat is open. */}
          <aside
            role="complementary"
            aria-label={`Chat sobre ${scopeLabel}`}
            onAnimationEnd={(event) => {
              // Only the panel's own slide-out — animations bubbling up from
              // children must not unmount the chat mid-conversation.
              if (event.target === event.currentTarget && !open) setMounted(false);
            }}
            className={clsx(
              "desktop-shell-below-topbar fixed bottom-0 right-0 top-16 z-30 flex w-full max-w-md flex-col border-l border-line bg-surface shadow-raised",
              open ? "animate-chat-in" : "animate-chat-out",
            )}
          >
            <header className="flex items-center gap-2.5 border-b border-line px-4 py-3">
              <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent">
                <Sparkles size={15} aria-hidden />
              </span>
              <div className="min-w-0 leading-tight">
                <p className="font-semibold">Analista IA</p>
                <p className="truncate text-[11px] text-ink-muted">
                  {ticker ? `${ticker} · dados da empresa + sua posição + web` : "carteira completa + web"}
                </p>
              </div>
              <div className="ml-auto flex items-center gap-1">
                {messages.length ? (
                  <button
                    type="button"
                    onClick={startNewConversation}
                    className="btn-ghost px-2 py-1.5"
                    title="Nova conversa (a atual fica salva em Conversas IA)"
                  >
                    <RotateCcw size={15} />
                  </button>
                ) : null}
                <button type="button" onClick={() => setOpen(false)} className="btn-ghost px-2 py-1.5" aria-label="Fechar">
                  <X size={16} />
                </button>
              </div>
            </header>

            <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
              {!messages.length ? (
                <div className="space-y-3">
                  <p className="text-sm text-ink-secondary">
                    {ticker
                      ? `Pergunte sobre ${ticker}: fundamentos, notícias, riscos, ou o desempenho da sua posição.`
                      : "Pergunte sobre sua carteira: diversificação, riscos, desempenho, onde aportar."}{" "}
                    Eu vejo os dados da carteira e posso pesquisar na web.
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {suggestions.map((suggestion) => (
                      <button
                        key={suggestion}
                        type="button"
                        onClick={() => send(suggestion)}
                        className="rounded-full border border-line bg-surface-raised px-3 py-1.5 text-left text-xs text-ink-secondary transition-colors hover:border-accent/40 hover:text-ink"
                      >
                        {suggestion}
                      </button>
                    ))}
                  </div>
                </div>
              ) : null}

              {messages.map((message, index) =>
                message.content || message.role === "user" ? (
                  <div
                    key={index}
                    className={clsx(
                      "max-w-[92%] rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed",
                      message.role === "user"
                        ? "ml-auto bg-accent-soft text-ink"
                        : "mr-auto bg-surface-raised text-ink-secondary",
                    )}
                  >
                    <MessageText text={message.content} />
                  </div>
                ) : null,
              )}

              {status ? (
                <p className="flex items-center gap-2 text-xs text-ink-muted">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" aria-hidden />
                  {status}
                </p>
              ) : null}
              {error ? <p className="text-xs text-negative">{error}</p> : null}
              <div ref={bottomRef} />
            </div>

            <footer className="border-t border-line p-3">
              <form
                className="flex items-end gap-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  void send(input);
                }}
              >
                <textarea
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      void send(input);
                    }
                  }}
                  rows={2}
                  placeholder={`Perguntar sobre ${scopeLabel}…`}
                  className="input flex-1 resize-none text-sm"
                  disabled={streaming}
                />
                <button
                  type="submit"
                  className="btn-primary px-3 py-2.5"
                  disabled={streaming || !input.trim()}
                  aria-label="Enviar"
                >
                  <Send size={15} />
                </button>
              </form>
              <p className="mt-2 text-[10px] leading-snug text-ink-muted">
                Análise gerada por IA com seus dados e busca na web. Conteúdo educacional, não é
                recomendação de investimento.
              </p>
            </footer>
          </aside>
            </>,
            document.body,
          )
        : null}
    </>
  );
}
