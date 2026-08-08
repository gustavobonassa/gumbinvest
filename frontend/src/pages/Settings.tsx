/** Preferences, market data control, AI providers, corporate actions, backup
    and data-quality warnings — one tab each. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  AlertTriangle,
  Bot,
  Database,
  Download,
  GitMerge,
  RefreshCw,
  SlidersHorizontal,
  TrendingUp,
} from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import AiSettings from "@/components/AiSettings";
import CloudBackupCard from "@/components/CloudBackupCard";
import CorporateActions from "@/components/CorporateActions";
import UpdateCard from "@/components/UpdateCard";
import SecretField from "@/components/SecretField";
import { useToast } from "@/components/Toast";
import { Badge, Card, ErrorState, SectionTitle, Select, Skeleton, Tabs } from "@/components/ui";
import { api } from "@/lib/api";
import { configureFormatting, dateTime, money, percent } from "@/lib/format";

/**
 * One tab per thing you came here to do.
 *
 * "Dados" used to hold four unrelated cards — corporate actions, the asset
 * universe, database export and the data-quality report — and finding any one
 * of them meant scrolling past the other three. They are split here, and the
 * universe moved out entirely: its settings now sit on the Universo page's
 * Sincronização tab, next to the button they enable. "Sistema" folded back
 * into "Geral" for the same reason: two API-key/environment cards did not
 * justify their own tab.
 *
 * Ordered from what you tune, through the data itself, to the machine.
 */
const TABS = [
  { value: "geral", label: "Geral", icon: SlidersHorizontal },
  { value: "cotacoes", label: "Cotações", icon: TrendingUp },
  { value: "ia", label: "Inteligência Artificial", icon: Bot },
  { value: "eventos", label: "Eventos corporativos", icon: GitMerge },
  { value: "qualidade", label: "Qualidade dos dados", icon: AlertTriangle },
  { value: "backup", label: "Backup", icon: Database },
] as const;

type TabValue = (typeof TABS)[number]["value"];

/** Links and bookmarks pointing at an old, since-merged-or-removed tab still
    have to land somewhere sensible. */
const LEGACY_TABS: Record<string, TabValue> = { dados: "eventos", sistema: "geral", watchlist: "geral" };

const CURRENCIES = ["BRL", "USD", "EUR"];
const TIMEZONES = ["America/Sao_Paulo", "America/New_York", "Europe/Lisbon", "UTC"];
const LOCALES = ["pt-BR", "en-US"];

/**
 * Which producers reach the header bell.
 *
 * The list is the backend's, not this file's: `notification_catalog` describes
 * every kind that exists, so a new producer appears here without a frontend
 * change.
 *
 * Ticks say what you receive; what is *stored* is the complement — the muted
 * set (`values.notification_muted_kinds`). Saving the enabled set instead would
 * freeze the catalogue as it stood the day it was saved, and a kind added in a
 * later release would arrive already switched off for everyone who had ever
 * opened this screen.
 *
 * Unticking mutes, it does not delete — entries keep being recorded and simply
 * leave the feed, so ticking a kind back on returns its history rather than a
 * gap. Said on the card, because a switch labelled "receber" reads like it
 * throws things away.
 */
function NotificationSettings({
  catalog,
  muted,
  onChange,
}: {
  catalog: { kind: string; label: string; description: string }[];
  muted: unknown;
  onChange: (next: string[]) => void;
}) {
  // Anything that is not a list means the setting was never written, which is
  // the same as muting nothing.
  const silenced = new Set(Array.isArray(muted) ? muted.map(String) : []);
  const selected = new Set(
    catalog.map((item) => item.kind).filter((kind) => !silenced.has(kind)),
  );

  return (
    <Card className="p-5">
      <SectionTitle
        title="Notificações"
        subtitle="O que aparece no sino, no topo da página"
      />
      <div className="space-y-3">
        {catalog.map((item) => (
          <label key={item.kind} className="flex cursor-pointer items-start gap-3">
            <input
              type="checkbox"
              className="mt-1"
              checked={selected.has(item.kind)}
              onChange={(event) => {
                const next = new Set(silenced);
                if (event.target.checked) next.delete(item.kind);
                else next.add(item.kind);
                // Ordered by the catalogue rather than by click order, so two
                // installations with the same choices store the same value.
                onChange(catalog.map((k) => k.kind).filter((kind) => next.has(kind)));
              }}
            />
            <span className="text-sm">
              <span className="font-medium">{item.label}</span>
              <span className="mt-1 block text-xs text-ink-muted">{item.description}</span>
            </span>
          </label>
        ))}
      </div>
      <p className="mt-4 text-xs text-ink-muted">
        Desmarcar apenas esconde: as notificações continuam sendo registradas, então marcar de
        volta traz também o que aconteceu enquanto estavam desligadas.
      </p>
    </Card>
  );
}

