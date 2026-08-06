/** Small presentational primitives shared across pages. */
import clsx from "clsx";
import {
  ArrowDownRight,
  ArrowUpRight,
  Calendar,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  X,
  type LucideIcon,
} from "lucide-react";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import { kindColor } from "@/lib/colors";
import { kindLabel, money, percent, shortDate, toneOf } from "@/lib/format";

export function Card({
  children,
  className,
  hover = true,
}: {
  children: ReactNode;
  className?: string;
  hover?: boolean;
}) {
  return <div className={clsx("card", hover && "card-hover", className)}>{children}</div>;
}

export function SectionTitle({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 className="text-base font-semibold tracking-tight text-ink">{title}</h2>
        {subtitle ? <p className="mt-0.5 text-sm text-ink-muted">{subtitle}</p> : null}
      </div>
      {action}
    </div>
  );
}

export function Delta({
  value,
  suffix,
  className,
}: {
  value: number | null | undefined;
  suffix?: string;
  className?: string;
}) {
  const tone = toneOf(value);
  const Icon = tone === "negative" ? ArrowDownRight : ArrowUpRight;
  return (
    <span
      className={clsx(
        "tnum inline-flex items-center gap-1 text-sm font-medium",
        tone === "positive" && "text-positive",
        tone === "negative" && "text-negative",
        tone === "neutral" && "text-ink-muted",
        className,
      )}
    >
      {tone !== "neutral" ? <Icon size={14} strokeWidth={2.5} aria-hidden /> : null}
      {percent(value ?? 0, 2, true)}
      {suffix ? <span className="text-ink-muted">{suffix}</span> : null}
    </span>
  );
}

/**
 * A stat tile — the right form when the answer is a single number. Never wrap
 * a chart around something a number already says.
 */
export function StatTile({
  label,
  value,
  hint,
  delta,
  icon: Icon,
  tone = "neutral",
  loading,
  className,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  delta?: number | null;
  icon?: LucideIcon;
  tone?: "neutral" | "positive" | "negative" | "accent";
  loading?: boolean;
  className?: string;
}) {
  return (
    <Card className={clsx("cq group relative overflow-hidden p-5", className)}>
      <div className="flex items-start justify-between gap-3">
        <span className="text-[13px] font-medium text-ink-muted">{label}</span>
        {Icon ? (
          <span
            className={clsx(
              "rounded-lg p-1.5 transition-colors",
              tone === "positive" && "bg-positive/10 text-positive",
              tone === "negative" && "bg-negative/10 text-negative",
              tone === "accent" && "bg-accent-soft text-accent",
              tone === "neutral" && "bg-surface-hover text-ink-secondary",
            )}
          >
            <Icon size={16} strokeWidth={2} aria-hidden />
          </span>
        ) : null}
      </div>
      {loading ? (
        <div className="mt-3 h-8 w-32 skeleton" />
      ) : (
        <p
          className={clsx(
            // The size tracks the card's own width (cqw, see .cq): when the
            // docked chat squeezes the tile, the figure shrinks instead of
            // being clipped by overflow-hidden. Caps at the old 26px.
            "tnum mt-2 text-[clamp(1.125rem,10.5cqw,1.625rem)] font-semibold leading-tight tracking-tight",
            tone === "positive" && "text-positive",
            tone === "negative" && "text-negative",
            tone !== "positive" && tone !== "negative" && "text-ink",
          )}
        >
          {value}
        </p>
      )}
      <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5">
        {delta !== undefined && delta !== null ? <Delta value={delta} /> : null}
        {hint ? <span className="min-w-0 text-xs text-ink-muted">{hint}</span> : null}
      </div>
    </Card>
  );
}

export function MoneyStat({ value, compact }: { value: number; compact?: boolean }) {
  return <span className="tnum">{money(value, { compact })}</span>;
}

export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: ReactNode;
  tone?: "neutral" | "positive" | "negative" | "accent" | "warning";
  className?: string;
}) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] font-medium",
        tone === "neutral" && "bg-surface-hover text-ink-secondary",
        tone === "positive" && "bg-positive/12 text-positive",
        tone === "negative" && "bg-negative/12 text-negative",
        tone === "accent" && "bg-accent-soft text-accent",
        tone === "warning" && "bg-warning/12 text-warning",
        className,
      )}
    >
      {children}
    </span>
  );
}

