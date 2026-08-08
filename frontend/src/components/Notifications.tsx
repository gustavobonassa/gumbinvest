/**
 * The bell in the header and the panel it opens.
 *
 * Renders whatever the backend registry produces (see
 * app/services/notifications.py) rather than knowing about any one kind: an
 * entry is a title, a body, an optional progress bar and optional chips. A new
 * source — a price target being hit, an import finishing — shows up here with
 * no change to this file.
 *
 * The feed arrives in two halves. Live entries describe a condition holding
 * right now and come back whole on every poll, so they sit pinned at the top
 * and are never paged. Stored entries are past events, and those are what the
 * list pages through as it is scrolled.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Bell, CheckCircle2, Info, TriangleAlert, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { AppNotification } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { AnchoredPanel, Badge } from "@/components/ui";

const LEVEL_ICON = { info: Info, success: CheckCircle2, warning: TriangleAlert } as const;
const LEVEL_TONE = {
  info: "text-accent",
  success: "text-positive",
  warning: "text-warning",
} as const;

/** Chips beyond this are folded into a "+N" so one bad sync cannot flood the panel. */
const MAX_CHIPS = 12;

function ProgressBar({ done, total, label }: { done: number; total: number; label: string }) {
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return (
    <div className="mt-2.5">
      <div className="mb-1 flex items-baseline justify-between text-[11px] text-ink-muted">
        <span>
          {done} de {total} {label}
        </span>
        <span className="tabular-nums">{pct}%</span>
      </div>
      <div
        className="h-1.5 overflow-hidden rounded-full bg-surface-hover"
        role="progressbar"
        aria-valuenow={done}
        aria-valuemin={0}
        aria-valuemax={total}
      >
        <div
          className="h-full rounded-full bg-accent transition-[width] duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function Entry({ item, onArchive }: { item: AppNotification; onArchive: () => void }) {
  const Icon = LEVEL_ICON[item.level] ?? Info;
  const chips = item.items.slice(0, MAX_CHIPS);
  return (
    <li className="group relative border-b border-line px-4 py-3 last:border-b-0">
      <div className="flex gap-2.5">
        <Icon size={15} className={clsx("mt-0.5 shrink-0", LEVEL_TONE[item.level])} aria-hidden />
        <div className="min-w-0 flex-1">
          {/* pr leaves the archive button a lane of its own, so a long title
              never runs underneath it. */}
          <p className="break-words pr-6 text-sm font-medium text-ink">{item.title}</p>
          {/* break-words, because a body is not always prose: a broker file
              name is 130 unbroken characters, and with nowhere to wrap it set
              the panel's minimum width and gave the whole thing a horizontal
              scrollbar. Breaking mid-token is the lesser evil — the alternative
              is a list that scrolls sideways to read one line. */}
          <p className="mt-0.5 break-words text-xs leading-relaxed text-ink-muted">{item.body}</p>
          {item.progress ? <ProgressBar {...item.progress} /> : null}
          {chips.length ? (
            <div className="mt-2 flex flex-wrap gap-1">
              {chips.map((chip) => (
                <Badge key={chip}>{chip}</Badge>
              ))}
              {item.items.length > chips.length ? (
                <Badge>+{item.items.length - chips.length}</Badge>
              ) : null}
            </div>
          ) : null}
          {item.at ? (
            <p className="mt-2 text-[11px] text-ink-muted">
              {/* A live entry's timestamp is in the future — it says when the
                  next attempt lands. A stored one is when the thing happened. */}
              {item.source === "live" ? "Próxima tentativa " : ""}
              {dateTime(item.at)}
            </p>
          ) : null}
        </div>
      </div>
      {/* Always reachable by keyboard, revealed on hover for the mouse: a row
          of × marks down the panel turns a list of news into a list of chores.
          No opacity trick on touch, where there is no hover to reveal it. */}
      <button
        type="button"
        onClick={onArchive}
        aria-label={`Arquivar: ${item.title}`}
        title="Arquivar"
        className="absolute right-2 top-2.5 rounded-lg p-1 text-ink-muted opacity-0 transition-opacity hover:bg-surface-hover hover:text-ink focus-visible:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100"
      >
        <X size={13} aria-hidden />
      </button>
    </li>
  );
}

export default function Notifications() {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const queryClient = useQueryClient();

  // Two queries against one endpoint, and they are not redundant. This one is
  // the bell itself: it polls while closed so the dot can appear on its own,
  // and it asks for a single row because the only thing being read off it is
  // `unread` — the badge must not pay for a page of bodies nobody is looking at.
  const { data: badge } = useQuery({
    queryKey: ["notifications", "badge"],
    queryFn: () => api.notifications(null, 1),
    refetchInterval: open ? 15_000 : 60_000,
  });

  // Polled rather than pushed: the only live producer today resolves itself
  // within minutes, and a websocket for one self-clearing banner is not worth
  // the moving parts. Faster while the panel is open, because that is when the
  // user is watching the progress bar move.
  const feed = useInfiniteQuery({
    queryKey: ["notifications", "feed"],
    queryFn: ({ pageParam }) => api.notifications(pageParam),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next_cursor,
    enabled: open,
    refetchInterval: open ? 15_000 : false,
  });

  const markRead = useMutation({
    mutationFn: api.notificationsRead,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });
  const archive = useMutation({
    mutationFn: ({ source, id }: Pick<AppNotification, "source" | "id">) =>
      api.notificationArchive(source, id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const unread = badge?.unread ?? 0;

  // Read on close, not on open. Opening and immediately marking everything read
  // would clear the dot out from under the eyes still reading the panel — and
  // a refetch landing mid-read would then reorder nothing but drop the very
  // highlight the user is using to find their place.
  const close = () => {
    setOpen(false);
    if (unread > 0) markRead.mutate();
  };

  // Only the first page carries the live entries; every later page appends
  // history under them.
  const pages = feed.data?.pages ?? [];
  const live = pages[0]?.live ?? [];
  const stored = pages.flatMap((page) => page.items);
  const empty = !feed.isLoading && !live.length && !stored.length;

  // Infinite scroll: a sentinel at the end of the list asks for the next page
  // as it comes into view. Bound to the panel's own scrollport rather than the
  // viewport — the panel scrolls internally and the viewport never moves.
  const sentinelRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = feed;
  useEffect(() => {
    const sentinel = sentinelRef.current;
    const root = scrollRef.current;
    if (!sentinel || !root || !hasNextPage) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting && !isFetchingNextPage) fetchNextPage();
      },
      // A margin, so the next page is already in flight by the time the last
      // row is read rather than after the scroll has hit the floor.
      { root, rootMargin: "120px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage, stored.length]);

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => (open ? close() : setOpen(true))}
        className="btn-ghost relative px-2 py-2"
        aria-label={unread ? `Notificações (${unread} não lidas)` : "Notificações"}
        title="Notificações"
      >
        <Bell size={15} aria-hidden />
        {/* The dot straddles the button's top-right corner rather than sitting
            inside it, where it read as part of the bell instead of as a mark on
            top of it. Sitting *on* the edge is what the offset buys: the button
            is 33px square with a 12px radius, so the corner's own outline runs
            about 3.5px in from each side — a -1px inset lands the 8px dot's
            centre on that curve, half over the button and half over the header.
            The ping ring is free to spill past it; nothing here clips. */}
        {unread ? (
          <span
            className="absolute -right-px -top-px flex h-2 w-2 items-center justify-center"
            aria-hidden
          >
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
          </span>
        ) : null}
      </button>

      {open ? (
        <AnchoredPanel anchor={buttonRef.current} onClose={close} matchWidth={false} minWidth={320}>
          {/* Vertical only. Naming one axis makes the other compute to `auto`,
              so anything that still outgrows the fixed 320px — a pathological
              chip, a future entry kind — would reintroduce the sideways scroll
              rather than be clipped. Same backstop as the page shell. */}
          <div ref={scrollRef} className="max-h-[70vh] overflow-y-auto overflow-x-clip">
            <div className="sticky top-0 z-10 border-b border-line bg-surface-raised px-4 py-2.5">
              <p className="text-xs font-medium uppercase tracking-wide text-ink-muted">
                Notificações
              </p>
            </div>
            {empty ? (
              <p className="px-4 py-6 text-center text-xs text-ink-muted">Nada por aqui.</p>
            ) : (
              <ul>
                {[...live, ...stored].map((item) => (
                  <Entry
                    key={`${item.source}:${item.id}`}
                    item={item}
                    onArchive={() => archive.mutate({ source: item.source, id: item.id })}
                  />
                ))}
              </ul>
            )}
            {/* Sits below the list whether or not there is more: the observer
                only watches it while another page exists. */}
            <div ref={sentinelRef} aria-hidden>
              {isFetchingNextPage ? (
                <p className="px-4 py-3 text-center text-[11px] text-ink-muted">Carregando…</p>
              ) : null}
            </div>
          </div>
        </AnchoredPanel>
      ) : null}
    </>
  );
}