export default function Settings() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const status = useQuery({ queryKey: ["market-status"], queryFn: api.marketStatus });
  const warnings = useQuery({ queryKey: ["portfolio-warnings"], queryFn: api.portfolioWarnings });

  const [values, setValues] = useState<Record<string, unknown>>({});

  // The tab lives in the URL, so "resolver eventos corporativos" elsewhere in
  // the app can link straight at the section that resolves them.
  const [params, setParams] = useSearchParams();
  const requested = params.get("aba");
  const tab: TabValue = TABS.some((option) => option.value === requested)
    ? (requested as TabValue)
    : (LEGACY_TABS[requested ?? ""] ?? "geral");
  const setTab = (next: TabValue) =>
    setParams(next === "geral" ? {} : { aba: next }, { replace: true });

  useEffect(() => {
    if (settings.data) setValues(settings.data.values);
  }, [settings.data]);

  const save = useMutation({
    mutationFn: (next: Record<string, unknown>) => api.updateSettings(next),
    onSuccess: (data) => {
      configureFormatting(String(data.values.number_format ?? "pt-BR"), String(data.values.currency ?? "BRL"));
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      toast.success("Preferências salvas.");
    },
    onError: (error) => toast.error("Não foi possível salvar as preferências.", error),
  });
  const refresh = useMutation({
    mutationFn: api.refreshQuotes,
    onSuccess: () => {
      queryClient.invalidateQueries();
      toast.success("Cotações atualizadas.");
    },
    onError: (error) => toast.error("Não foi possível atualizar as cotações.", error),
  });
  const backfill = useMutation({
    mutationFn: api.backfillHistory,
    onSuccess: () => {
      queryClient.invalidateQueries();
      toast.success("Histórico de preços baixado.");
    },
    onError: (error) => toast.error("Não foi possível baixar o histórico.", error),
  });
  const syncFx = useMutation({
    mutationFn: api.syncFx,
    onSuccess: () => {
      queryClient.invalidateQueries();
      toast.success("Câmbio atualizado.");
    },
    onError: (error) => toast.error("Não foi possível atualizar o câmbio.", error),
  });
  if (settings.isError) return <ErrorState error={settings.error} retry={() => settings.refetch()} />;
  if (settings.isLoading || !settings.data) return <Skeleton className="h-80 w-full" />;

  // Preferences save on change, not through a separate button: a dropdown is
  // already a commit, and there is nothing else on this card to batch it with.
  const updatePreference = (key: string, value: unknown) => {
    const next = { ...values, [key]: value };
    setValues(next);
    save.mutate(next);
  };

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">Sistema</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Configurações</h1>
      </header>

      <Tabs
        value={tab}
        onChange={setTab}
        options={TABS.map((option) =>
          option.value === "qualidade" ? { ...option, count: warnings.data?.length } : { ...option },
        )}
      />

      {tab === "geral" ? (
        <>
        <Card className="p-5">
          <SectionTitle title="Preferências" subtitle="Moeda, fuso horário e formatação numérica" />
          {/* No theme picker: the app ships one (dark) theme. The old "Claro"
              option only flipped native widgets light on a dark UI — offering
              it was worse than not having it. */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <div>
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">Moeda</span>
              <Select
                ariaLabel="Moeda"
                value={String(values.currency ?? "BRL")}
                onChange={(next) => updatePreference("currency", next)}
                options={CURRENCIES.map((item) => ({ value: item, label: item }))}
              />
            </div>
            <div>
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">Fuso horário</span>
              <Select
                ariaLabel="Fuso horário"
                value={String(values.timezone ?? "America/Sao_Paulo")}
                onChange={(next) => updatePreference("timezone", next)}
                options={TIMEZONES.map((item) => ({ value: item, label: item }))}
              />
            </div>
            <div>
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">Formato numérico</span>
              <Select
                ariaLabel="Formato numérico"
                value={String(values.number_format ?? "pt-BR")}
                onChange={(next) => updatePreference("number_format", next)}
                options={LOCALES.map((item) => ({ value: item, label: item }))}
              />
            </div>
          </div>
        </Card>

        <NotificationSettings
          catalog={settings.data.notification_catalog}
          muted={values.notification_muted_kinds}
          onChange={(next) => updatePreference("notification_muted_kinds", next)}
        />

        <Card className="p-5">
          <SectionTitle
            title="Chaves de API"
            subtitle="Suas próprias chaves, guardadas apenas neste computador"
          />
          <div className="space-y-4">
            <SecretField
              label="brapi (cotações)"
              hint="Opcional, só se usar o provedor brapi. Crie em brapi.dev."
              placeholder="token brapi"
              configured={Boolean(settings.data.secrets?.brapi_token)}
              settingKey="brapi_token"
            />
          </div>
          <p className="mt-4 text-xs text-ink-muted">
            As chaves ficam no banco de dados local, nunca aparecem de volta nesta tela e não
            saem no export .gumbinvest. A chave é salva sozinha ao terminar de digitar; o ícone
            de lixeira remove.
          </p>
        </Card>

        <UpdateCard />

        <Card className="p-5">
          <SectionTitle title="Ambiente" subtitle="Valores lidos das variáveis de ambiente do backend" />
          <dl className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
            {Object.entries(settings.data.env).map(([key, value]) => (
              <div key={key} className="rounded-xl border border-line bg-surface-raised/50 p-3">
                <dt className="flex items-center gap-1.5 text-xs text-ink-muted">
                  <Database size={12} /> {key}
                </dt>
                <dd className="tnum mt-1 truncate font-medium">{String(value)}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-3 text-xs text-ink-muted">
            {settings.data.known_movements.length} tipos de movimentação mapeados no importador.
          </p>
        </Card>
        </>
      ) : null}

      {tab === "cotacoes" ? (
        <Card className="p-5">
          <SectionTitle
            title="Cotações"
            subtitle="Provedor configurado por variável de ambiente (MARKET_DATA_PROVIDER)"
          />
          <div className="flex flex-wrap items-center gap-3">
            <Badge tone="accent">provedor ativo: {settings.data.provider_active}</Badge>
            <Badge>disponíveis: {settings.data.providers.join(", ")}</Badge>
            <Badge tone={settings.data.env.brapi_token_configured ? "positive" : "warning"}>
              token brapi {settings.data.env.brapi_token_configured ? "configurado" : "ausente"}
            </Badge>
            <Badge>atualização automática: {String(settings.data.env.price_refresh_minutes)} min</Badge>
            <span className="text-sm text-ink-muted">última atualização {dateTime(status.data?.last_update ?? null)}</span>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <button type="button" className="btn-ghost" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
              <RefreshCw size={15} className={refresh.isPending ? "animate-spin" : undefined} /> Atualizar cotações
            </button>
            <button type="button" className="btn-ghost" onClick={() => backfill.mutate()} disabled={backfill.isPending}>
              <Download size={15} className={backfill.isPending ? "animate-pulse" : undefined} /> Baixar histórico de preços
            </button>
            <button type="button" className="btn-ghost" onClick={() => syncFx.mutate()} disabled={syncFx.isPending}>
              <RefreshCw size={15} className={syncFx.isPending ? "animate-spin" : undefined} /> Atualizar câmbio (PTAX)
            </button>
          </div>
          {status.data?.fx?.length ? (
            <p className="mt-3 text-xs text-ink-muted">
              Câmbio:{" "}
              {/* Coin series are listed too — they are what lets a trade priced
                  in Bitcoin reach reais — but named for what they are, so the
                  page does not appear to claim Bitcoin is a currency. */}
              {status.data.fx
                .map(
                  (series) =>
                    `${series.pair} de ${series.start} a ${series.end} (${series.points} cotações` +
                    `${series.is_currency ? "" : ", fechamento da moeda digital"})`,
                )
                .join(" · ")}
            </p>
          ) : null}
          {backfill.isPending ? (
            <p className="mt-2 text-xs text-ink-muted">
              O download do histórico percorre todos os ativos e pode levar alguns minutos.
            </p>
          ) : null}

          {status.isError ? (
            <div className="mt-4">
              <ErrorState error={status.error} retry={() => status.refetch()} />
            </div>
          ) : null}
          {status.data?.quotes.length ? (
            <div className="mt-4 max-h-64 overflow-auto">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-surface text-left text-xs uppercase tracking-wide text-ink-muted">
                  <tr>
                    <th className="px-2 py-2">Ativo</th>
                    <th className="px-2 py-2 text-right">Preço</th>
                    <th className="px-2 py-2 text-right">Variação</th>
                    <th className="px-2 py-2">Fonte</th>
                    <th className="px-2 py-2">Atualizado</th>
                  </tr>
                </thead>
                <tbody>
                  {status.data.quotes.map((quote) => (
                    <tr key={quote.ticker} className="table-row">
                      <td className="px-2 py-1.5 font-medium">{quote.ticker}</td>
                      <td className="tnum px-2 py-1.5 text-right">{money(quote.price)}</td>
                      <td
                        className={clsx(
                          "tnum px-2 py-1.5 text-right",
                          (quote.change_percent ?? 0) >= 0 ? "text-positive" : "text-negative",
                        )}
                      >
                        {percent(quote.change_percent ?? 0, 2, true)}
                      </td>
                      <td className="px-2 py-1.5 text-xs text-ink-muted">{quote.source}</td>
                      <td className="px-2 py-1.5 text-xs text-ink-muted">{dateTime(quote.fetched_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </Card>
      ) : null}

      {tab === "ia" ? <AiSettings settings={settings.data} /> : null}

      {tab === "eventos" ? <CorporateActions /> : null}

      {tab === "backup" ? (
        <>
        <Card className="p-5">
          <SectionTitle
            title="Backup e migração"
            subtitle="Todo o banco de dados em um arquivo .gumbinvest"
          />
          <div className="flex flex-wrap gap-2">
            <a className="btn-ghost" href={api.fullExportUrl()}>
              <Download size={15} /> Exportar banco de dados
            </a>
          </div>
          <p className="mt-3 text-xs text-ink-muted">
            O arquivo carrega tudo (carteiras, movimentações, cotações, histórico) e serve para
            levar seus dados a outra instalação (por exemplo, do Docker para o aplicativo desktop):
            basta arrastá-lo na página Importar de uma instalação vazia. O import é recusado se o
            destino já tiver movimentações, para nunca misturar duas histórias.
          </p>
        </Card>
        <CloudBackupCard settings={settings.data} />
        </>
      ) : null}

      {tab === "qualidade" ? (
        <Card className="p-5">
          <SectionTitle
            title="Qualidade dos dados"
            subtitle="Inconsistências detectadas ao reprocessar o histórico do extrato"
          />
          {warnings.isError ? (
            <ErrorState error={warnings.error} retry={() => warnings.refetch()} />
          ) : warnings.isLoading ? (
            <Skeleton className="h-9 w-full" />
          ) : !warnings.data?.length ? (
            <p className="text-sm text-ink-muted">Nenhuma inconsistência encontrada.</p>
          ) : (
            <ul className="space-y-2">
              {warnings.data.map((warning, index) => (
                <li
                  key={`${warning.ticker}-${index}`}
                  className="flex items-start gap-2.5 rounded-xl border border-warning/25 bg-warning/5 p-3 text-sm"
                >
                  <AlertTriangle size={15} className="mt-0.5 shrink-0 text-warning" />
                  <span>
                    <span className="font-medium">{warning.ticker}</span>
                    <span className="ml-2 text-ink-secondary">{warning.message}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      ) : null}
    </div>
  );
}