/**
 * Asset class as a labelled swatch — the same colour the class carries in every
 * chart, so a row and a slice are recognisably the same thing.
 *
 * The text stays in ink: a coloured mark beside a label carries identity far
 * more reliably than coloured text, and it keeps the tag legible whatever the
 * class hue happens to be.
 */
export function KindTag({ kind, className }: { kind: string; className?: string }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-md bg-surface-hover px-2 py-0.5 text-[11px] font-medium text-ink-secondary",
        className,
      )}
    >
      <span className="h-2 w-2 shrink-0 rounded-sm" style={{ background: kindColor(kind) }} aria-hidden />
      {kindLabel(kind)}
    </span>
  );
}

/** Centred dialog. Closes on backdrop click and on Escape; keyboard focus is
 *  trapped inside while open and returns to the opener on close. */
export function Modal({
  open,
  title,
  subtitle,
  onClose,
  children,
}: {
  open: boolean;
  title: ReactNode;
  subtitle?: ReactNode;
  onClose: () => void;
  children: ReactNode;
}) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    // Focus moves into the dialog on open and back to the opener on close —
    // without this, a keyboard user is left tabbing the page behind the modal.
    const opener = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key === "Tab" && dialogRef.current) {
        const focusables = dialogRef.current.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])',
        );
        if (!focusables.length) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = document.activeElement;
        if (event.shiftKey && (active === first || active === dialogRef.current)) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && active === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
      opener?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 px-4 pt-[10vh] backdrop-blur-sm animate-fade-in">
      {/* Click-to-close only: Escape and the × carry the keyboard path, so the
          backdrop stays out of the tab order. */}
      <button type="button" tabIndex={-1} className="absolute inset-0 cursor-default" aria-hidden onClick={onClose} />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="relative w-full max-w-lg animate-scale-in overflow-hidden rounded-2xl border border-line-strong bg-surface-raised shadow-raised outline-none"
      >
        <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div>
            <p id={titleId} className="font-semibold tracking-tight text-ink">
              {title}
            </p>
            {subtitle ? <p className="mt-0.5 text-sm text-ink-muted">{subtitle}</p> : null}
          </div>
          <button type="button" onClick={onClose} className="btn-ghost px-2.5 py-2.5" aria-label="Fechar">
            <X size={16} />
          </button>
        </div>
        <div className="max-h-[62vh] overflow-auto px-5 py-4">{children}</div>
      </div>
    </div>
  );
}

/**
 * A panel pinned to an anchor element, rendered in a portal.
 *
 * Portalled and positioned in viewport coordinates on purpose: these popups
 * open from inside cards and horizontally scrolling filter rows, and anything
 * laid out in the normal flow gets clipped by the first `overflow` ancestor.
 * It closes on scroll rather than tracking it — a popup that follows the page
 * while the anchor slides away costs more than it gives.
 */
