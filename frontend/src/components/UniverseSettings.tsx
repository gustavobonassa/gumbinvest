/**
 * Universo de ativos → Sincronização: the switch, market selection and SEC
 * contact e-mail. Starting and monitoring the sync itself lives in
 * UniverseJobs, rendered right below this — a single "start sync" button
 * shared by both the first download and later re-syncs.
 */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Database } from "lucide-react";

import { api } from "@/lib/api";
import { Card, SectionTitle, Badge } from "@/components/ui";
import { useToast } from "@/components/Toast";
import { kindLabel } from "@/lib/format";

const MARKETS: { value: string; label: string; hint: string }[] = [
  { value: "B3", label: "B3 (Brasil)", hint: "ações, FIIs, ETFs e BDRs — com fundamentos da CVM" },
  {
    value: "US",
    label: "EUA (SEC)",
    hint:
      "stocks e REITs com balanços da SEC (ROE, margens, crescimento). Sem preço: " +
      "nenhuma fonte pública em massa publica cotações dos EUA, então não há P/L nem P/VP. " +
      "Exige SEC_USER_AGENT com um e-mail real no .env.",
  },
];

/** The shape the SEC accepts. Any address the user controls will do. */
const SEC_SUGGESTION = "GumbInvest/1.0 (contato: seu-email@exemplo.com)";

export default function UniverseSettings() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [markets, setMarkets] = useState<string[]>(["B3"]);
  const [secAgent, setSecAgent] = useState("");

  const statusQ = useQuery({
    queryKey: ["universe-status"],
    queryFn: api.universeStatus,
    // Self-terminating poll: the backend already discounts a stale heartbeat,
    // so `active` is the honest answer to "is something running".
    refetchInterval: (query) => (query.state.data?.active ? 2_000 : false),
  });
  const status = statusQ.data;
  const running = Boolean(status?.active);

  useEffect(() => {
    if (status?.settings?.markets?.length) setMarkets(status.settings.markets);
  }, [status?.settings?.markets?.join(",")]);
  useEffect(() => {
    if (status?.settings?.sec_user_agent) setSecAgent(status.settings.sec_user_agent);
  }, [status?.settings?.sec_user_agent]);

  const save = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.updateSettings(values),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      queryClient.invalidateQueries({ queryKey: ["universe-status"] });
      // The checkbox flips optimistically; the toast confirms it persisted.
      toast.success("Preferência salva.");
    },
    onError: (error) => toast.error("Não foi possível salvar a preferência.", error),
  });

  const enabled = Boolean(status?.settings?.enabled);
  const coverage = status?.coverage;

  return (
    <Card className="p-5">
      <SectionTitle
        title="Universo de ativos"
        subtitle="Todos os papéis listados, baixados das fontes oficiais B3 e CVM"
        action={
          coverage && coverage.total > 0 ? (
            <Badge tone="neutral">{coverage.total.toLocaleString("pt-BR")} ativos</Badge>
          ) : null
        }
      />

      <label className="flex cursor-pointer items-start gap-3">
        <input
          type="checkbox"
          className="mt-1"
          checked={enabled}
          disabled={save.isPending}
          onChange={(event) => save.mutate({ universe_enabled: event.target.checked })}
        />
        <span className="text-sm">
          <span className="font-medium">Manter um universo de ativos local</span>
          <span className="mt-1 block text-xs text-ink-muted">
            Baixa arquivos públicos da B3 (cotações históricas) e da CVM (balanços e informes de
            FIIs) para montar uma tabela local com preço, liquidez, P/L, P/VP, ROE, margens e
            dividend yield de todos os papéis listados. Serve para filtrar ativos por critérios
            objetivos e para dar à IA uma lista real de candidatos. Nada é enviado para fora e
            nenhuma chave é necessária.
          </span>
        </span>
      </label>

      {enabled ? (
        <>
          <div className="mt-4 space-y-2">
            {MARKETS.map((item) => (
              <label key={item.value} className="flex cursor-pointer items-start gap-3">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={markets.includes(item.value)}
                  disabled={running}
                  onChange={(event) => {
                    const next = event.target.checked
                      ? [...markets, item.value]
                      : markets.filter((m) => m !== item.value);
                    setMarkets(next);
                    save.mutate({ universe_markets: next });
                  }}
                />
                <span className="text-sm">
                  {item.label}
                  <span className="mt-0.5 block text-xs text-ink-muted">{item.hint}</span>
                </span>
              </label>
            ))}
          </div>

          {markets.includes("US") ? (
            <div className="mt-3 rounded-xl border border-line p-3">
              <label className="block text-xs text-ink-muted">
                E-mail de contato para a SEC
                <input
                  className="input mt-1 w-full"
                  placeholder={SEC_SUGGESTION}
                  value={secAgent}
                  onChange={(event) => setSecAgent(event.target.value)}
                  onBlur={() =>
                    secAgent.trim() !== (status?.settings?.sec_user_agent ?? "") &&
                    save.mutate({ sec_user_agent: secAgent.trim() })
                  }
                />
              </label>
              <p className="mt-2 text-xs text-ink-muted">
                A SEC recusa (403) qualquer requisição sem um e-mail de contato na identificação
                do cliente.
                Se preferir não informar nenhum, desmarque o mercado EUA — todo o resto do
                universo vem da B3 e da CVM, que publicam os arquivos abertamente e não pedem
                identificação.
              </p>

            </div>
          ) : null}

          {!running && status?.stale ? (
            <p className="mt-3 text-sm text-warning">
              A última atualização foi interrompida. Inicie a sincronização abaixo para retomar de
              onde parou.
            </p>
          ) : null}

          {!running && status?.state === "error" ? (
            <p className="mt-3 text-sm text-negative">{status.message}</p>
          ) : null}

          {status?.warnings?.length ? (
            <ul className="mt-3 space-y-1 text-xs text-warning">
              {status.warnings.map((warning) => (
                <li key={warning}>• {warning}</li>
              ))}
            </ul>
          ) : null}

          {coverage && coverage.total > 0 ? (
            <p className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
              <Database size={13} />
              <span className="tnum">{coverage.total.toLocaleString("pt-BR")} ativos</span>
              <span className="tnum">
                {coverage.with_fundamentals.toLocaleString("pt-BR")} com fundamentos
              </span>
              {Object.entries(coverage.by_kind)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 5)
                .map(([kind, count]) => (
                  <span key={kind} className="tnum">
                    {count} {kindLabel(kind)}
                  </span>
                ))}
            </p>
          ) : null}
        </>
      ) : null}
    </Card>
  );
}
