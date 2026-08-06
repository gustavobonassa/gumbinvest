/**
 * Transient feedback for actions the user just took.
 *
 * Confirmation belongs next to the user's attention, not next to the control:
 * a "Salvo." pinned beside a button competes with the form's own layout, moves
 * things around, and is missed as often as it is read. A toast says the same
 * thing in one place, for every action in the app, and then goes away.
 *
 * The stack is bottom-right, newest at the bottom (closest to where the eye
 * lands after a click), capped so a burst of actions cannot cover the page.
 * Errors stay longer than confirmations and are announced assertively — a
 * failure the user misses is a figure they believe was saved.
 */
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

export type ToastTone = "success" | "error" | "warning" | "info";

interface ToastOptions {
  /** Second line — detail, never a repeat of the message. */
  description?: string;
  /** Milliseconds on screen; defaults per tone. */
  duration?: number;
}

interface ToastItem extends ToastOptions {
  id: number;
  tone: ToastTone;
  message: string;
  /** Bumped when an identical toast is raised again, to restart its timer. */
  nonce: number;
}

export interface ToastApi {
  show: (tone: ToastTone, message: string, options?: ToastOptions) => void;
  success: (message: string, options?: ToastOptions) => void;
  /** `cause` is the caught value — its message becomes the detail line. */
  error: (message: string, cause?: unknown, options?: ToastOptions) => void;
  warning: (message: string, options?: ToastOptions) => void;
  info: (message: string, options?: ToastOptions) => void;
  dismiss: (id: number) => void;
}

const DURATIONS: Record<ToastTone, number> = {
  success: 4000,
  info: 5000,
  warning: 7000,
  error: 9000,
};

const MAX_VISIBLE = 4;

const ICONS: Record<ToastTone, typeof CheckCircle2> = {
  success: CheckCircle2,
  error: XCircle,
  warning: AlertTriangle,
  info: Info,
};

const ToastContext = createContext<ToastApi | null>(null);

/** The message inside whatever was thrown, when there is one worth showing. */
export function describeError(cause: unknown): string | undefined {
  if (cause instanceof Error) return cause.message || undefined;
  if (typeof cause === "string") return cause || undefined;
  return undefined;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const show = useCallback((tone: ToastTone, message: string, options?: ToastOptions) => {
    setToasts((current) => {
      // Clicking a button twice should reset the timer, not stack two identical
      // cards — the second click told us nothing new.
      const duplicate = current.find(
        (toast) =>
          toast.tone === tone &&
          toast.message === message &&
          toast.description === options?.description,
      );
      if (duplicate) {
        return current.map((toast) =>
          toast.id === duplicate.id ? { ...toast, nonce: toast.nonce + 1 } : toast,
        );
      }
      const item: ToastItem = { ...options, id: nextId.current++, tone, message, nonce: 0 };
      return [...current, item].slice(-MAX_VISIBLE);
    });
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      show,
      dismiss,
      success: (message, options) => show("success", message, options),
      warning: (message, options) => show("warning", message, options),
      info: (message, options) => show("info", message, options),
      error: (message, cause, options) =>
        show("error", message, { ...options, description: options?.description ?? describeError(cause) }),
    }),
    [show, dismiss],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      {createPortal(
        <div
          // `pointer-events-none` on the stack, restored per card: an empty
          // strip of toast column must never swallow clicks on the page.
          className="pointer-events-none fixed inset-x-0 bottom-0 z-[80] flex flex-col items-end gap-2 p-4 sm:inset-x-auto sm:right-0"
          aria-live="polite"
          aria-relevant="additions"
        >
          {toasts.map((toast) => (
            <ToastCard key={toast.id} toast={toast} onDismiss={() => dismiss(toast.id)} />
          ))}
        </div>,
        document.body,
      )}
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const api = useContext(ToastContext);
  if (!api) throw new Error("useToast precisa de um <ToastProvider> acima na árvore.");
  return api;
}

function ToastCard({ toast, onDismiss }: { toast: ToastItem; onDismiss: () => void }) {
  const [leaving, setLeaving] = useState(false);
  const [paused, setPaused] = useState(false);
  const Icon = ICONS[toast.tone];

  const close = useCallback(() => {
    setLeaving(true);
    window.setTimeout(onDismiss, 180); // let the exit animation finish
  }, [onDismiss]);

  // Reading a toast holds it: the countdown stops while the pointer is on the
  // card, and starts over when it leaves. On touch there is no hover, so a tap
  // pins the card (and a second tap lets it go) — otherwise an error message
  // vanishes before it can be read, taking the only copy of the failure along.
  useEffect(() => {
    if (paused || leaving) return undefined;
    const timer = window.setTimeout(close, toast.duration ?? DURATIONS[toast.tone]);
    return () => window.clearTimeout(timer);
  }, [close, paused, leaving, toast.duration, toast.tone, toast.nonce]);

  return (
    <div
      role={toast.tone === "error" ? "alert" : "status"}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onClick={() => setPaused((current) => !current)}
      className={`pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-xl border px-4 py-3 shadow-raised ${
        paused ? "border-accent/50" : "border-line-strong"
      } bg-surface-raised ${leaving ? "animate-toast-out" : "animate-toast-in"}`}
    >
      <Icon
        size={17}
        className={`mt-0.5 shrink-0 ${
          toast.tone === "success"
            ? "text-positive"
            : toast.tone === "error"
              ? "text-negative"
              : toast.tone === "warning"
                ? "text-warning"
                : "text-accent"
        }`}
        aria-hidden
      />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-ink">{toast.message}</p>
        {toast.description ? (
          <p className="mt-0.5 break-words text-xs text-ink-secondary">{toast.description}</p>
        ) : null}
      </div>
      <button
        type="button"
        onClick={(event) => {
          // The card's own click toggles the pause; closing must not also pin.
          event.stopPropagation();
          close();
        }}
        aria-label="Fechar aviso"
        className="-m-2 -mr-2.5 shrink-0 rounded-lg p-2 text-ink-muted transition-colors hover:bg-surface-hover hover:text-ink"
      >
        <X size={15} />
      </button>
    </div>
  );
}