function AnchoredPanel({
  anchor,
  onClose,
  children,
  matchWidth = true,
  minWidth = 180,
}: {
  anchor: HTMLElement | null;
  onClose: () => void;
  children: ReactNode;
  matchWidth?: boolean;
  minWidth?: number;
}) {
  const panelRef = useRef<HTMLDivElement>(null);

  // Laid out at the anchor from the first paint (the width has to be right
  // before the content is measured, or wrapping changes the height), then
  // adjusted once the real height is known.
  const place = (height: number): CSSProperties => {
    const rect = anchor?.getBoundingClientRect();
    if (!rect) return { position: "fixed", visibility: "hidden" };
    const width = Math.max(matchWidth ? rect.width : 0, minWidth);
    const below = window.innerHeight - rect.bottom;
    // Flip above when the space below cannot hold the panel but the space
    // above can — otherwise it stays below and scrolls internally.
    const flip = height > 0 && below < height + 12 && rect.top > below;
    return {
      position: "fixed",
      top: flip ? Math.max(8, rect.top - height - 6) : rect.bottom + 6,
      left: Math.min(Math.max(8, rect.left), Math.max(8, window.innerWidth - width - 8)),
      width,
      zIndex: 60,
    };
  };

  const [style, setStyle] = useState<CSSProperties>(() => place(0));

  useLayoutEffect(() => {
    const height = panelRef.current?.offsetHeight ?? 0;
    const next = place(height);
    setStyle((current) => (current.top === next.top && current.left === next.left ? current : next));
    // Placement depends on the anchor and the measured height, both settled by
    // the time this runs; re-running on every render would loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anchor]);

  useEffect(() => {
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (panelRef.current?.contains(target) || anchor?.contains(target)) return;
      onClose();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    // The page scrolling out from under the panel closes it — but a long list
    // scrolling *inside* it is the user reading the options, not leaving.
    const onScroll = (event: Event) => {
      const target = event.target;
      if (target instanceof Node && panelRef.current?.contains(target)) return;
      onClose();
    };
    window.addEventListener("mousedown", onPointerDown);
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", onClose);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      window.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [anchor, onClose]);

  return createPortal(
    <div
      ref={panelRef}
      style={style}
      className="animate-scale-in overflow-hidden rounded-xl border border-line-strong bg-surface-raised p-1 shadow-raised"
    >
      {children}
    </div>,
    document.body,
  );
}

/**
 * A select whose open list belongs to the app rather than to the OS — the
 * native one cannot be styled, and a grey system menu over a dark card is the
 * one place the interface stops looking like itself.
 */
export function Select<T extends string>({
  value,
  onChange,
  options,
  className,
  disabled,
  ariaLabel,
  id,
}: {
  value: T;
  onChange: (value: T) => void;
  options: { value: T; label: string; hint?: string }[];
  className?: string;
  disabled?: boolean;
  ariaLabel?: string;
  id?: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const selected = options.find((option) => option.value === value);

  const openList = () => {
    setActive(Math.max(0, options.findIndex((option) => option.value === value)));
    setOpen(true);
  };

  const commit = (index: number) => {
    const option = options[index];
    if (option) onChange(option.value);
    setOpen(false);
    buttonRef.current?.focus();
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (!open) {
      if (["Enter", " ", "ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault();
        openList();
      }
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current) => {
        const next = current + (event.key === "ArrowDown" ? 1 : -1);
        return (next + options.length) % options.length;
      });
    }
    if (event.key === "Home") setActive(0);
    if (event.key === "End") setActive(options.length - 1);
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      commit(active);
    }
    if (event.key === "Tab") setOpen(false);
  };

  return (
    <>
      <button
        id={id}
        ref={buttonRef}
        type="button"
        role="combobox"
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => (open ? setOpen(false) : openList())}
        onKeyDown={onKeyDown}
        className={clsx(
          "input flex items-center justify-between gap-2 text-left disabled:cursor-not-allowed disabled:opacity-50",
          open && "border-accent/60",
          className,
        )}
      >
        <span className={clsx("truncate", !selected && "text-ink-muted")}>{selected?.label ?? "—"}</span>
        <ChevronDown
          size={15}
          className={clsx("shrink-0 text-ink-muted transition-transform duration-200", open && "rotate-180")}
          aria-hidden
        />
      </button>

      {open ? (
        <AnchoredPanel anchor={buttonRef.current} onClose={() => setOpen(false)}>
          <ul role="listbox" className="max-h-[280px] overflow-auto" aria-label={ariaLabel}>
            {options.map((option, index) => {
              const isSelected = option.value === value;
              return (
                <li key={option.value}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={isSelected}
                    onMouseEnter={() => setActive(index)}
                    onClick={() => commit(index)}
                    className={clsx(
                      "flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm transition-colors",
                      index === active ? "bg-surface-hover text-ink" : "text-ink-secondary",
                    )}
                  >
                    <Check
                      size={14}
                      className={clsx("shrink-0 text-accent", !isSelected && "opacity-0")}
                      aria-hidden
                    />
                    {/* Hint below the label, both truncating: a long hint must
                        never widen the row past the panel (it used to force a
                        stray scrollbar inside the list). */}
                    <span className="min-w-0 flex-1">
                      <span className="block truncate">{option.label}</span>
                      {option.hint ? (
                        <span className="block truncate text-xs text-ink-muted">{option.hint}</span>
                      ) : null}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </AnchoredPanel>
      ) : null}
    </>
  );
}

/**
 * The same control for a filter that takes several values at once.
 *
 * The alternative — one toggle chip per option, laid out in a row — stops
 * working the moment there are more than a handful: a ledger with fifteen kinds
 * of movement turns the filter bar into two lines of buttons that push the data
 * off the screen. Collapsed into a trigger, the filter costs one line whatever
 * the vocabulary grows to, and the summary says what is on without opening it.
 */
export function MultiSelect<T extends string>({
  values,
  onChange,
  options,
  className,
  ariaLabel,
  placeholder = "Todos",
  id,
}: {
  values: T[];
  onChange: (values: T[]) => void;
  options: { value: T; label: string; hint?: string }[];
  className?: string;
  ariaLabel?: string;
  /** Shown when nothing is picked — "no filter" reads better than "0 selected". */
  placeholder?: string;
  id?: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const chosen = new Set<string>(values);

  const toggle = (value: T) =>
    onChange(chosen.has(value) ? values.filter((item) => item !== value) : [...values, value]);

  const summary = !values.length
    ? placeholder
    : values.length === 1
      ? (options.find((option) => option.value === values[0])?.label ?? String(values[0]))
      : `${values.length} selecionados`;

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (!open) {
      if (["Enter", " ", "ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault();
        setOpen(true);
      }
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current) => {
        const next = current + (event.key === "ArrowDown" ? 1 : -1);
        return (next + options.length) % options.length;
      });
    }
    // Enter and Space pick without closing: choosing several is the point.
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      const option = options[active];
      if (option) toggle(option.value);
    }
    if (event.key === "Escape" || event.key === "Tab") setOpen(false);
  };

  return (
    <>
      <button
        id={id}
        ref={buttonRef}
        type="button"
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={onKeyDown}
        className={clsx(
          "input flex items-center justify-between gap-2 text-left",
          open && "border-accent/60",
          className,
        )}
      >
        <span className={clsx("flex min-w-0 items-center gap-2", !values.length && "text-ink-muted")}>
          <span className="truncate">{summary}</span>
          {values.length > 1 ? (
            <span className="tnum shrink-0 rounded-md bg-accent-soft px-1.5 py-0.5 text-[11px] font-medium text-accent">
              {values.length}
            </span>
          ) : null}
        </span>
        <ChevronDown
          size={15}
          className={clsx("shrink-0 text-ink-muted transition-transform duration-200", open && "rotate-180")}
          aria-hidden
        />
      </button>

      {open ? (
        <AnchoredPanel anchor={buttonRef.current} onClose={() => setOpen(false)}>
          <ul role="listbox" aria-multiselectable className="max-h-[300px] overflow-auto" aria-label={ariaLabel}>
            {options.map((option, index) => {
              const isSelected = chosen.has(option.value);
              return (
                <li key={option.value}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={isSelected}
                    onMouseEnter={() => setActive(index)}
                    onClick={() => toggle(option.value)}
                    className={clsx(
                      "flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm transition-colors",
                      index === active ? "bg-surface-hover text-ink" : "text-ink-secondary",
                    )}
                  >
                    {/* A box rather than a tick: it says "several may be on"
                        before anything is chosen. */}
                    <span
                      className={clsx(
                        "grid h-4 w-4 shrink-0 place-items-center rounded border transition-colors",
                        isSelected ? "border-accent bg-accent text-canvas" : "border-line-strong",
                      )}
                      aria-hidden
                    >
                      {isSelected ? <Check size={11} strokeWidth={3} /> : null}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{option.label}</span>
                  </button>
                </li>
              );
            })}
          </ul>
          {values.length ? (
            <button
              type="button"
              onClick={() => onChange([])}
              className="mt-1 w-full rounded-lg border-t border-line px-2.5 py-2 text-left text-xs text-ink-muted transition-colors hover:text-ink"
            >
              Limpar seleção
            </button>
          ) : null}
        </AnchoredPanel>
      ) : null}
    </>
  );
}

/**
 * A text field that suggests, without ever refusing what is typed.
 *
 * The difference from `Select` is the whole point: a broker the ledger has
 * never seen and a ticker nobody imported yet are both *valid* — the first
 * entry for anything is by definition not in the list. So the options are a
 * shortcut, not a vocabulary, and the value is always whatever is in the box.
 */
export function Combobox({
  value,
  onChange,
  onPick,
  options,
  placeholder,
  ariaLabel,
  className,
  inputClassName,
  emptyHint,
  id,
}: {
  value: string;
  onChange: (value: string) => void;
  /** Fired only when a suggestion is taken — typing does not call it. */
  onPick?: (value: string) => void;
  options: { value: string; label: string; hint?: string }[];
  placeholder?: string;
  ariaLabel?: string;
  className?: string;
  inputClassName?: string;
  /** Shown when nothing matches, to say that typing on is fine. */
  emptyHint?: string;
  id?: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // The list narrows as you type, and ranks: what *starts* with the text beats
  // what merely contains it, and a match on the code beats a match on the
  // description. Without this, typing "PETR" answers with an option series and
  // three American refiners before it answers with Petrobras. Ties keep the
  // caller's order, which is deliberate — it arrives sorted by relevance
  // already (held assets first, by size).
  const needle = value.trim().toLowerCase();
  const rank = (option: { value: string; label: string }) => {
    const code = option.value.toLowerCase();
    const label = option.label.toLowerCase();
    if (code === needle) return 0;
    if (code.startsWith(needle)) return 1;
    if (label.startsWith(needle)) return 2;
    if (code.includes(needle)) return 3;
    return 4;
  };
  const matches = needle
    ? options
        .filter(
          (option) =>
            option.value.toLowerCase().includes(needle) || option.label.toLowerCase().includes(needle),
        )
        .map((option, index) => ({ option, index, rank: rank(option) }))
        .sort((a, b) => a.rank - b.rank || a.index - b.index)
        .map((entry) => entry.option)
    : options;
  const visible = matches.slice(0, 30);

  useEffect(() => setActive(0), [needle]);

  const pick = (option: { value: string }) => {
    onChange(option.value);
    onPick?.(option.value);
    setOpen(false);
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      setActive((current) => {
        const next = current + (event.key === "ArrowDown" ? 1 : -1);
        return visible.length ? (next + visible.length) % visible.length : 0;
      });
    }
    // Enter takes the highlighted suggestion when the list is open, and
    // otherwise does nothing — it must never swallow a value typed in full.
    if (event.key === "Enter" && open && visible[active]) {
      event.preventDefault();
      pick(visible[active]);
    }
    if (event.key === "Escape") setOpen(false);
    if (event.key === "Tab") setOpen(false);
  };

  return (
    <div className={clsx("relative", className)}>
      <input
        id={id}
        ref={inputRef}
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-autocomplete="list"
        aria-label={ariaLabel}
        autoComplete="off"
        placeholder={placeholder}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
        className={clsx("input", inputClassName)}
      />
      {open && (visible.length || emptyHint) ? (
        <AnchoredPanel anchor={inputRef.current} onClose={() => setOpen(false)}>
          {visible.length ? (
            <ul role="listbox" className="max-h-[260px] overflow-auto" aria-label={ariaLabel}>
              {visible.map((option, index) => (
                <li key={option.value}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={index === active}
                    onMouseEnter={() => setActive(index)}
                    // `mousedown` rather than `click`: the input's blur would
                    // otherwise close the panel before the click landed.
                    onMouseDown={(event) => {
                      event.preventDefault();
                      pick(option);
                    }}
                    className={clsx(
                      "flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm transition-colors",
                      index === active ? "bg-surface-hover text-ink" : "text-ink-secondary",
                    )}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-medium text-ink">{option.value}</span>
                      {option.label && option.label !== option.value ? (
                        <span className="block truncate text-xs text-ink-muted">{option.label}</span>
                      ) : null}
                    </span>
                    {option.hint ? (
                      <span className="shrink-0 text-xs text-ink-muted">{option.hint}</span>
                    ) : null}
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="px-2.5 py-2 text-xs text-ink-muted">{emptyHint}</p>
          )}
        </AnchoredPanel>
      ) : null}
    </div>
  );
}

const WEEKDAYS = ["dom", "seg", "ter", "qua", "qui", "sex", "sáb"];
const MONTHS = [
  "Janeiro",
  "Fevereiro",
  "Março",
  "Abril",
  "Maio",
  "Junho",
  "Julho",
  "Agosto",
  "Setembro",
  "Outubro",
  "Novembro",
  "Dezembro",
];

/** ISO day key for a local date — `toISOString` would shift it by the offset. */
const isoDay = (date: Date) =>
  `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;

/**
 * A date field that reads dd/mm/aaaa and picks from a calendar.
 *
 * `<input type="date">` renders in the *browser's* locale, so a pt-BR portfolio
 * on an en-US browser shows 03/07 meaning July 3rd — the one formatting error
 * that silently changes what a filter means. The value stays ISO, so callers
 * and the API are unchanged.
 */
export function DateField({
  value,
  onChange,
  ariaLabel,
  placeholder = "dd/mm/aaaa",
  className,
  id,
}: {
  value: string;
  onChange: (value: string) => void;
  ariaLabel?: string;
  placeholder?: string;
  className?: string;
  id?: string;
}) {
  const [open, setOpen] = useState(false);
  // Days -> months -> years, drilled into from the header. A ledger goes back
  // years, and paging one month at a time to reach 2011 is not navigation.
  const [view, setView] = useState<"days" | "months" | "years">("days");
  const buttonRef = useRef<HTMLButtonElement>(null);
  const selected = value ? new Date(`${value}T00:00:00`) : null;
  const [month, setMonth] = useState(() => selected ?? new Date());

  // Reopening on a value set elsewhere should land on that month, not on the
  // one browsed to last time.
  useEffect(() => {
    if (open) {
      setMonth(value ? new Date(`${value}T00:00:00`) : new Date());
      setView("days");
    }
  }, [open, value]);

  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const days: (Date | null)[] = [
    ...Array.from({ length: first.getDay() }, () => null),
    ...Array.from(
      { length: new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate() },
      (_, index) => new Date(month.getFullYear(), month.getMonth(), index + 1),
    ),
  ];
  const today = isoDay(new Date());

  // One step of the header arrows: a month, a year, or a page of twelve years,
  // depending on what is on screen.
  const shift = (steps: number) =>
    setMonth((current) => {
      if (view === "days") return new Date(current.getFullYear(), current.getMonth() + steps, 1);
      const years = view === "months" ? steps : steps * 12;
      return new Date(current.getFullYear() + years, current.getMonth(), 1);
    });

  const pick = (date: Date) => {
    onChange(isoDay(date));
    setOpen(false);
    buttonRef.current?.focus();
  };

  // Years are paged twelve at a time, aligned so a page always starts on a
  // multiple of twelve — the same page for a given year every time.
  const yearPageStart = Math.floor(month.getFullYear() / 12) * 12;
  const headerLabel =
    view === "days"
      ? `${MONTHS[month.getMonth()]} ${month.getFullYear()}`
      : view === "months"
        ? String(month.getFullYear())
        : `${yearPageStart} – ${yearPageStart + 11}`;

  return (
    <>
      <button
        id={id}
        ref={buttonRef}
        type="button"
        aria-label={ariaLabel}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        className={clsx(
          "input flex items-center gap-2 text-left",
          open && "border-accent/60",
          className ?? "w-auto min-w-[150px]",
        )}
      >
        <Calendar size={15} className="shrink-0 text-ink-muted" aria-hidden />
        <span className={clsx("tnum flex-1 truncate", !value && "text-ink-muted")}>
          {value ? shortDate(value) : placeholder}
        </span>
        {value ? (
          <span
            role="button"
            tabIndex={-1}
            aria-label="Limpar data"
            onClick={(event) => {
              event.stopPropagation();
              onChange("");
            }}
            // Negative margins keep the field height; the padding is hit area.
            className="-my-2 shrink-0 rounded p-2 text-ink-muted hover:text-ink"
          >
            <X size={14} />
          </span>
        ) : null}
      </button>

      {open ? (
        <AnchoredPanel anchor={buttonRef.current} onClose={() => setOpen(false)} matchWidth={false} minWidth={268}>
          <div className="p-2">
            <div className="mb-2 flex items-center justify-between gap-2">
              <button
                type="button"
                onClick={() => shift(-1)}
                className="btn-ghost px-1.5 py-1"
                aria-label="Anterior"
              >
                <ChevronLeft size={15} />
              </button>
              {/* The label is the way up a level: month -> year -> decade. */}
              <button
                type="button"
                onClick={() => setView(view === "days" ? "months" : view === "months" ? "years" : "days")}
                className="rounded-lg px-2 py-1 text-sm font-medium text-ink transition-colors hover:bg-surface-hover"
                aria-label={view === "years" ? "Voltar aos dias" : "Escolher período"}
              >
                {headerLabel}
              </button>
              <button
                type="button"
                onClick={() => shift(1)}
                className="btn-ghost px-1.5 py-1"
                aria-label="Próximo"
              >
                <ChevronRight size={15} />
              </button>
            </div>

            {view === "days" ? (
              <>
                <div className="grid grid-cols-7 gap-0.5 text-center text-[11px] text-ink-muted">
                  {WEEKDAYS.map((day) => (
                    <span key={day} className="py-1">
                      {day}
                    </span>
                  ))}
                </div>
                <div className="grid grid-cols-7 gap-0.5">
                  {days.map((date, index) => {
                    if (!date) return <span key={`empty-${index}`} />;
                    const key = isoDay(date);
                    return (
                      <button
                        key={key}
                        type="button"
                        onClick={() => pick(date)}
                        aria-current={key === today ? "date" : undefined}
                        className={clsx(
                          "tnum grid h-8 place-items-center rounded-lg text-sm transition-colors",
                          key === value
                            ? "bg-accent font-medium text-white"
                            : key === today
                              ? "text-accent hover:bg-surface-hover"
                              : "text-ink-secondary hover:bg-surface-hover hover:text-ink",
                        )}
                      >
                        {date.getDate()}
                      </button>
                    );
                  })}
                </div>
              </>
            ) : view === "months" ? (
              <div className="grid grid-cols-3 gap-1">
                {MONTHS.map((name, index) => {
                  const isCurrent =
                    selected?.getFullYear() === month.getFullYear() && selected.getMonth() === index;
                  return (
                    <button
                      key={name}
                      type="button"
                      onClick={() => {
                        setMonth(new Date(month.getFullYear(), index, 1));
                        setView("days");
                      }}
                      className={clsx(
                        "rounded-lg px-2 py-2 text-sm transition-colors",
                        isCurrent
                          ? "bg-accent font-medium text-white"
                          : "text-ink-secondary hover:bg-surface-hover hover:text-ink",
                      )}
                    >
                      {name.slice(0, 3)}
                    </button>
                  );
                })}
              </div>
            ) : (
              <div className="grid grid-cols-4 gap-1">
                {Array.from({ length: 12 }, (_, index) => yearPageStart + index).map((year) => (
                  <button
                    key={year}
                    type="button"
                    onClick={() => {
                      setMonth(new Date(year, month.getMonth(), 1));
                      setView("months");
                    }}
                    className={clsx(
                      "tnum rounded-lg px-2 py-2 text-sm transition-colors",
                      selected?.getFullYear() === year
                        ? "bg-accent font-medium text-white"
                        : "text-ink-secondary hover:bg-surface-hover hover:text-ink",
                    )}
                  >
                    {year}
                  </button>
                ))}
              </div>
            )}

            <div className="mt-2 flex items-center justify-between gap-2 border-t border-line pt-2">
              <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={() => pick(new Date())}>
                Hoje
              </button>
              <button
                type="button"
                className="btn-ghost px-2 py-1 text-xs"
                onClick={() => {
                  onChange("");
                  setOpen(false);
                }}
              >
                Limpar
              </button>
            </div>
          </div>
        </AnchoredPanel>
      ) : null}
    </>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("skeleton", className)} />;
}

export function EmptyState({
  title,
  description,
  icon: Icon,
  action,
}: {
  title: string;
  description?: string;
  icon?: LucideIcon;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-line px-6 py-14 text-center">
      {Icon ? (
        <span className="rounded-2xl bg-surface-raised p-3 text-ink-muted">
          <Icon size={22} aria-hidden />
        </span>
      ) : null}
      <div>
        <p className="font-medium text-ink">{title}</p>
        {description ? <p className="mt-1 max-w-sm text-sm text-ink-muted">{description}</p> : null}
      </div>
      {action}
    </div>
  );
}

/**
 * Range readout plus prev/next, and optionally the page size itself — a list
 * long enough to paginate is one where "show me more at once" is a reasonable
 * next thought. A size of 0 means "all", which turns the arrows off.
 */
export function Pager({
  page,
  pages,
  total,
  pageSize,
  onChange,
  noun = "registros",
  pageSizeOptions,
  onPageSizeChange,
}: {
  page: number;
  pages: number;
  total: number;
  pageSize: number;
  onChange: (page: number) => void;
  noun?: string;
  pageSizeOptions?: number[];
  onPageSizeChange?: (pageSize: number) => void;
}) {
  const sizable = pageSizeOptions !== undefined && onPageSizeChange !== undefined;
  // Without a size control there is nothing to say about a single-page list.
  if (total <= pageSize && !sizable) return null;
  const size = pageSize > 0 ? pageSize : total;
  const first = total === 0 ? 0 : (page - 1) * size + 1;

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3 text-sm">
      <span className="text-ink-muted">
        {first}–{Math.min(page * size, total)} de <span className="tnum">{total}</span> {noun}
      </span>
      <div className="flex items-center gap-3">
        {sizable ? (
          <span className="flex items-center gap-2 text-ink-muted">
            <span className="hidden sm:inline">Por página</span>
            <Select
              ariaLabel="Itens por página"
              className="w-auto min-w-[92px] py-1"
              value={String(pageSize)}
              onChange={(next) => onPageSizeChange(Number(next))}
              options={pageSizeOptions.map((option) => ({
                value: String(option),
                label: option === 0 ? "Todos" : String(option),
              }))}
            />
          </span>
        ) : null}
        {pages > 1 ? (
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="btn-ghost px-3 py-2.5"
              disabled={page <= 1}
              onClick={() => onChange(page - 1)}
              aria-label="Página anterior"
            >
              <ChevronLeft size={16} />
            </button>
            <span className="tnum text-ink-secondary">
              {page} / {pages || 1}
            </span>
            <button
              type="button"
              className="btn-ghost px-3 py-2.5"
              disabled={page >= pages}
              onClick={() => onChange(page + 1)}
              aria-label="Próxima página"
            >
              <ChevronRight size={16} />
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  size = "md",
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  size?: "sm" | "md";
}) {
  return (
    <div className="inline-flex rounded-xl border border-line bg-surface-raised p-0.5">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          aria-pressed={value === option.value}
          className={clsx(
            "rounded-[10px] font-medium transition-all duration-200 ease-premium",
            size === "sm" ? "px-2.5 py-1.5 text-xs" : "px-3 py-2 text-sm",
            value === option.value
              ? "bg-surface-hover text-ink shadow-sm"
              : "text-ink-muted hover:text-ink-secondary",
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/**
 * Page-level tabs. Distinct from `Segmented`, which filters what a single view
 * shows — a tab swaps the view itself, so it reads as navigation and lives
 * directly under the page header.
 */
export function Tabs<T extends string>({
  value,
  options,
  onChange,
  className,
}: {
  value: T;
  options: { value: T; label: string; icon?: LucideIcon; count?: number; color?: string }[];
  onChange: (value: T) => void;
  className?: string;
}) {
  // Arrow keys move between tabs, as a tablist is expected to.
  const move = (delta: number) => {
    const index = options.findIndex((option) => option.value === value);
    const next = options[(index + delta + options.length) % options.length];
    if (next) onChange(next.value);
  };

  return (
    <div className={clsx("no-scrollbar flex gap-1 overflow-x-auto border-b border-line", className)} role="tablist">
      {options.map((option) => {
        const active = option.value === value;
        const Icon = option.icon;
        return (
          <button
            key={option.value}
            type="button"
            role="tab"
            aria-selected={active}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(option.value)}
            onKeyDown={(event) => {
              if (event.key === "ArrowRight") {
                event.preventDefault();
                move(1);
              }
              if (event.key === "ArrowLeft") {
                event.preventDefault();
                move(-1);
              }
            }}
            // A tab carrying its own colour underlines in it, so the class the
            // table is showing is identifiable without reading the label.
            style={option.color && active ? { borderBottomColor: option.color } : undefined}
            className={clsx(
              "-mb-px flex shrink-0 items-center gap-2 border-b-2 px-3 py-2.5 text-sm font-medium transition-colors duration-200 ease-premium",
              active
                ? clsx("text-ink", !option.color && "border-accent")
                : "border-transparent text-ink-muted hover:border-line-strong hover:text-ink-secondary",
            )}
          >
            {option.color ? (
              <span
                className="h-2 w-2 shrink-0 rounded-sm transition-opacity"
                style={{ background: option.color, opacity: active ? 1 : 0.55 }}
                aria-hidden
              />
            ) : Icon ? (
              <Icon size={15} aria-hidden />
            ) : null}
            {option.label}
            {option.count !== undefined ? (
              <span
                className={clsx(
                  "tnum rounded-md px-1.5 py-0.5 text-[11px] font-medium",
                  active && !option.color && "bg-accent-soft text-accent",
                  active && option.color && "text-ink",
                  !active && "bg-surface-hover text-ink-muted",
                )}
                style={
                  option.color && active
                    ? { background: `color-mix(in srgb, ${option.color} 18%, transparent)` }
                    : undefined
                }
              >
                {option.count}
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const message = error instanceof Error ? error.message : "Erro inesperado";
  return (
    <div className="rounded-2xl border border-negative/30 bg-negative/5 px-5 py-4 text-sm text-ink-secondary">
      <p className="font-medium text-negative">Não foi possível carregar os dados</p>
      {/* break-words: API errors can carry URLs/identifiers longer than a
          phone screen, and this card is often the whole page. */}
      <p className="mt-1 break-words">{message}</p>
      {retry ? (
        <button type="button" onClick={retry} className="btn-ghost mt-3">
          Tentar novamente
        </button>
      ) : null}
    </div>
  );
}
