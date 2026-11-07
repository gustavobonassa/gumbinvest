/** The desktop updater, as seen from the SPA.
 *
 * The updater lives in the Electron shell (desktop-shell/main.js) and its state
 * crosses over through the preload bridge (desktop-shell/preload.js). Outside
 * the desktop app — a browser, the phone — there is no `window.gumbinvest`, and
 * everything here reports "no bridge" so the update UI renders nothing.
 */
import { useEffect, useState } from "react";

export type UpdateStatus =
  "idle" | "checking" | "available" | "downloading" | "downloaded" | "current" | "error";

export type UpdateState = {
  status: UpdateStatus;
  version: string | null;
  percent: number;
  error: string | null;
  /** The version whose startup prompt the user waved off; older shells omit it. */
  dismissed?: string | null;
};

/** Everything the Electron preload exposes; see desktop-shell/preload.js. */
export type DesktopBridge = {
  version: () => Promise<string>;
  /** Repaints the native window chrome — `lib/theme.ts` is its only caller. */
  setTheme?: (theme: "dark" | "light") => Promise<void>;
  checkForUpdates: () => Promise<UpdateState>;
  downloadUpdate: () => Promise<UpdateState>;
  installUpdate: () => Promise<UpdateState>;
  /** Absent on shells older than the startup prompt. */
  dismissUpdate?: (version: string) => Promise<UpdateState>;
  updateState: () => Promise<UpdateState>;
  onUpdateState: (handler: (state: UpdateState) => void) => () => void;
};

declare global {
  interface Window {
    gumbinvest?: DesktopBridge;
  }
}

const IDLE: UpdateState = { status: "idle", version: null, percent: 0, error: null };

/** The shell's update state: asked for once, then followed through its pushes —
 *  the startup check may finish before or after the component mounts. */
export function useUpdateState() {
  const bridge = window.gumbinvest;
  const [state, setState] = useState<UpdateState>(IDLE);

  useEffect(() => {
    if (!bridge) return;
    let live = true;
    let pushed = false;
    const unsubscribe = bridge.onUpdateState((next) => {
      pushed = true;
      setState(next);
    });
    // A push can overtake the initial answer; the push is newer, keep it.
    bridge.updateState().then((current) => live && !pushed && setState(current));
    return () => {
      live = false;
      unsubscribe();
    };
  }, [bridge]);

  return { bridge, state };
}
