/** Version and updates, for the desktop app only.
 *
 * The whole card depends on `window.gumbinvest`, which only the Electron
 * preload defines — in a browser or on the phone there is no app to update, so
 * this renders nothing rather than showing a button that cannot work.
 *
 * The updater lives in the shell (desktop-shell/main.js); this is a view of
 * its state machine: ask once on mount, then follow the pushes.
 */
import { Download, RefreshCw, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge, Card, SectionTitle } from "@/components/ui";

type UpdateStatus =
  | "idle"
  | "checking"
  | "available"
  | "downloading"
  | "downloaded"
  | "current"
  | "error";

type UpdateState = {
  status: UpdateStatus;
  version: string | null;
  percent: number;
  error: string | null;
};

/** Everything the Electron preload exposes; see desktop-shell/preload.js. */
type UpdateBridge = {
  version: () => Promise<string>;
  /** Repaints the native window chrome — `lib/theme.ts` is its only caller. */
  setTheme?: (theme: "dark" | "light") => Promise<void>;
  checkForUpdates: () => Promise<UpdateState>;
  downloadUpdate: () => Promise<UpdateState>;
  installUpdate: () => Promise<UpdateState>;
  updateState: () => Promise<UpdateState>;
  onUpdateState: (handler: (state: UpdateState) => void) => () => void;
};

declare global {
  interface Window {
    gumbinvest?: UpdateBridge;
  }
}

export default function UpdateCard() {
  const bridge = window.gumbinvest;
  const [version, setVersion] = useState<string>("");
  const [state, setState] = useState<UpdateState>({
    status: "idle",
    version: null,
    percent: 0,
    error: null,
  });

  useEffect(() => {
    if (!bridge) return;
    bridge.version().then(setVersion);
    bridge.updateState().then(setState);
    // The shell may already be mid-check from the startup poll — subscribing
    // is what keeps this screen in step with it.
    return bridge.onUpdateState(setState);
  }, [bridge]);

  if (!bridge) return null;

  const busy = state.status === "checking" || state.status === "downloading";

  return (
    <Card className="p-5">
      <SectionTitle title="Versão e atualizações" subtitle="Baixe e instale novas versões sem sair do app" />

      <div className="flex flex-wrap items-center gap-3">
        <Badge>versão instalada: {version || "—"}</Badge>
        {state.status === "current" ? <Badge tone="positive">está atualizado</Badge> : null}
        {state.status === "available" || state.status === "downloaded" ? (
          <Badge tone="accent">nova versão: {state.version}</Badge>
        ) : null}
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        <button
          type="button"
          className="btn-ghost"
          onClick={() => bridge.checkForUpdates()}
          disabled={busy}
        >
          <RefreshCw size={15} className={state.status === "checking" ? "animate-spin" : undefined} />
          {state.status === "checking" ? "Procurando…" : "Verificar atualizações"}
        </button>

        {state.status === "available" ? (
          <button type="button" className="btn-primary" onClick={() => bridge.downloadUpdate()}>
            <Download size={15} /> Baixar versão {state.version}
          </button>
        ) : null}

        {state.status === "downloaded" ? (
          <button type="button" className="btn-primary" onClick={() => bridge.installUpdate()}>
            <RotateCcw size={15} /> Reiniciar e instalar
          </button>
        ) : null}
      </div>

      {state.status === "downloading" ? (
        <div className="mt-4">
          <div className="h-1.5 overflow-hidden rounded-full bg-surface-raised">
            <div
              className="h-full rounded-full bg-accent transition-[width] duration-300"
              style={{ width: `${state.percent}%` }}
            />
          </div>
          <p className="mt-2 text-xs text-ink-muted">Baixando… {state.percent}%</p>
        </div>
      ) : null}

      {state.status === "error" && state.error ? (
        <p className="mt-3 text-sm text-negative">{state.error}</p>
      ) : null}

      <p className="mt-4 text-xs text-ink-muted">
        A atualização troca só o programa: seus dados, chaves e backups continuam onde estão. Como
        o instalador não é assinado digitalmente, o Windows ainda mostra o aviso de origem
        desconhecida ao aplicar a nova versão.
      </p>
    </Card>
  );
}
