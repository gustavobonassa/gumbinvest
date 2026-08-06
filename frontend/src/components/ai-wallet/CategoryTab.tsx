/**
 * One category of an AI wallet: generate its slice, review it, ask for changes.
 *
 * Generation and suggestions run as BACKEND jobs — clicking the button only
 * starts one; this component polls its status every couple of seconds while it
 * runs. Switching tabs, reloading or closing the browser does not lose the
 * run: on return, the poll picks the job back up (or its finished result).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { ChevronDown, ChevronUp, Sparkles, Wand2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import SuggestionList from "@/components/ai-wallet/SuggestionList";
import { useToast } from "@/components/Toast";
import { Badge, Card, StatTile } from "@/components/ui";
import type { AiWalletCategoryBlock, AiWalletDetail, AiWalletPositionRow } from "@/lib/api";
import { api, ApiError } from "@/lib/api";
import { dateTime, money, percent, quantity as formatQuantity, toneOf } from "@/lib/format";

interface SkippedItem {
  ticker?: string;
  reason?: string;
}

export default function CategoryTab({
  wallet,
  block,
}: {
  wallet: AiWalletDetail;
  block: AiWalletCategoryBlock;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [expanded, setExpanded] = useState<number | null>(null);
  const processedJobRef = useRef<string | null>(null);
  // Jobs this screen actually started or watched run. A finished job found on
  // arrival (started days ago, or in another tab) must not announce itself.
  const watchedJobRef = useRef<string | null>(null);

  const jobQ = useQuery({
    queryKey: ["ai-wallet-job", wallet.id, block.category],
    queryFn: () => api.aiWalletJob(wallet.id, block.category),
    refetchInterval: (query) => (query.state.data?.active ? 2_000 : false),
  });
  const job = jobQ.data;
  const running = Boolean(job?.active);

  // When a job we were watching finishes, everything it touched must refetch —
  // and, since the run is long enough to walk away from, it says so.
  useEffect(() => {
    if (!job || !job.id) return;
    if (job.active) {
      watchedJobRef.current = job.id;
      return;
    }
    if (processedJobRef.current === job.id) return;
    processedJobRef.current = job.id;
    queryClient.invalidateQueries({ queryKey: ["ai-wallet", wallet.id] });
    queryClient.invalidateQueries({ queryKey: ["ai-wallets"] });
    queryClient.invalidateQueries({ queryKey: ["ai-wallet-suggestions", wallet.id] });
    queryClient.invalidateQueries({ queryKey: ["ai-wallet-events", wallet.id] });
    queryClient.invalidateQueries({ queryKey: ["ai-wallet-compare"] });

    if (watchedJobRef.current !== job.id) return;
    watchedJobRef.current = null;
    const what = job.kind === "suggest" ? "as sugestões" : "a carteira";
    if (job.error) {
      toast.error(`A IA não conseguiu gerar ${what} de ${block.label}.`, undefined, {
        description: job.error,
      });
    } else {
      toast.success(
        job.kind === "suggest"
          ? `Sugestões prontas para ${block.label}.`
          : `Carteira de ${block.label} gerada.`,
      );
    }
  }, [job, queryClient, wallet.id, block.label, toast]);

  const start = useMutation({
    mutationFn: (kind: "generate" | "suggest") =>
      kind === "generate"
        ? api.generateAiWalletCategory(wallet.id, block.category)
        : api.suggestAiWalletCategory(wallet.id, block.category),
    onSuccess: (started, kind) => {
      // Remembered here too: a job that finishes between two polls would
      // otherwise never be seen running, and end silently.
      if (started.id) watchedJobRef.current = started.id;
      queryClient.invalidateQueries({ queryKey: ["ai-wallet-job", wallet.id, block.category] });
      toast.info(
        kind === "generate"
          ? `Gerando a carteira de ${block.label}…`
          : `Analisando ${block.label} em busca de mudanças…`,
        { description: "Pode levar alguns minutos; você pode navegar à vontade." },
      );
    },
    onError: (error) => toast.error("Não foi possível iniciar a análise.", error),
  });

  const skipped = (job?.result as { skipped?: SkippedItem[] } | null)?.skipped ?? [];
  const deferred = (job?.result as { pending?: SkippedItem[] } | null)?.pending ?? [];
  const startError =
    start.isError && (start.error instanceof ApiError ? start.error.message : "Não foi possível iniciar.");

  const jobNotice = (
    <>
      {running && job?.status ? (
        <p className="mt-3 flex items-center gap-2 text-sm text-ink-secondary">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" aria-hidden />
          {job.status}
          <span className="text-xs text-ink-muted">— pode levar alguns minutos; você pode navegar à vontade.</span>
        </p>
      ) : null}
      {!running && job?.error ? <p className="mt-3 text-sm text-negative">{job.error}</p> : null}
      {startError ? <p className="mt-3 text-sm text-negative">{startError}</p> : null}
      {!running && deferred.length ? (
        <p className="mt-3 text-xs text-accent">
          Aguardando cotação para comprar: {deferred.map((item) => item.ticker).join(", ")} — o
          valor ficou reservado e a compra conclui sozinha quando o preço chegar.
        </p>
      ) : null}
      {!running && skipped.length ? (
        <p className="mt-3 text-xs text-warning">
          Não reconhecidos pelo mercado:{" "}
          {skipped.map((item) => `${item.ticker ?? "?"} (${item.reason ?? "rejeitado"})`).join(", ")}
        </p>
      ) : null}
    </>
  );

  if (!block.active) {
    return (
      <Card className="p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-xl">
            <h2 className="text-base font-semibold tracking-tight text-ink">
              Gerar carteira de {block.label}
            </h2>
            <p className="mt-1 text-sm text-ink-secondary">
              {wallet.provider_label} · {wallet.model} recebe R$ 10.000 virtuais e monta a
              alocação desta categoria: os candidatos do modelo são verificados com preços e
              fundamentos reais antes da decisão final.
            </p>
            {!wallet.web_search ? (
              <p className="mt-2">
                <Badge tone="warning">sem busca na web</Badge>{" "}
                <span className="text-xs text-ink-muted">
                  Este modelo decide apenas com os dados verificados e o cenário macro.
                </span>
              </p>
            ) : null}
          </div>
          <button
            type="button"
            className="btn-primary"
            disabled={running || start.isPending || !wallet.key_configured}
            onClick={() => start.mutate("generate")}
          >
            <Sparkles size={15} /> {running ? "Gerando…" : "Gerar carteira"}
          </button>
        </div>
        {jobNotice}
      </Card>
    );
  }

  const positions = block.positions ?? [];
  const value = block.value ?? 0;
  const cash = block.cash ?? 0;
  const invested = block.budget ?? 0;
  const result = value - invested;

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-3">
        <StatTile label={`Valor em ${block.label}`} value={<span className="tnum">{money(value)}</span>} hint={`orçamento ${money(invested)}`} />
        <StatTile
          label="Resultado da categoria"
          value={<span className="tnum">{money(result)}</span>}
          tone={result > 0 ? "positive" : result < 0 ? "negative" : "neutral"}
          delta={invested > 0 ? ((value - invested) / invested) * 100 : null}
        />
        <StatTile
          label="Caixa disponível"
          value={<span className="tnum">{money(cash)}</span>}
          hint={block.generated_at ? `gerada em ${dateTime(block.generated_at)}` : undefined}
        />
      </div>

      {block.thesis ? (
        <Card className="p-5">
          <h2 className="text-sm font-semibold tracking-tight text-ink">
            Estratégia da IA para {block.label}
          </h2>
          <p className="mt-1.5 max-w-3xl whitespace-pre-line text-sm text-ink-secondary">
            {block.thesis}
          </p>
          <p className="mt-2 text-xs text-ink-muted">
            Escrita pelo modelo na geração; ele a relê a cada rodada de sugestões.
          </p>
        </Card>
      ) : null}

      <Card className="overflow-hidden p-0">
        <div className="flex flex-wrap items-center justify-between gap-3 px-5 pt-4">
          <h2 className="text-base font-semibold tracking-tight text-ink">Posições</h2>
          <button
            type="button"
            className="btn-ghost"
            disabled={running || start.isPending || !wallet.key_configured}
            onClick={() => start.mutate("suggest")}
            title="A IA analisa a categoria e propõe mudanças que você aceita ou recusa uma a uma"
          >
            <Wand2 size={15} /> {running ? "Analisando…" : "Sugerir mudanças"}
          </button>
        </div>
        <div className="px-5">{jobNotice}</div>
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-ink-muted">
                <th className="px-5 py-2.5 font-medium">Ativo</th>
                <th className="px-3 py-2.5 text-right font-medium">Quantidade</th>
                <th className="px-3 py-2.5 text-right font-medium">Preço médio</th>
                <th className="px-3 py-2.5 text-right font-medium">Custo</th>
                <th className="px-3 py-2.5 text-right font-medium">Valor atual</th>
                <th className="px-3 py-2.5 text-right font-medium">Resultado</th>
                <th className="px-3 py-2.5 text-right font-medium">Peso</th>
                <th className="w-10 px-3 py-2.5" aria-label="Justificativa" />
              </tr>
            </thead>
            <tbody>
              {positions.map((position) => (
                <PositionRows
                  key={position.id}
                  position={position}
                  expanded={expanded === position.id}
                  onToggle={() => setExpanded(expanded === position.id ? null : position.id)}
                />
              ))}
              {positions.length === 0 ? (
                <tr>
                  <td colSpan={8} className="px-5 py-8 text-center text-sm text-ink-muted">
                    Sem posições — todo o orçamento está em caixa.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </Card>

      <SuggestionList wallet={wallet} category={block.category} />
    </div>
  );
}

function PositionRows({
  position,
  expanded,
  onToggle,
}: {
  position: AiWalletPositionRow;
  expanded: boolean;
  onToggle: () => void;
}) {
  const tone = toneOf(position.pnl_brl);
  return (
    <>
      <tr className="table-row border-b border-line/60">
        <td className="px-5 py-3">
          {position.is_fixed_income ? (
            // Synthetic paper: there is no asset page behind it.
            <span className="font-medium text-ink">{position.ticker}</span>
          ) : (
            <Link to={`/ativos/${encodeURIComponent(position.ticker)}`} className="group">
              <span className="font-medium text-ink group-hover:text-accent">{position.ticker}</span>
            </Link>
          )}
          <span className="mt-0.5 block max-w-[220px] truncate text-xs text-ink-muted">
            {position.is_fixed_income ? position.fi_label : position.name}
          </span>
        </td>
        <td className="tnum px-3 py-3 text-right">
          {position.is_fixed_income || position.quantity <= 0 ? "—" : formatQuantity(position.quantity)}
        </td>
        <td className="tnum px-3 py-3 text-right">
          {position.is_fixed_income || position.quantity <= 0
            ? "—"
            : money(position.avg_price, { currency: position.currency !== "BRL" ? position.currency : undefined })}
        </td>
        <td className="tnum px-3 py-3 text-right">{money(position.cost_brl)}</td>
        <td className="tnum px-3 py-3 text-right">
          {money(position.market_value_brl)}
          {position.pending_brl > 0 ? (
            <span className="block text-[11px] text-accent">aguardando cotação para comprar</span>
          ) : !position.priced ? (
            <span className="block text-[11px] text-warning">sem cotação — ao custo</span>
          ) : null}
        </td>
        <td className="px-3 py-3 text-right">
          <span className={clsx("tnum block font-medium", tone === "positive" && "text-positive", tone === "negative" && "text-negative")}>
            {money(position.pnl_brl)}
          </span>
          <span className={clsx("tnum text-xs", tone === "positive" && "text-positive", tone === "negative" && "text-negative", tone === "neutral" && "text-ink-muted")}>
            {percent(position.pnl_pct, 2, true)}
          </span>
        </td>
        <td className="tnum px-3 py-3 text-right">{percent(position.weight_pct, 1)}</td>
        <td className="px-3 py-3 text-right">
          {position.rationale ? (
            <button
              type="button"
              className="btn-ghost px-1.5 py-1"
              onClick={onToggle}
              aria-expanded={expanded}
              title="Justificativa da IA"
            >
              {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </button>
          ) : null}
        </td>
      </tr>
      {expanded && position.rationale ? (
        <tr className="border-b border-line/60 bg-surface-hover/40">
          <td colSpan={8} className="px-5 py-3 text-sm text-ink-secondary">
            <span className="text-xs font-medium uppercase tracking-wide text-ink-muted">
              Justificativa da IA
            </span>
            <p className="mt-1 max-w-3xl">{position.rationale}</p>
          </td>
        </tr>
      ) : null}
    </>
  );
}
