/** Preferences, market data control, watchlist and data-quality warnings. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  AlertTriangle,
  Database,
  Download,
  Eye,
  Plus,
  RefreshCw,
  Server,
  SlidersHorizontal,
  TrendingUp,
  Trash2,
} from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import CorporateActions from "@/components/CorporateActions";
import UniverseSettings from "@/components/UniverseSettings";
import { useToast } from "@/components/Toast";
import { Badge, Card, ErrorState, SectionTitle, Select, Skeleton, Tabs } from "@/components/ui";
import { api, type AppSettings } from "@/lib/api";
import { configureFormatting, dateTime, money, percent } from "@/lib/format";

const TABS = [
  { value: "geral", label: "Geral", icon: SlidersHorizontal },
  { value: "cotacoes", label: "Cotações", icon: TrendingUp },
  { value: "watchlist", label: "Watchlist", icon: Eye },
  { value: "dados", label: "Dados", icon: Database },
  { value: "sistema", label: "Sistema", icon: Server },
] as const;

type TabValue = (typeof TABS)[number]["value"];

const CURRENCIES = ["BRL", "USD", "EUR"];
const TIMEZONES = ["America/Sao_Paulo", "America/New_York", "Europe/Lisbon", "UTC"];
const LOCALES = ["pt-BR", "en-US"];

export default function Settings() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const status = useQuery({ queryKey: ["market-status"], queryFn: api.marketStatus });
  const warnings = useQuery({ queryKey: ["portfolio-warnings"], queryFn: api.portfolioWarnings });
  const watchlist = useQuery({ queryKey: ["watchlist"], queryFn: api.watchlist });

  const [values, setValues] = useState<Record<string, unknown>>({});
  const [newTicker, setNewTicker] = useState("");

  // The tab lives in the URL, so "resolver eventos corporativos" elsewhere in
  // the app can link straight at the section that resolves them.
  const [params, setParams] = useSearchParams();
  const requested = params.get("aba");
  const tab: TabValue = TABS.some((option) => option.value === requested)
    ? (requested as TabValue)
    : "geral";
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
  const addWatch = useMutation({
    mutationFn: (ticker: string) => api.addWatchlist(ticker),
    onSuccess: (_data, ticker) => {
      setNewTicker("");
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      toast.success(`${ticker} adicionado à watchlist.`);
    },
    onError: (error) => toast.error("Não foi possível adicionar à watchlist.", error),
  });
  const removeWatch = useMutation({
    mutationFn: (id: number) => api.removeWatchlist(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      toast.success("Removido da watchlist.");
    },
    onError: (error) => toast.error("Não foi possível remover da watchlist.", error),
  });

  if (settings.isError) return <ErrorState error={settings.error} retry={() => settings.refetch()} />;
  if (settings.isLoading || !settings.data) return <Skeleton className="h-80 w-full" />;

  const update = (key: string, value: unknown) => setValues((current) => ({ ...current, [key]: value }));

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
          option.value === "watchlist"
            ? { ...option, count: watchlist.data?.length }
            : option.value === "dados"
              ? { ...option, count: warnings.data?.length }
              : { ...option },
        )}
      />

      {tab === "geral" ? (
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
                onChange={(next) => update("currency", next)}
                options={CURRENCIES.map((item) => ({ value: item, label: item }))}
              />
            </div>
            <div>
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">Fuso horário</span>
              <Select
                ariaLabel="Fuso horário"
                value={String(values.timezone ?? "America/Sao_Paulo")}
                onChange={(next) => update("timezone", next)}
                options={TIMEZONES.map((item) => ({ value: item, label: item }))}
              />
            </div>
            <div>
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">Formato numérico</span>
              <Select
                ariaLabel="Formato numérico"
                value={String(values.number_format ?? "pt-BR")}
                onChange={(next) => update("number_format", next)}
                options={LOCALES.map((item) => ({ value: item, label: item }))}
              />
            </div>
          </div>
          <button type="button" className="btn-primary mt-4" onClick={() => save.mutate(values)} disabled={save.isPending}>
            {save.isPending ? "Salvando…" : "Salvar preferências"}
          </button>
        </Card>
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

      {tab === "watchlist" ? (
        <Card className="p-5">
          <SectionTitle title="Watchlist" subtitle="Acompanhe ativos que ainda não estão na carteira" />
          <div className="flex flex-wrap gap-2">
            <input
              value={newTicker}
              onChange={(event) => setNewTicker(event.target.value.toUpperCase())}
              placeholder="PETR4"
              className="input w-auto min-w-[160px]"
            />
            <button
              type="button"
              className="btn-primary"
              disabled={!newTicker.trim() || addWatch.isPending}
              onClick={() => addWatch.mutate(newTicker.trim())}
            >
              <Plus size={15} /> Adicionar
            </button>
          </div>
          {/* A failed fetch must not read as an empty watchlist — in a finance
              app "there is nothing here" after a network error is a false
              all-clear. */}
          {watchlist.isError ? (
            <div className="mt-4">
              <ErrorState error={watchlist.error} retry={() => watchlist.refetch()} />
            </div>
          ) : watchlist.isLoading ? (
            <div className="mt-4 space-y-2">
              {Array.from({ length: 3 }).map((_, index) => (
                <Skeleton key={index} className="h-9 w-full" />
              ))}
            </div>
          ) : watchlist.data?.length ? (
            <ul className="mt-4 divide-y divide-line">
              {watchlist.data.map((item) => (
                <li key={item.id} className="flex items-center justify-between py-1.5">
                  <span>
                    <span className="font-medium">{item.ticker}</span>
                    {item.note ? <span className="ml-2 text-xs text-ink-muted">{item.note}</span> : null}
                  </span>
                  <span className="flex items-center gap-2">
                    <span className="tnum text-sm text-ink-secondary">{item.price ? money(item.price) : "—"}</span>
                    <button
                      type="button"
                      onClick={() => removeWatch.mutate(item.id)}
                      className="rounded-lg p-2.5 text-ink-muted transition-colors hover:bg-surface-hover hover:text-negative"
                      aria-label={`Remover ${item.ticker}`}
                    >
                      <Trash2 size={15} />
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-3 text-sm text-ink-muted">Nenhum ativo na watchlist.</p>
          )}
        </Card>
      ) : null}

      {tab === "dados" ? (
      <>
        <CorporateActions />

        <UniverseSettings />

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
            O arquivo carrega tudo — carteiras, movimentações, cotações, histórico — e serve para
            levar seus dados a outra instalação (por exemplo, do Docker para o aplicativo desktop):
            basta arrastá-lo na página Importar de uma instalação vazia. O import é recusado se o
            destino já tiver movimentações, para nunca misturar duas histórias.
          </p>
        </Card>

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
        </>
      ) : null}

      {tab === "sistema" ? (
        <>
        <AiProviderCard settings={settings.data} />

        <Card className="p-5">
          <SectionTitle
            title="Chaves de API"
            subtitle="Suas próprias chaves, guardadas apenas neste computador"
          />
          <div className="space-y-4">
            <SecretField
              label="brapi (cotações)"
              hint="Opcional — só se usar o provedor brapi. Crie em brapi.dev."
              placeholder="token brapi"
              configured={Boolean(settings.data.secrets?.brapi_token)}
              settingKey="brapi_token"
            />
          </div>
          <p className="mt-4 text-xs text-ink-muted">
            As chaves ficam no banco de dados local, nunca aparecem de volta nesta tela e não
            saem no export .gumbinvest. Deixar em branco e salvar remove a chave.
          </p>
        </Card>

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
    </div>
  );
}

/** Provider, model and key for the AI chat — any OpenAI-compatible vendor or
 *  Anthropic. The model field is free text on purpose: vendors ship models
 *  faster than a hardcoded list could chase. */
function AiProviderCard({ settings: data }: { settings: AppSettings }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [provider, setProvider] = useState(data.ai.active_provider);
  const [model, setModel] = useState(data.ai.active_model);
  const [customModel, setCustomModel] = useState(false);

  const info = data.ai.providers.find((p) => p.id === provider) ?? data.ai.providers[0];
  // The provider's live catalog, fetched with the user's own key — vendors
  // retire models faster than any list shipped with the app.
  const liveModels = useQuery({
    queryKey: ["ai-models", provider],
    queryFn: () => api.aiModels(provider),
    staleTime: 5 * 60_000,
  });
  const knownModels = liveModels.data?.models ?? info.models;

  const saveAi = useMutation({
    mutationFn: () =>
      api.updateSettings({ ai_provider: provider, ai_model: model.trim() || info.default_model }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      toast.success("Provedor e modelo salvos.", {
        description: `${info.label} · ${model.trim() || info.default_model}`,
      });
    },
    onError: (error) => toast.error("Não foi possível salvar o provedor.", error),
  });

  return (
    <Card className="p-5">
      <SectionTitle
        title="Inteligência Artificial (chat)"
        subtitle="Escolha o provedor e o modelo das conversas — com a sua própria chave"
      />
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Provedor</span>
          <Select
            ariaLabel="Provedor de IA"
            value={provider}
            onChange={(next) => {
              setProvider(next);
              setCustomModel(false);
              const chosen = data.ai.providers.find((p) => p.id === next);
              if (chosen) setModel(chosen.default_model);
            }}
            options={data.ai.providers.map((p) => ({
              value: p.id,
              label: p.label,
            }))}
          />
        </div>
        <div>
          <span className="mb-1.5 block text-xs font-medium text-ink-muted">Modelo</span>
          <Select
            ariaLabel="Modelo de IA"
            value={customModel || !knownModels.includes(model) ? "__custom__" : model}
            onChange={(next) => {
              if (next === "__custom__") {
                setCustomModel(true);
              } else {
                setCustomModel(false);
                setModel(next);
              }
            }}
            options={[
              ...knownModels.map((name) => ({ value: name, label: name })),
              { value: "__custom__", label: "Outro (digitar)…" },
            ]}
          />
          {customModel || !knownModels.includes(model) ? (
            <input
              value={model}
              onChange={(event) => setModel(event.target.value)}
              placeholder={info.default_model}
              className="input mt-2 w-full"
              autoFocus
            />
          ) : null}
          <p className="mt-1 text-xs text-ink-muted">
            {liveModels.data?.live
              ? "Lista carregada direto do provedor com a sua chave."
              : "Lista sugerida — salve a chave do provedor para carregar a lista real."}
          </p>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="btn-primary"
          disabled={saveAi.isPending}
          onClick={() => saveAi.mutate()}
        >
          {saveAi.isPending ? "Salvando…" : "Salvar provedor e modelo"}
        </button>
      </div>
      <div className="mt-5 space-y-4 border-t border-line pt-4">
        <p className="text-xs font-medium text-ink-muted">
          Chaves por provedor — todas ficam salvas ao mesmo tempo; trocar o provedor acima usa a
          chave dele.
        </p>
        {data.ai.providers.map((p) => (
          <SecretField
            key={p.id}
            label={p.label}
            hint={`Crie a sua em ${p.key_hint}.`}
            placeholder="chave de API"
            configured={p.key_configured}
            settingKey={p.key_setting}
          />
        ))}
      </div>
      <p className="mt-4 text-xs text-ink-muted">
        Crie a chave no site indicado em cada provedor e cole aqui. As chaves ficam só neste
        computador.
      </p>
    </Card>
  );
}

/** Write-only API-key input: saves through the settings endpoint, never shows
 *  the stored value back — only whether one is configured. */
function SecretField({
  label,
  hint,
  placeholder,
  configured,
  settingKey,
}: {
  label: string;
  hint: string;
  placeholder: string;
  configured: boolean;
  settingKey: string;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [value, setValue] = useState("");
  const saveKey = useMutation({
    mutationFn: (next: string) => api.updateSettings({ [settingKey]: next }),
    onSuccess: (_data, next) => {
      setValue("");
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      // Saving an empty field is how a key is deleted — say which one happened.
      toast.success(next ? `Chave salva: ${label}.` : `Chave removida: ${label}.`);
    },
    onError: (error) => toast.error(`Não foi possível salvar a chave de ${label}.`, error),
  });

  return (
    <div>
      <div className="mb-1.5 flex items-center gap-2">
        <span className="text-xs font-medium text-ink-muted">{label}</span>
        <Badge tone={configured ? "positive" : "warning"}>
          {configured ? "configurada" : "ausente"}
        </Badge>
      </div>
      <div className="flex flex-wrap gap-2">
        <input
          type="password"
          autoComplete="off"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder={configured ? "•••••• (salvar em branco remove)" : placeholder}
          className="input min-w-0 flex-1 sm:min-w-[280px] sm:flex-none sm:w-auto"
        />
        <button
          type="button"
          className="btn-primary"
          disabled={saveKey.isPending}
          onClick={() => saveKey.mutate(value.trim())}
        >
          {saveKey.isPending ? "Salvando…" : "Salvar"}
        </button>
      </div>
      <p className="mt-1 text-xs text-ink-muted">{hint}</p>
    </div>
  );
}
