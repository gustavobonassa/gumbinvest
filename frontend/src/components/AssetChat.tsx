/** AI analyst: per-asset on asset pages, portfolio-wide everywhere else.
 *
 * A top-bar button opens a right-side panel. `ticker` scopes the conversation:
 * a string chats about that asset (company data + the owner's position); null
 * chats about the whole portfolio (totals + every open position). The backend
 * assembles the context and streams the answer over SSE; this component only
 * keeps the conversation and renders the stream as it arrives.
 */
import clsx from "clsx";
import { ArrowDown, RotateCcw, Send, Sparkles, Square, X } from "lucide-react";
import { lazy, Suspense, useEffect, useLayoutEffect, useRef, useState } from "react";
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

// The Markdown parser is the chat's alone; it downloads when a conversation is
// first rendered, not with the app.
const ChatMarkdown = lazy(() => import("@/components/ChatMarkdown"));

/** Text exactly as written — the user's own messages, and the answer's
 *  stand-in for the moment the Markdown renderer is still loading. */
function PlainText({ text }: { text: string }) {
  // break-words: answers cite URLs longer than the bubble is wide.
  return <span className="whitespace-pre-wrap break-words">{text}</span>;
}

function MessageText({ text, markdown }: { text: string; markdown: boolean }) {
  const decoded = decodeEscapes(text);
  if (!markdown) return <PlainText text={decoded} />;
  return (
    <Suspense fallback={<PlainText text={decoded} />}>
      <ChatMarkdown text={decoded} />
    </Suspense>
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
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Whether the view follows the conversation. Changed only by the reader's
  // own scrolling (and by sending, which always means "show me the answer") —
  // never by content growing, or one long chunk would silently stop the
  // follow. A ref, not state: it is read in the same frame tokens land in.
  const followRef = useRef(true);
  // Scrolled away while something new arrived below: offer the way back.
  const [unseenBelow, setUnseenBelow] = useState(false);

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
        followRef.current = true;
        setMessages(chat.messages);
        setChatId(chat.id);
        setOpen(true);
      })
      .catch(() => setError("Não foi possível carregar a conversa salva."));
    return () => {
      cancelled = true;
    };
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

  const scrollToLatest = () => {
    const el = scrollRef.current;
    if (!el) return;
    followRef.current = true;
    setUnseenBelow(false);
    el.scrollTop = el.scrollHeight;
  };

  // Follow the stream while the reader is following; if they scrolled up to
  // reread something, new tokens must not yank them back down. A layout
  // effect, and an instant jump rather than a smooth one: it lands before the
  // paint (no flicker), and a smooth scroll restarted on every token lags
  // behind the text and fires scroll events mid-flight that read as "the
  // reader moved".
  useLayoutEffect(() => {
    if (!mounted) return;
    if (followRef.current) {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    } else if (messages.length) {
      setUnseenBelow(true);
    }
  }, [messages, status, error, mounted]);

  // Opening the panel always lands on the latest message. Keyed on `mounted`
  // too: on the first open the panel is not in the document yet when `open`
  // flips, so the scroll container only exists one render later.
  useLayoutEffect(() => {
    if (open && mounted) {
      scrollToLatest();
      inputRef.current?.focus();
    }
  }, [open, mounted]);

  // The reader's own scrolling is the only thing that changes the follow.
  // Direction matters, not just distance: in a short answer the very top is
  // within a few dozen pixels of the bottom, and a distance-only rule would
  // read "scrolled all the way up" as "still at the bottom".
  const lastTop = useRef(0);
  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const movedUp = el.scrollTop < lastTop.current;
    lastTop.current = el.scrollTop;
    if (distance <= 2) {
      // Truly at the bottom — also where a layout shift (the status line
      // giving way to text) leaves a reader who never moved.
      followRef.current = true;
    } else if (movedUp) {
      followRef.current = false;
    } else if (distance < 40) {
      followRef.current = true; // scrolled back down to the end
    }
    if (followRef.current) setUnseenBelow(false);
  };
  // A wheel or swipe toward older messages stops the follow immediately,
  // before its scroll event lands — otherwise a token arriving in between
  // would snap the view back to the bottom against the reader's hand.
  const touchY = useRef<number | null>(null);
  const releaseFollow = () => {
    followRef.current = false;
  };

  // Hand the cursor back once an answer finishes, ready for the follow-up.
  useEffect(() => {
    if (!streaming && open) inputRef.current?.focus();
  }, [streaming, open]);

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
    // Sending means "show me the answer", wherever the reader had scrolled.
    followRef.current = true;
    setUnseenBelow(false);
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
        current.length && current[current.length - 1].content === ""
          ? current.slice(0, -1)
          : current,
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
              // Square while the label is hidden, so on a phone it matches the
              // icon buttons beside it; padded again once "Analista IA" shows.
              className={clsx(
                "btn-topbar w-10 px-0 sm:w-auto sm:px-3.5",
                open && "bg-accent-soft text-accent",
              )}
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
                      {ticker
                        ? `${ticker} · dados da empresa + sua posição + web`
                        : "carteira completa + web"}
                    </p>
                  </div>
                  <div className="ml-auto flex items-center gap-1">
                    {messages.length ? (
                      <button
                        type="button"
                        onClick={startNewConversation}
                        className="btn-ghost px-2 py-1.5"
                        title="Nova conversa (a atual fica salva em Conversas)"
                      >
                        <RotateCcw size={15} />
                      </button>
                    ) : null}
                    <button
                      type="button"
                      onClick={() => setOpen(false)}
                      className="btn-ghost px-2 py-1.5"
                      aria-label="Fechar"
                    >
                      <X size={16} />
                    </button>
                  </div>
                </header>

                <div className="relative flex min-h-0 flex-1 flex-col">
                  <div
                    ref={scrollRef}
                    onScroll={onScroll}
                    onWheel={(event) => {
                      if (event.deltaY < 0) releaseFollow();
                    }}
                    onTouchStart={(event) => {
                      touchY.current = event.touches[0]?.clientY ?? null;
                    }}
                    onTouchMove={(event) => {
                      const y = event.touches[0]?.clientY;
                      // Finger moving down drags the content toward older messages.
                      if (touchY.current !== null && y !== undefined && y > touchY.current + 4) {
                        releaseFollow();
                      }
                    }}
                    onKeyDown={(event) => {
                      if (["ArrowUp", "PageUp", "Home"].includes(event.key)) releaseFollow();
                    }}
                    tabIndex={-1}
                    className="flex-1 space-y-3 overflow-y-auto overscroll-contain px-4 py-4 focus:outline-none"
                  >
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
                          <MessageText
                            text={message.content}
                            markdown={message.role === "assistant"}
                          />
                        </div>
                      ) : null,
                    )}

                    {status ? (
                      <p className="flex items-center gap-2 text-xs text-ink-muted">
                        <span
                          className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent"
                          aria-hidden
                        />
                        {status}
                      </p>
                    ) : null}
                    {error ? <p className="text-xs text-negative">{error}</p> : null}
                  </div>
                  {unseenBelow ? (
                    <button
                      type="button"
                      onClick={() => {
                        scrollToLatest();
                        inputRef.current?.focus();
                      }}
                      className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-line-strong bg-surface-raised px-3 py-1.5 text-xs font-medium text-ink shadow-raised transition-colors hover:border-accent/50 hover:text-accent"
                    >
                      <ArrowDown size={13} aria-hidden />
                      {streaming ? "Acompanhar resposta" : "Ir para o fim"}
                    </button>
                  ) : null}
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
                      ref={inputRef}
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
                      // Stays editable while an answer streams: the follow-up
                      // can be written meanwhile and sent with Enter once it ends.
                      className="input flex-1 resize-none text-sm"
                    />
                    {streaming ? (
                      <button
                        type="button"
                        onClick={() => abortRef.current?.abort()}
                        className="btn-ghost border border-line-strong px-3 py-2.5"
                        aria-label="Parar resposta"
                        title="Parar resposta"
                      >
                        <Square size={14} className="fill-current" />
                      </button>
                    ) : (
                      <button
                        type="submit"
                        className="btn-primary px-3 py-2.5"
                        disabled={!input.trim()}
                        aria-label="Enviar"
                      >
                        <Send size={15} />
                      </button>
                    )}
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
