/** "Nova versão disponível" — the desktop app's startup update prompt.
 *
 * The shell checks for updates a few seconds after launch (desktop-shell/
 * main.js). If that check finds a version the user hasn't waved off, this
 * modal offers it once. "Agora não" — or closing the dialog any other way —
 * records the version in the shell's data dir, so the prompt stays quiet
 * until a newer one ships; Configurações → Versão e atualizações still offers
 * the dismissed version whenever the user wants it.
 *
 * Only the startup result is considered: a manual "Verificar atualizações"
 * already happens on the screen that shows the answer, so a modal on top of it
 * would only repeat it. Outside the desktop app there is no bridge and this
 * renders nothing.
 */
import { useQuery } from "@tanstack/react-query";
import { Download, ExternalLink, RotateCcw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Modal } from "@/components/ui";
import { api } from "@/lib/api";
import { useUpdateState } from "@/lib/updates";

const RELEASES_URL = "https://github.com/gustavobonassa/gumbinvest/releases/tag";

/** True while the first-run wizard is, or may yet be, on screen. Mirrors the
 *  gate in Onboarding.tsx on the same cached queries, so two dialogs never
 *  stack on a fresh install. */
function useOnboardingMayShow(enabled: boolean) {
  const settingsQ = useQuery({
    queryKey: ["settings"],
    queryFn: api.settings,
    staleTime: 5 * 60_000,
    enabled,
  });
  const completed = settingsQ.data ? settingsQ.data.values.onboarding_completed === true : null;
  const overviewQ = useQuery({
    queryKey: ["overview"],
    queryFn: api.overview,
    enabled: enabled && completed === false,
  });
  if (settingsQ.isError) return false;
  if (completed === null) return true;
  if (completed) return false;
  return !overviewQ.data || overviewQ.data.assets_tracked === 0;
}

export default function UpdatePrompt() {
  const { bridge, state } = useUpdateState();
  const onboardingMayShow = useOnboardingMayShow(Boolean(bridge));
  const [installed, setInstalled] = useState("");
  // What the startup check found — captured the moment it lands, so a manual
  // check later in the session can't be mistaken for it.
  const [found, setFound] = useState<string | null>(null);
  const [closed, setClosed] = useState(false);
  const decided = useRef(false);

  useEffect(() => {
    bridge?.version().then(setInstalled);
  }, [bridge]);

  useEffect(() => {
    if (!bridge || decided.current) return;
    if (state.status === "idle" || state.status === "checking") return;
    decided.current = true;
    if (state.status === "available" && state.version && state.version !== state.dismissed) {
      setFound(state.version);
    }
  }, [bridge, state]);

  // Showing it, though, waits until the first-run wizard is out of the way.
  const offered = onboardingMayShow ? null : found;

  if (!bridge || !offered) return null;

  const status = state.status;
  const close = () => {
    // Ignoring the offer is the only close that silences it. Mid-download or
    // after it, the user already said yes — closing just gets out of the way
    // (Configurações shows the progress and the restart button).
    if (status === "available") void bridge.dismissUpdate?.(offered);
    setClosed(true);
  };

  return (
    <Modal
      open={!closed}
      title="Nova versão disponível"
      subtitle={
        installed
          ? `GumbInvest ${offered} — você está na versão ${installed}`
          : `GumbInvest ${offered}`
      }
      onClose={close}
      centered
    >
      {status === "downloading" ? (
        <div>
          <div className="h-1.5 overflow-hidden rounded-full bg-surface-hover">
            <div
              className="h-full rounded-full bg-accent transition-[width] duration-300"
              style={{ width: `${state.percent}%` }}
            />
          </div>
          <p className="mt-2 text-sm text-ink-muted">Baixando… {state.percent}%</p>
        </div>
      ) : status === "downloaded" ? (
        <p className="text-sm text-ink-muted">
          Download concluído. O app fecha, instala a nova versão e abre de novo sozinho.
        </p>
      ) : status === "error" ? (
        <p className="text-sm text-negative">
          {state.error || "Não foi possível baixar a atualização."}
        </p>
      ) : (
        <div className="space-y-3 text-sm text-ink-muted">
          <p>
            A atualização troca só o programa: seus dados, chaves e backups continuam onde estão.
          </p>
          <a
            href={`${RELEASES_URL}/v${offered}`}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 text-accent hover:underline"
          >
            Ver o que mudou <ExternalLink size={13} />
          </a>
        </div>
      )}

      <div className="mt-5 flex flex-wrap justify-end gap-2">
        {status === "downloading" ? (
          <button type="button" className="btn-ghost" onClick={close}>
            Continuar em segundo plano
          </button>
        ) : status === "downloaded" ? (
          <>
            <button type="button" className="btn-ghost" onClick={close}>
              Depois
            </button>
            <button type="button" className="btn-primary" onClick={() => bridge.installUpdate()}>
              <RotateCcw size={15} /> Reiniciar e instalar
            </button>
          </>
        ) : status === "error" ? (
          <>
            <button type="button" className="btn-ghost" onClick={close}>
              Fechar
            </button>
            <button type="button" className="btn-primary" onClick={() => bridge.downloadUpdate()}>
              <Download size={15} /> Tentar de novo
            </button>
          </>
        ) : (
          <>
            <button type="button" className="btn-ghost" onClick={close}>
              Agora não
            </button>
            <button type="button" className="btn-primary" onClick={() => bridge.downloadUpdate()}>
              <Download size={15} /> Atualizar agora
            </button>
          </>
        )}
      </div>
    </Modal>
  );
}
