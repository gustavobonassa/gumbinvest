/**
 * Carteira IA — virtual wallets generated and managed by an AI model.
 *
 * Each wallet is pinned to the provider/model that was active in Configurações
 * when it was created; per-category tabs generate and review that slice with
 * R$ 10.000 virtuais each. Nothing here touches the user's real portfolio.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import CategoryTab from "@/components/ai-wallet/CategoryTab";
import HistoryTab from "@/components/ai-wallet/HistoryTab";
import OverviewTab from "@/components/ai-wallet/OverviewTab";
import { useToast } from "@/components/Toast";
import { Badge, Card, EmptyState, ErrorState, Modal, Select, Skeleton, Tabs } from "@/components/ui";
import { api } from "@/lib/api";

export default function CarteiraIA() {
  const [params, setParams] = useSearchParams();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [createOpen, setCreateOpen] = useState(false);
  const [walletName, setWalletName] = useState("");
  const [chosenProvider, setChosenProvider] = useState<string | null>(null);
  const [chosenModel, setChosenModel] = useState<string | null>(null);

  const settingsQ = useQuery({ queryKey: ["settings"], queryFn: api.settings, staleTime: 5 * 60_000 });
  const walletsQ = useQuery({ queryKey: ["ai-wallets"], queryFn: api.aiWallets });

  const wallets = walletsQ.data ?? [];
  const paramWallet = Number(params.get("carteira"));
  const walletId = wallets.some((item) => item.id === paramWallet)
    ? paramWallet
    : wallets[0]?.id ?? null;

  const detailQ = useQuery({
    queryKey: ["ai-wallet", walletId],
    queryFn: () => api.aiWalletDetail(walletId as number),
    enabled: walletId !== null,
  });

  const activeAi = settingsQ.data?.ai;
  // Only providers whose key is configured can run a wallet.
  const configuredProviders = (activeAi?.providers ?? []).filter((item) => item.key_configured);
  const keyConfigured = configuredProviders.length > 0;
  const defaultProvider =
    configuredProviders.find((item) => item.id === activeAi?.active_provider)?.id ??
    configuredProviders[0]?.id ??
    null;
  const provider =
    chosenProvider && configuredProviders.some((item) => item.id === chosenProvider)
      ? chosenProvider
      : defaultProvider;
  const providerInfo = configuredProviders.find((item) => item.id === provider);

  // Live model catalog from the provider (same source as Configurações).
  const modelsQ = useQuery({
    queryKey: ["ai-models", provider],
    queryFn: () => api.aiModels(provider as string),
    enabled: createOpen && provider !== null,
    staleTime: 5 * 60_000,
  });
  const models = modelsQ.data?.models?.length ? modelsQ.data.models : providerInfo?.models ?? [];
  const defaultModel =
    provider === activeAi?.active_provider && activeAi?.active_model
      ? activeAi.active_model
      : providerInfo?.default_model ?? models[0] ?? "";
  const model = chosenModel && models.includes(chosenModel) ? chosenModel : defaultModel;
  const modelOptions = models.includes(model) || !model ? models : [model, ...models];

  const create = useMutation({
    mutationFn: (payload: { name: string; provider?: string; model?: string }) =>
      api.createAiWallet(payload),
    onSuccess: (wallet) => {
      queryClient.invalidateQueries({ queryKey: ["ai-wallets"] });
      setCreateOpen(false);
      setWalletName("");
      setChosenProvider(null);
      setChosenModel(null);
      setParams((current) => {
        const next = new URLSearchParams(current);
        next.set("carteira", String(wallet.id));
        next.delete("aba");
        return next;
      });
      toast.success(`Carteira “${wallet.name}” criada.`, {
        description: `${wallet.provider_label} · ${wallet.model}`,
      });
    },
    // The modal shows the message too, but a toast survives the modal closing.
    onError: (error) => toast.error("Não foi possível criar a carteira.", error),
  });

  // Deleting a wallet erases its whole history — it earns a confirm.
  const [confirmDelete, setConfirmDelete] = useState(false);
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteAiWallet(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["ai-wallets"] });
      queryClient.invalidateQueries({ queryKey: ["ai-wallet-compare"] });
      setParams((current) => {
        const next = new URLSearchParams(current);
        next.delete("carteira");
        next.delete("aba");
        return next;
      });
      toast.success("Carteira excluída.");
    },
    onError: (error) => toast.error("Não foi possível excluir a carteira.", error),
  });

  const detail = detailQ.data;
  const tabOptions = useMemo(() => {
    const categories = detail?.categories ?? [];
    return [
      { value: "visao", label: "Visão geral" },
      ...categories.map((block) => ({
        value: block.category,
        label: block.label,
        count: block.pending_suggestions > 0 ? block.pending_suggestions : undefined,
      })),
      { value: "historico", label: "Histórico de mudanças" },
    ];
  }, [detail]);

  const abaParam = params.get("aba") ?? "visao";
  const aba = tabOptions.some((option) => option.value === abaParam) ? abaParam : "visao";

  const setQueryParam = (key: string, value: string | null) => {
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (value === null) next.delete(key);
        else next.set(key, value);
        return next;
      },
      { replace: true },
    );
  };

  if (walletsQ.isError) {
    return <ErrorState error={walletsQ.error} retry={() => walletsQ.refetch()} />;
  }

  const createModal = (
    <Modal
      open={createOpen}
      title="Nova carteira IA"
      subtitle="O modelo escolhido fica fixo na carteira: é ele quem gera e sugere mudanças."
      onClose={() => setCreateOpen(false)}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (walletName.trim() && provider && model) {
            create.mutate({ name: walletName.trim(), provider, model });
          }
        }}
        className="space-y-4"
      >
        <label className="block text-sm text-ink-secondary">
          Nome da carteira
          <input
            className="input mt-1 w-full"
            value={walletName}
            onChange={(event) => setWalletName(event.target.value)}
            placeholder="Ex.: Competidor 1"
            maxLength={120}
            autoFocus
          />
        </label>
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="block text-sm text-ink-secondary">
            Provedor
            <Select
              ariaLabel="Provedor de IA"
              className="mt-1 w-full"
              value={provider ?? ""}
              onChange={(next) => {
                setChosenProvider(next);
                setChosenModel(null);
              }}
              options={configuredProviders.map((item) => ({ value: item.id, label: item.label }))}
            />
          </label>
          <label className="block text-sm text-ink-secondary">
            Modelo
            <Select
              ariaLabel="Modelo"
              className="mt-1 w-full"
              value={model}
              onChange={(next) => setChosenModel(next)}
              options={modelOptions.map((item) => ({ value: item, label: item }))}
              disabled={modelsQ.isLoading && modelOptions.length === 0}
            />
          </label>
        </div>
        <p className="text-xs text-ink-muted">
          Só aparecem provedores com chave configurada em Configurações → Inteligência Artificial.
          {modelsQ.data?.live === false ? " Lista de modelos sugerida (catálogo ao vivo indisponível)." : ""}
        </p>
        {create.isError ? (
          <p className="text-xs text-negative">{(create.error as Error).message}</p>
        ) : null}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={() => setCreateOpen(false)}>
            Cancelar
          </button>
          <button
            type="submit"
            className="btn-primary"
            disabled={!walletName.trim() || !provider || !model || create.isPending}
          >
            {create.isPending ? "Criando…" : "Criar carteira"}
          </button>
        </div>
      </form>
    </Modal>
  );

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">Ferramentas</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Carteira IA</h1>
        <p className="mt-2 max-w-2xl text-sm text-ink-secondary">
          Carteiras virtuais montadas e geridas inteiramente por IA. Cada categoria recebe
          R$ 10.000 fictícios e o desempenho é acompanhado no tempo. Crie uma carteira por
          modelo e compare quem investe melhor. Nada aqui usa a sua carteira real.
        </p>
      </header>

      {walletsQ.isLoading ? (
        <Skeleton className="h-80 w-full" />
      ) : wallets.length === 0 ? (
        <EmptyState
          icon={Bot}
          title="Nenhuma carteira IA ainda"
          description={
            keyConfigured
              ? "Crie a primeira carteira escolhendo o nome e o modelo de IA que vai geri-la: cada carteira fica presa ao seu modelo, ideal para competição."
              : "Informe a chave de um provedor de IA em Configurações → Inteligência Artificial para começar."
          }
          action={
            keyConfigured ? (
              <button type="button" className="btn-primary" onClick={() => setCreateOpen(true)}>
                <Plus size={15} /> Criar primeira carteira
              </button>
            ) : undefined
          }
        />
      ) : (
        <>
          <Card className="flex flex-wrap items-center gap-3 p-4">
            <Select
              ariaLabel="Carteira ativa"
              className="w-auto min-w-[260px]"
              value={String(walletId)}
              onChange={(next) => {
                // One setParams call: react-router's functional updater reads
                // the pre-click URL, so two sequential calls lose the first.
                setParams(
                  (current) => {
                    const params = new URLSearchParams(current);
                    params.set("carteira", next);
                    params.delete("aba");
                    return params;
                  },
                  { replace: true },
                );
              }}
              options={wallets.map((item) => ({
                value: String(item.id),
                label: item.name,
                hint: `${item.provider_label} · ${item.model}`,
              }))}
            />
            {detail ? (
              <Badge tone={detail.web_search ? "accent" : "warning"}>
                {detail.web_search ? "busca na web" : "sem busca na web"}
              </Badge>
            ) : null}
            <div className="ml-auto flex items-center gap-2">
              <button type="button" className="btn-ghost" onClick={() => setCreateOpen(true)}>
                <Plus size={15} /> Nova carteira
              </button>
              <button
                type="button"
                className="btn-ghost text-negative"
                title="Excluir esta carteira e todo o histórico dela"
                disabled={remove.isPending || walletId === null}
                onClick={() => setConfirmDelete(true)}
              >
                <Trash2 size={15} /> Excluir
              </button>
            </div>
          </Card>

          <Modal
            open={confirmDelete}
            title="Excluir esta carteira IA?"
            subtitle={wallets.find((item) => item.id === walletId)?.name}
            onClose={() => setConfirmDelete(false)}
          >
            <p className="text-sm text-ink-secondary">
              A carteira, as posições virtuais, as sugestões e todo o histórico de decisões do
              modelo serão apagados. Sua carteira real não é afetada. Esta ação não pode ser
              desfeita.
            </p>
            <div className="mt-4 flex flex-wrap justify-end gap-2">
              <button type="button" className="btn-ghost" onClick={() => setConfirmDelete(false)}>
                Cancelar
              </button>
              <button
                type="button"
                className="btn-primary bg-negative hover:brightness-110"
                disabled={remove.isPending}
                onClick={() => {
                  if (walletId !== null) remove.mutate(walletId);
                  setConfirmDelete(false);
                }}
              >
                {remove.isPending ? "Excluindo…" : "Excluir carteira"}
              </button>
            </div>
          </Modal>

          {detailQ.isError ? (
            <ErrorState error={detailQ.error} retry={() => detailQ.refetch()} />
          ) : !detail ? (
            <Skeleton className="h-96 w-full" />
          ) : (
            <>
              <Tabs value={aba} options={tabOptions} onChange={(next) => setQueryParam("aba", next)} />
              {aba === "visao" ? (
                <OverviewTab wallet={detail} />
              ) : aba === "historico" ? (
                <HistoryTab wallet={detail} />
              ) : (
                <CategoryTab
                  key={`${detail.id}-${aba}`}
                  wallet={detail}
                  block={detail.categories.find((item) => item.category === aba)!}
                />
              )}
            </>
          )}
        </>
      )}
      {createModal}
    </div>
  );
}
