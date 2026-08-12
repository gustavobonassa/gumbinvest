/**
 * Modo privacidade: every amount on screen replaced by a mask.
 *
 * The point is the person standing behind you — a call being shared, a laptop
 * on a train — so it has to be one click away and it has to be *complete*: what
 * is hidden is anything that says how much you have (money and quantities),
 * while percentages, dates, tickers and public fundamentals stay, and the app
 * stays useful with the numbers off.
 *
 * Structured like `lib/theme.ts`, and for the same reason: the preference is a
 * backend setting, but pages paint as soon as their own queries resolve, which
 * can be *before* the settings query answers. A stored choice that is only
 * applied on the round trip would flash the balances it exists to hide, so the
 * choice is mirrored to localStorage and read synchronously at module load.
 *
 * Toggling notifies subscribers; `App` subscribes, and since nothing in the
 * tree is memoised its re-render reaches every formatted value — no remount, so
 * scroll position and open panels survive a flick of the switch.
 */
import { useSyncExternalStore } from "react";

import { setValuesHidden } from "@/lib/format";

const STORAGE_KEY = "gumbinvest.hideValues";

let current = read();
const listeners = new Set<() => void>();

function read(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function applyValuesHidden(hidden: boolean) {
  if (hidden === current) return;
  current = hidden;
  setValuesHidden(hidden);
  try {
    window.localStorage.setItem(STORAGE_KEY, hidden ? "1" : "0");
  } catch {
    // Not being able to remember it is not a reason to refuse to apply it.
  }
  listeners.forEach((listener) => listener());
}

export function getValuesHidden(): boolean {
  return current;
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Re-renders the caller whenever the switch is flicked. */
export function useValuesHidden(): boolean {
  return useSyncExternalStore(subscribe, getValuesHidden, getValuesHidden);
}

// The formatter is a plain module with no idea where the flag came from; bring
// it into line with what was mirrored, before the first render reads it.
if (typeof window !== "undefined" && current) setValuesHidden(true);
