/**
 * The one place a theme is decided and applied.
 *
 * The stored preference lives in the backend settings (`theme`), like every
 * other preference — but a round trip cannot be waited on before the first
 * paint, so the choice is mirrored into localStorage and re-applied by a tiny
 * inline script in `index.html` before the stylesheet paints. Without that
 * mirror a light-theme user gets a dark flash on every load.
 *
 * Reading order, therefore: the inline script (localStorage) decides what is
 * painted, and the settings query decides what is *true* — `applyTheme` is
 * called with the server's value as soon as it arrives, correcting the mirror
 * if the two ever disagree (a second machine, a restored backup).
 */
import { useSyncExternalStore } from "react";

import { setChartTheme } from "@/lib/colors";

export type Theme = "dark" | "light";

/** Also read by the inline script in index.html — keep the two in step. */
export const THEME_STORAGE_KEY = "gumbinvest.theme";

export const THEME_LABELS: Record<Theme, string> = { dark: "Escuro", light: "Claro" };

/** The colour behind the browser/OS chrome on mobile: the page's own canvas. */
const BROWSER_CHROME: Record<Theme, string> = { dark: "#0b0d11", light: "#f1f3f7" };

export function isTheme(value: unknown): value is Theme {
  return value === "dark" || value === "light";
}

function stored(): Theme {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (isTheme(value)) return value;
  } catch {
    // Private mode, or storage disabled: the default is still a valid answer.
  }
  return "dark";
}

let current: Theme = typeof window === "undefined" ? "dark" : stored();
const listeners = new Set<() => void>();

function paint(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", BROWSER_CHROME[theme]);
  // Charts draw into SVG attributes, which no stylesheet reaches — the chart
  // token module is switched in the same breath as the CSS variables.
  setChartTheme(theme);
  // Inside the desktop window, the native min/max/close buttons are painted by
  // Electron and are equally out of CSS's reach. Absent in a browser.
  void window.gumbinvest?.setTheme?.(theme);
}

/**
 * Switch the app to `theme`. Safe to call with the value already in force —
 * it returns without notifying, so the settings query re-applying its answer
 * on every refetch costs nothing.
 */
export function applyTheme(theme: Theme) {
  if (theme === current && document.documentElement.dataset.theme === theme) return;
  current = theme;
  paint(theme);
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Not being able to remember it is not a reason to refuse to apply it.
  }
  listeners.forEach((listener) => listener());
}

export function getTheme(): Theme {
  return current;
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Re-renders the caller whenever the theme changes. */
export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, getTheme, getTheme);
}

// The inline script only sets the attribute; the rest of the application —
// chart tokens, the chrome colour — is brought into line here, on first import.
if (typeof window !== "undefined") paint(current);
