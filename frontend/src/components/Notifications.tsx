/**
 * The bell in the header and the panel it opens.
 *
 * Renders whatever the backend registry produces (see
 * app/services/notifications.py) rather than knowing about any one kind: an
 * entry is a title, a body, an optional progress bar and optional chips. A new
 * source — a price target being hit, an import finishing — shows up here with
 * no change to this file.
 */
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { Bell, CheckCircle2, Info, TriangleAlert } from "lucide-react";
import { useRef, useState } from "react";

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

function Entry({ item }: { item: AppNotification }) {
  const Icon = LEVEL_ICON[item.level] ?? Info;
  const chips = item.items.slice(0, MAX_CHIPS);
  return (
    <li className="border-b border-line px-4 py-3 last:border-b-0">
      <div className="flex gap-2.5">
        <Icon size={15} className={clsx("mt-0.5 shrink-0", LEVEL_TONE[item.level])} aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-ink">{item.title}</p>
          <p className="mt-0.5 text-xs leading-relaxed text-ink-muted">{item.body}</p>
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
            <p className="mt-2 text-[11px] text-ink-muted">Próxima tentativa {dateTime(item.at)}</p>
          ) : null}
        </div>
      </div>
    </li>
  );
}

export default function Notifications() {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);

  // Polled rather than pushed: the only producer today resolves itself within
  // minutes, and a websocket for one self-clearing banner is not worth the
  // moving parts. Faster while the panel is open, because that is when the
  // user is watching the bar move.
  const { data } = useQuery({
    queryKey: ["notifications"],
    queryFn: api.notifications,
    refetchInterval: open ? 15_000 : 60_000,
  });

  const items = data?.items ?? [];
  const count = items.length;

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="btn-ghost relative px-2 py-2"
        aria-label={count ? `Notificações (${count})` : "Notificações"}
        title="Notificações"
      >
        <Bell size={15} aria-hidden />
        {count ? (
          <span
            className="absolute right-1 top-1 flex h-2 w-2 items-center justify-center"
            aria-hidden
          >
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
          </span>
        ) : null}
      </button>

      {open ? (
        <AnchoredPanel
          anchor={buttonRef.current}
          onClose={() => setOpen(false)}
          matchWidth={false}
          minWidth={320}
        >
          <div className="max-h-[70vh] overflow-auto">
            <div className="border-b border-line px-4 py-2.5">
              <p className="text-xs font-medium uppercase tracking-wide text-ink-muted">
                Notificações
              </p>
            </div>
            {count ? (
              <ul>
                {items.map((item) => (
                  <Entry key={item.id} item={item} />
                ))}
              </ul>
            ) : (
              <p className="px-4 py-6 text-center text-xs text-ink-muted">Nada por aqui.</p>
            )}
          </div>
        </AnchoredPanel>
      ) : null}
    </>
  );
}
