/** Backup na nuvem: conectar Google Drive / Dropbox, enviar agora e restaurar.
 *
 * Both providers authorize by pasting a short code — Google's device flow and
 * Dropbox's no-redirect PKCE — so the same UI works under Docker and on the
 * desktop build's variable port. The manual send runs as a backend job polled
 * here; the nightly sync reports through the notification bell instead.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CloudUpload, Download, ExternalLink, RefreshCw, Unplug } from "lucide-react";
import { useEffect, useState } from "react";

import SecretField from "@/components/SecretField";
import { useToast } from "@/components/Toast";
import { Badge, Card, Modal, SectionTitle, Skeleton } from "@/components/ui";
import {
  api,
  type AppSettings,
  type CloudProviderStatus,
  type RemoteBackupItem,
} from "@/lib/api";
import { dateTime } from "@/lib/format";

const sizeLabel = (bytes: number | null | undefined) =>
  bytes == null ? null : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;

export default function CloudBackupCard({ settings }: { settings: AppSettings }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const status = useQuery({
    queryKey: ["cloud-backup"],
    queryFn: api.cloudBackupStatus,
    refetchInterval: (q) => (q.state.data?.job?.active ? 2_000 : false),
  });

  // Only the send this tab started deserves a toast — a job that finished
  // before the page opened already said what it had to say.
  const [watchingId, setWatchingId] = useState<string | null>(null);
  const send = useMutation({
    mutationFn: api.cloudBackupSend,
    onSuccess: (job) => {
      setWatchingId(job.id);
      queryClient.invalidateQueries({ queryKey: ["cloud-backup"] });
    },
    onError: (error) => toast.error("Não foi possível iniciar o envio.", error),
  });
  useEffect(() => {
    const job = status.data?.job;
    if (!watchingId || !job || job.id !== watchingId || job.active) return;
    if (job.error) toast.error(job.error);
    else toast.success("Backup enviado para a nuvem.");
    setWatchingId(null);
    queryClient.invalidateQueries({ queryKey: ["cloud-backup"] });
    queryClient.invalidateQueries({ queryKey: ["cloud-backups"] });
  }, [status.data, watchingId, queryClient, toast]);

  const providers = status.data?.providers ?? [];
  const anyConnected = providers.some((provider) => provider.connected);
  const jobActive = Boolean(status.data?.job?.active);

  return (
    <Card className="p-5">
      <SectionTitle
        title="Backup na nuvem"
        subtitle="Envie o arquivo .gumbinvest para o seu próprio Google Drive ou Dropbox"
      />

      {status.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : (
        <div className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-2">
            <GoogleDriveSection
              provider={providers.find((provider) => provider.name === "gdrive")}
              settings={settings}
            />
            <DropboxSection
              provider={providers.find((provider) => provider.name === "dropbox")}
              settings={settings}
            />
          </div>

          <SecretField
            label="Senha de criptografia (opcional)"
            hint={
              "Se definida, o arquivo é criptografado antes de subir e a mesma senha é pedida ao " +
              "restaurar. Guarde-a bem: sem a senha, o backup não pode ser restaurado — nem por você."
            }
            placeholder="senha de criptografia"
            configured={Boolean(settings.secrets?.cloud_backup_passphrase)}
            settingKey="cloud_backup_passphrase"
          />

          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              className="btn-primary"
              disabled={!anyConnected || jobActive || send.isPending}
              onClick={() => send.mutate()}
            >
              <CloudUpload size={15} className={jobActive ? "animate-pulse" : undefined} />
              {jobActive ? "Enviando…" : "Enviar agora"}
            </button>
            {status.data?.backup_time ? (
              <span className="text-xs text-ink-muted">
                Envio automático todo dia às {status.data.backup_time}, junto com o backup local.
              </span>
            ) : null}
          </div>

          {anyConnected ? <RestoreSection /> : null}

          <p className="text-xs text-ink-muted">
            As conexões e a senha ficam só nesta instalação e não saem no export .gumbinvest — na
            outra instalação, conecte de novo para restaurar.
          </p>
        </div>
      )}
    </Card>
  );
}

function LastResult({ provider }: { provider: CloudProviderStatus }) {
  const last = provider.last;
  if (!last) return null;
  if (last.state === "error") {
    return (
      <p className="mt-2 text-xs text-warning">
        Último envio falhou ({dateTime(last.at)}): {last.message}
      </p>
    );
  }
  return (
    <p className="mt-2 truncate text-xs text-ink-muted">
      Último envio: {dateTime(last.at)}
      {last.file ? ` · ${last.file}` : ""}
      {sizeLabel(last.size) ? ` · ${sizeLabel(last.size)}` : ""}
    </p>
  );
}

function DisconnectButton({ provider }: { provider: CloudProviderStatus }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const disconnect = useMutation({
    mutationFn: () => api.cloudDisconnect(provider.name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["cloud-backup"] });
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      toast.success(`${provider.label} desconectado.`);
    },
    onError: (error) => toast.error(`Não foi possível desconectar o ${provider.label}.`, error),
  });
  return (
    <button
      type="button"
      className="btn-ghost"
      disabled={disconnect.isPending}
      onClick={() => disconnect.mutate()}
    >
      <Unplug size={15} /> Desconectar
    </button>
  );
}

function GoogleDriveSection({
  provider,
  settings,
}: {
  provider: CloudProviderStatus | undefined;
  settings: AppSettings;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [flow, setFlow] = useState<{
    verification_url: string;
    user_code: string;
    interval: number;
  } | null>(null);

  const storedClientId = String(settings.values.gdrive_client_id ?? "");
  const [clientId, setClientId] = useState(storedClientId);
  useEffect(() => setClientId(storedClientId), [storedClientId]);
  const [clientSecret, setClientSecret] = useState("");
  const secretConfigured = Boolean(settings.secrets?.gdrive_client_secret);

  // Conectar is the save: it persists whatever changed in the two fields
  // above, then starts the device flow — no per-input save buttons.
  const start = useMutation({
    mutationFn: async () => {
      const changes: Record<string, unknown> = {};
      if (clientId.trim() !== storedClientId) changes.gdrive_client_id = clientId.trim();
      if (clientSecret.trim()) changes.gdrive_client_secret = clientSecret.trim();
      if (Object.keys(changes).length) await api.updateSettings(changes);
      return api.gdriveDeviceStart();
    },
    onSuccess: (data) => {
      setClientSecret("");
      setFlow(data);
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      queryClient.invalidateQueries({ queryKey: ["cloud-backup"] });
    },
    onError: (error) => toast.error("Não foi possível iniciar a conexão com o Google.", error),
  });

  useEffect(() => {
    if (!flow) return undefined;
    let cancelled = false;
    const timer = setInterval(async () => {
      try {
        const result = await api.gdriveDevicePoll();
        if (cancelled) return;
        if (result.status === "connected") {
          setFlow(null);
          toast.success("Google Drive conectado.");
          queryClient.invalidateQueries({ queryKey: ["cloud-backup"] });
          queryClient.invalidateQueries({ queryKey: ["settings"] });
        } else if (result.interval && result.interval !== flow.interval) {
          // slow_down do Google: alarga o próprio polling
          setFlow((current) => (current ? { ...current, interval: result.interval! } : current));
        }
      } catch (error) {
        if (cancelled) return;
        setFlow(null);
        toast.error("A conexão com o Google Drive falhou.", error);
      }
    }, Math.max(flow.interval, 3) * 1000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [flow, queryClient, toast]);

  if (!provider) return null;
  return (
    <div className="min-w-0 rounded-xl border border-line bg-surface-raised/50 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">Google Drive</span>
        <Badge tone={provider.connected ? "positive" : "warning"}>
          {provider.connected ? "conectado" : "desconectado"}
        </Badge>
      </div>

      {provider.connected ? (
        <>
          <LastResult provider={provider} />
          <div className="mt-3">
            <DisconnectButton provider={provider} />
          </div>
        </>
      ) : flow ? (
        <div className="mt-3 space-y-2 text-sm">
          <p>
            Acesse{" "}
            <a
              href={flow.verification_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 font-medium text-accent hover:underline"
            >
              google.com/device <ExternalLink size={13} />
            </a>{" "}
            e digite o código:
          </p>
          <p className="tnum select-all text-center text-2xl font-semibold tracking-[0.2em]">
            {flow.user_code}
          </p>
          <p className="flex items-center gap-1.5 text-xs text-ink-muted">
            <RefreshCw size={12} className="animate-spin" /> Aguardando autorização…
          </p>
        </div>
      ) : (
        <div className="mt-3 space-y-3">
          <div>
            <span className="mb-1.5 block text-xs font-medium text-ink-muted">Client ID</span>
            <input
              value={clientId}
              onChange={(event) => setClientId(event.target.value)}
              placeholder="xxxxx.apps.googleusercontent.com"
              autoComplete="off"
              className="input w-full"
            />
            <p className="mt-1 text-xs text-ink-muted">
              Do seu projeto no Google Cloud (tipo “TVs e dispositivos de entrada limitada”).
            </p>
          </div>
          <div>
            <span className="mb-1.5 block text-xs font-medium text-ink-muted">Client secret</span>
            <input
              type="password"
              autoComplete="off"
              value={clientSecret}
              onChange={(event) => setClientSecret(event.target.value)}
              placeholder={secretConfigured ? "•••••• (digite para substituir)" : "client secret"}
              className="input w-full"
            />
            <p className="mt-1 text-xs text-ink-muted">
              Fica só neste computador e nunca aparece de volta.
            </p>
          </div>
          <button
            type="button"
            className="btn-primary"
            disabled={
              !clientId.trim() || (!clientSecret.trim() && !secretConfigured) || start.isPending
            }
            onClick={() => start.mutate()}
          >
            {start.isPending ? "Conectando…" : "Conectar Google Drive"}
          </button>
          <p className="text-xs text-ink-muted">
            Crie um projeto gratuito no Google Cloud, ative a API do Drive e um cliente OAuth do
            tipo “TVs e dispositivos de entrada limitada”. Publique a tela de consentimento (“Em
            produção”) — no modo “Teste”, o Google desconecta a cada 7 dias.
          </p>
        </div>
      )}
    </div>
  );
}

function DropboxSection({
  provider,
  settings,
}: {
  provider: CloudProviderStatus | undefined;
  settings: AppSettings;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [pendingCode, setPendingCode] = useState(false);
  const [code, setCode] = useState("");

  const storedAppKey = String(settings.values.dropbox_app_key ?? "");
  const [appKey, setAppKey] = useState(storedAppKey);
  useEffect(() => setAppKey(storedAppKey), [storedAppKey]);

  // Conectar is the save: it persists the app key if it changed, then opens
  // the authorization page — no per-input save button.
  const authorize = useMutation({
    mutationFn: async () => {
      if (appKey.trim() !== storedAppKey) {
        await api.updateSettings({ dropbox_app_key: appKey.trim() });
      }
      return api.dropboxAuthorize();
    },
    onSuccess: ({ authorize_url }) => {
      window.open(authorize_url, "_blank", "noopener");
      setPendingCode(true);
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      queryClient.invalidateQueries({ queryKey: ["cloud-backup"] });
    },
    onError: (error) => toast.error("Não foi possível iniciar a conexão com o Dropbox.", error),
  });
  const complete = useMutation({
    mutationFn: (next: string) => api.dropboxComplete(next),
    onSuccess: () => {
      setPendingCode(false);
      setCode("");
      toast.success("Dropbox conectado.");
      queryClient.invalidateQueries({ queryKey: ["cloud-backup"] });
      queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (error) => toast.error("O Dropbox recusou o código.", error),
  });

  if (!provider) return null;
  return (
    <div className="min-w-0 rounded-xl border border-line bg-surface-raised/50 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">Dropbox</span>
        <Badge tone={provider.connected ? "positive" : "warning"}>
          {provider.connected ? "conectado" : "desconectado"}
        </Badge>
      </div>

      {provider.connected ? (
        <>
          <LastResult provider={provider} />
          <div className="mt-3">
            <DisconnectButton provider={provider} />
          </div>
        </>
      ) : pendingCode ? (
        <div className="mt-3 space-y-2">
          <p className="text-sm">Cole aqui o código exibido pelo Dropbox:</p>
          <div className="flex flex-wrap gap-2">
            <input
              value={code}
              onChange={(event) => setCode(event.target.value)}
              placeholder="código do Dropbox"
              autoComplete="off"
              className="input min-w-0 flex-1"
            />
            <button
              type="button"
              className="btn-primary"
              disabled={!code.trim() || complete.isPending}
              onClick={() => complete.mutate(code.trim())}
            >
              {complete.isPending ? "Conectando…" : "Confirmar"}
            </button>
          </div>
        </div>
      ) : (
        <div className="mt-3 space-y-3">
          <div>
            <span className="mb-1.5 block text-xs font-medium text-ink-muted">App key</span>
            <input
              value={appKey}
              onChange={(event) => setAppKey(event.target.value)}
              placeholder="app key"
              autoComplete="off"
              className="input w-full"
            />
            <p className="mt-1 text-xs text-ink-muted">
              Do seu app no Dropbox App Console (acesso “App folder”). Não há secret.
            </p>
          </div>
          <button
            type="button"
            className="btn-primary"
            disabled={!appKey.trim() || authorize.isPending}
            onClick={() => authorize.mutate()}
          >
            {authorize.isPending ? "Conectando…" : "Conectar Dropbox"}
          </button>
          <p className="text-xs text-ink-muted">
            Crie um app gratuito em dropbox.com/developers com acesso “App folder” — os backups
            ficam em Apps/&lt;seu app&gt;/ e o GumbInvest não enxerga o resto do Dropbox.
          </p>
        </div>
      )}
    </div>
  );
}

function RestoreSection() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState<{ provider: string; item: RemoteBackupItem } | null>(null);
  const [passphrase, setPassphrase] = useState("");

  const backups = useQuery({
    queryKey: ["cloud-backups"],
    queryFn: api.cloudBackups,
    enabled: open,
  });

  const restore = useMutation({
    mutationFn: (payload: { provider: string; backup_id: string; name: string; passphrase?: string }) =>
      api.cloudRestore(payload),
    onSuccess: (result) => {
      setTarget(null);
      setPassphrase("");
      queryClient.invalidateQueries();
      toast.success(`Backup restaurado: ${result.rows_imported} registros. Recarregue a página.`);
    },
    onError: (error) => toast.error("Não foi possível restaurar o backup.", error),
  });

  return (
    <div>
      <button type="button" className="btn-ghost" onClick={() => setOpen((current) => !current)}>
        <Download size={15} /> {open ? "Ocultar backups na nuvem" : "Ver backups na nuvem"}
      </button>

      {open ? (
        backups.isLoading ? (
          <Skeleton className="mt-3 h-20 w-full" />
        ) : (
          <div className="mt-3 space-y-3">
            {Object.entries(backups.data?.providers ?? {}).map(([name, entry]) => (
              <div key={name} className="min-w-0">
                <p className="mb-1 text-xs font-medium uppercase tracking-wide text-ink-muted">
                  {name === "gdrive" ? "Google Drive" : name === "dropbox" ? "Dropbox" : name}
                </p>
                {entry.error ? (
                  <p className="text-xs text-warning">{entry.error}</p>
                ) : !entry.items?.length ? (
                  <p className="text-xs text-ink-muted">Nenhum backup encontrado.</p>
                ) : (
                  <ul className="divide-y divide-line">
                    {entry.items.map((item) => (
                      <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 py-1.5">
                        <span className="min-w-0">
                          <span className="block truncate text-sm font-medium">{item.name}</span>
                          <span className="text-xs text-ink-muted">
                            {dateTime(item.modified_at)}
                            {sizeLabel(item.size) ? ` · ${sizeLabel(item.size)}` : ""}
                          </span>
                        </span>
                        <span className="flex items-center gap-2">
                          {item.encrypted ? <Badge>criptografado</Badge> : null}
                          <button
                            type="button"
                            className="btn-ghost"
                            onClick={() => setTarget({ provider: name, item })}
                          >
                            Restaurar
                          </button>
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        )
      ) : null}

      <Modal
        open={target !== null}
        title="Restaurar backup da nuvem"
        subtitle={target?.item.name}
        onClose={() => {
          setTarget(null);
          setPassphrase("");
        }}
      >
        <div className="space-y-3 text-sm">
          <p>
            Restaurar substitui todos os dados desta instalação pelo conteúdo do backup. Só é
            permitido numa instalação sem movimentações — nunca misturamos duas histórias.
          </p>
          {target?.item.encrypted ? (
            <div>
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">
                Senha de criptografia
              </span>
              <input
                type="password"
                autoComplete="off"
                value={passphrase}
                onChange={(event) => setPassphrase(event.target.value)}
                placeholder="deixe em branco para usar a senha salva"
                className="input w-full"
              />
            </div>
          ) : null}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-ghost"
              onClick={() => {
                setTarget(null);
                setPassphrase("");
              }}
            >
              Cancelar
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={restore.isPending}
              onClick={() =>
                target &&
                restore.mutate({
                  provider: target.provider,
                  backup_id: target.item.id,
                  name: target.item.name,
                  passphrase: passphrase.trim() || undefined,
                })
              }
            >
              {restore.isPending ? "Restaurando…" : "Restaurar"}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
