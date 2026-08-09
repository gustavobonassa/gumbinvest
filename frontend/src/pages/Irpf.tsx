/**
 * IRPF — the year's declaration, one block at a time.
 *
 * A worksheet to transcribe, never a filing. The information architecture is
 * the form's own: the declaration is filled block by block, so the page is
 * *navigated* block by block rather than scrolled. Three rules follow from it:
 *
 *  * **One block on screen.** Bens e Direitos alone runs to seventy lines here;
 *    stacked under three more tables it was a page nobody could find anything
 *    in. Each tab is one ficha, and the second level splits the ficha the same
 *    way the form does — by grupo, by pot of income.
 *  * **The pendências are a tab with a count, not a banner.** They have to be
 *    impossible to miss without pushing the numbers below the fold forever.
 *  * **The totals never move.** The stat row sits above the tabs, so switching
 *    block never costs you the figure you were checking against.
 *
 * Every line is copyable, singly and by the block: the work being saved here is
 * transcription.
 */
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import {
  AlertTriangle,
  CheckCircle2,
  ClipboardList,
  Coins,
  Copy,
  Landmark,
  Search,
  TrendingUp,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useToast } from "@/components/Toast";
import {
  Card,
  ErrorState,
  KindTag,
  SectionTitle,
  Segmented,
  Skeleton,
  StatTile,
  Tabs,
} from "@/components/ui";
import { api, type IrpfBem, type IrpfGap, type IrpfIncomeRow, type IrpfReport } from "@/lib/api";
import { money, periodLabel, quantity as formatQuantity, shortDate } from "@/lib/format";

type Block = "conferir" | "bens" | "rendimentos" | "vendas";
type Pot = "isentos" | "exclusiva" | "exterior";

/** What each disposal bucket answers to. The rules genuinely differ. */
const BUCKET_LABELS: Record<string, string> = {
  acoes: "Ações",
  fii: "FII",
  cripto: "Cripto",
  renda_fixa: "Renda fixa",
  exterior: "Exterior",
};
const BUCKET_ORDER = ["acoes", "fii", "cripto", "renda_fixa", "exterior"];

const INCOME_LABELS: Record<string, string> = {
  DIVIDEND: "Dividendo",
  JCP: "JCP",
  YIELD: "Rendimento",
  INTEREST: "Juros",
};

const POT_TITLES: Record<Pot, { label: string; title: string; subtitle: string }> = {
  isentos: {
    label: "Isentos",
    title: "Rendimentos Isentos e Não Tributáveis",
    subtitle: "Dividendos e rendimentos de fundos pagos por fonte brasileira",
  },
  exclusiva: {
    label: "Exclusiva",
    title: "Rendimentos Sujeitos à Tributação Exclusiva",
    subtitle: "JCP e juros de renda fixa — já tributados na fonte",
  },
  exterior: {
    label: "Exterior",
    title: "Rendimentos do exterior",
    subtitle:
      "Fora dos dois blocos acima de propósito: um dividendo estrangeiro não é isento no Brasil",
  },
};

function useCopy() {
  const toast = useToast();
  return async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success(`${what} copiado`);
    } catch {
      toast.error("O navegador não liberou a área de transferência");
    }
  };
}

function CopyButton({
  text,
  what,
  label,
}: {
  text: string;
  what: string;
  label?: string;
}) {
  const copy = useCopy();
  return (
    <button
      type="button"
      onClick={() => copy(text, what)}
      title={`Copiar ${what.toLowerCase()}`}
      className={clsx(
        "inline-flex shrink-0 items-center gap-1.5 rounded-lg text-ink-muted hover:bg-surface-hover hover:text-ink",
        label ? "border border-line px-2.5 py-1.5 text-xs" : "p-1.5",
      )}
    >
      <Copy size={13} />
      {label}
    </button>
  );
}

/* ------------------------------------------------------------------ blocks */

function Conferir({ gaps }: { gaps: IrpfGap[] }) {
  if (!gaps.length) {
    return (
      <Card className="p-8">
        <p className="flex items-center justify-center gap-2 text-sm text-positive">
          <CheckCircle2 size={16} />
          Nada pendente: todo valor desta declaração tem origem no extrato.
        </p>
      </Card>
    );
  }
  return (
    <div className="space-y-4">
      <p className="max-w-3xl text-sm text-ink-secondary">
        O que este relatório não consegue responder sozinho. Nenhum destes itens foi preenchido
        com um valor provável — resolva o que der antes de transcrever os outros blocos.
      </p>
      <div className="grid gap-4 xl:grid-cols-2">
        {gaps.map((gap) => (
          <Card key={gap.kind} className="border-warning/40 p-5">
            <div className="flex items-start gap-3">
              <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-warning/15 text-warning">
                <AlertTriangle size={14} />
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-ink">{gap.title}</p>
                <p className="mt-1 text-sm text-ink-secondary">{gap.detail}</p>
                <p className="mt-2.5 flex flex-wrap gap-1.5">
                  {gap.tickers.map((ticker) => (
                    <Link
                      key={ticker}
                      to={`/ativos/${ticker}`}
                      className="tnum rounded-md bg-surface-hover px-1.5 py-0.5 text-xs text-ink-secondary hover:text-accent"
                    >
                      {ticker}
                    </Link>
                  ))}
                </p>
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

/** Tab-separated, so a block pastes straight into a spreadsheet. */
function bensAsTable(rows: IrpfBem[], year: number): string {
  const header = [
    "Grupo",
    "Código",
    "Ativo",
    "CNPJ",
    "Quantidade",
    `Em 31/12/${year - 1}`,
    `Em 31/12/${year}`,
    "Discriminação",
  ];
  const body = rows.map((row) =>
    [
      row.grupo,
      row.codigo,
      row.ticker,
      row.cnpj ?? "",
      row.quantity,
      row.cost_previous,
      row.cost,
      row.discriminacao,
    ].join("\t"),
  );
  return [header.join("\t"), ...body].join("\n");
}

function Bens({ report }: { report: IrpfReport }) {
  const [grupo, setGrupo] = useState("todos");
  const [term, setTerm] = useState("");
  const year = report.year ?? 0;

  const grupos = useMemo(() => {
    const seen = new Map<string, number>();
    for (const row of report.bens) seen.set(row.grupo, (seen.get(row.grupo) ?? 0) + 1);
    return [...seen.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [report.bens]);

  const rows = useMemo(() => {
    const needle = term.trim().toLowerCase();
    return report.bens.filter(
      (row) =>
        (grupo === "todos" || row.grupo === grupo) &&
        (!needle ||
          row.ticker.toLowerCase().includes(needle) ||
          (row.name ?? "").toLowerCase().includes(needle)),
    );
  }, [report.bens, grupo, term]);

  const total = rows.reduce((sum, row) => sum + Number(row.cost), 0);
  const totalPrevious = rows.reduce((sum, row) => sum + Number(row.cost_previous), 0);

  // Codes only group visually when the list is not already filtered to one.
  const sections = useMemo(() => {
    const buckets = new Map<string, IrpfBem[]>();
    for (const row of rows) {
      const key = `${row.grupo}-${row.codigo}`;
      buckets.set(key, [...(buckets.get(key) ?? []), row]);
    }
    return [...buckets.entries()];
  }, [rows]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Segmented
          size="sm"
          value={grupo}
          onChange={setGrupo}
          options={[
            { value: "todos", label: `Todos (${report.bens.length})` },
            ...grupos.map(([code, count]) => ({ value: code, label: `${code} · ${count}` })),
          ]}
        />
        <label className="relative flex min-w-[180px] flex-1 items-center sm:max-w-xs">
          <Search size={14} className="pointer-events-none absolute left-3 text-ink-muted" />
          <input
            value={term}
            onChange={(event) => setTerm(event.target.value)}
            placeholder="Filtrar por ticker ou nome"
            className="w-full rounded-xl border border-line bg-surface-raised py-2 pl-9 pr-3 text-sm outline-none placeholder:text-ink-muted focus:border-accent"
          />
        </label>
        <CopyButton
          text={bensAsTable(rows, year)}
          what="Bloco"
          label={`Copiar ${rows.length} linha${rows.length === 1 ? "" : "s"}`}
        />
      </div>

      <p className="text-xs text-ink-muted">
        Pelo custo de aquisição, não pelo valor de mercado. Confira o grupo e o código contra o
        layout do ano — a Receita já os renumerou. Somando o que está à vista:{" "}
        <span className="tnum text-ink-secondary">{money(totalPrevious)}</span> em 31/12/{year - 1}{" "}
        e <span className="tnum text-ink-secondary">{money(total)}</span> em 31/12/{year}.
      </p>

      {!rows.length ? (
        <Card className="p-8">
          <p className="text-center text-sm text-ink-muted">Nada com esse filtro.</p>
        </Card>
      ) : (
        sections.map(([key, group]) => {
          const [grupoCode, codigo] = key.split("-");
          return (
            <Card key={key} className="p-5">
              <SectionTitle
                title={`Grupo ${grupoCode} · Código ${codigo}`}
                subtitle={report.groups[grupoCode] ?? "Confira o código no layout do ano"}
              />
              <div className="-mx-2 mt-2 overflow-x-auto">
                <table className="w-full min-w-[700px] text-sm">
                  <thead className="text-left text-xs uppercase tracking-wide text-ink-muted">
                    <tr>
                      <th className="px-2 pb-2">Ativo</th>
                      <th className="hidden px-2 pb-2 lg:table-cell">CNPJ</th>
                      <th className="px-2 pb-2 text-right">Qtd.</th>
                      <th className="px-2 pb-2 text-right">31/12/{year - 1}</th>
                      <th className="px-2 pb-2 text-right">31/12/{year}</th>
                      <th className="px-2 pb-2">Discriminação</th>
                    </tr>
                  </thead>
                  <tbody>
                    {group.map((row) => (
                      <tr key={row.ticker} className="table-row">
                        <td className="px-2 py-1.5">
                          <span className="flex items-center gap-2">
                            <Link
                              to={`/ativos/${row.ticker}`}
                              className="font-medium hover:text-accent"
                            >
                              {row.ticker}
                            </Link>
                            <KindTag kind={row.kind} />
                          </span>
                        </td>
                        <td className="tnum hidden px-2 py-1.5 text-xs text-ink-secondary lg:table-cell">
                          {row.cnpj ?? (row.is_foreign ? "exterior" : "—")}
                        </td>
                        <td className="tnum px-2 py-1.5 text-right text-ink-secondary">
                          {formatQuantity(row.quantity)}
                        </td>
                        <td className="tnum px-2 py-1.5 text-right text-ink-secondary">
                          {money(row.cost_previous)}
                        </td>
                        <td className="tnum px-2 py-1.5 text-right font-medium">
                          {money(row.cost)}
                        </td>
                        {/* `w-full max-w-0` is what makes `truncate` work in a
                            table: a cell sizes to its content otherwise, so the
                            sentence stretched the row and pushed the copy
                            button off the side. Zero max-width lets the cell
                            take the space that is left and no more. */}
                        <td className="w-full max-w-0 px-2 py-1.5">
                          {/* Truncated on purpose: this cell is copied, not
                              read. The full sentence is in the tooltip and in
                              what the button puts on the clipboard. */}
                          <span className="flex items-center gap-1">
                            <span
                              title={row.discriminacao}
                              className="min-w-0 flex-1 truncate text-xs text-ink-secondary"
                            >
                              {row.discriminacao}
                            </span>
                            <CopyButton text={row.discriminacao} what="Discriminação" />
                          </span>
                          {row.accrued_value !== null && (
                            // Quoted, never substituted: the informe decides
                            // whether cost or accrued value goes on the form.
                            <span className="block text-xs text-ink-muted">
                              rendido até 31/12: {money(row.accrued_value)} — confira o informe
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          );
        })
      )}
    </div>
  );
}

function incomeAsTable(rows: IrpfIncomeRow[]): string {
  const header = ["Fonte pagadora", "CNPJ", "Tipo", "Valor", "IRRF", "Líquido"];
  const body = rows.map((row) =>
    [row.ticker, row.cnpj ?? "", row.op_type, row.gross, row.withheld, row.net].join("\t"),
  );
  return [header.join("\t"), ...body].join("\n");
}

function Rendimentos({ report }: { report: IrpfReport }) {
  const [pot, setPot] = useState<Pot>("isentos");
  const rows = report[pot];
  const showTax = pot !== "isentos";
  const meta = POT_TITLES[pot];
  const total = rows.reduce((sum, row) => sum + Number(row.net), 0);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Segmented
          size="sm"
          value={pot}
          onChange={setPot}
          options={(Object.keys(POT_TITLES) as Pot[]).map((value) => ({
            value,
            label: `${POT_TITLES[value].label} (${report[value].length})`,
          }))}
        />
        {rows.length > 0 && (
          <CopyButton text={incomeAsTable(rows)} what="Bloco" label="Copiar bloco" />
        )}
      </div>

      <Card className="p-5">
        <SectionTitle title={meta.title} subtitle={meta.subtitle} />
        {!rows.length ? (
          <p className="py-8 text-center text-sm text-ink-muted">
            Nada a declarar neste bloco em {report.year}.
          </p>
        ) : (
          <div className="-mx-2 mt-2 overflow-x-auto">
            <table className="w-full min-w-[520px] text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-ink-muted">
                <tr>
                  <th className="px-2 pb-2">Fonte pagadora</th>
                  <th className="hidden px-2 pb-2 md:table-cell">CNPJ</th>
                  <th className="px-2 pb-2">Tipo</th>
                  <th className="px-2 pb-2 text-right">Valor</th>
                  {showTax && <th className="px-2 pb-2 text-right">IRRF</th>}
                  {showTax && <th className="px-2 pb-2 text-right">Líquido</th>}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={`${row.ticker}-${row.op_type}`} className="table-row">
                    <td className="px-2 py-1.5">
                      <Link to={`/ativos/${row.ticker}`} className="font-medium hover:text-accent">
                        {row.ticker}
                      </Link>
                      <span className="ml-2 text-xs text-ink-muted">{row.name}</span>
                    </td>
                    <td className="tnum hidden px-2 py-1.5 text-xs text-ink-secondary md:table-cell">
                      {row.cnpj ?? "—"}
                    </td>
                    <td className="px-2 py-1.5 text-ink-secondary">
                      {INCOME_LABELS[row.op_type] ?? row.op_type}
                    </td>
                    <td className="tnum px-2 py-1.5 text-right font-medium">{money(row.gross)}</td>
                    {showTax && (
                      <td className="tnum px-2 py-1.5 text-right text-ink-secondary">
                        {money(row.withheld)}
                      </td>
                    )}
                    {showTax && (
                      <td className="tnum px-2 py-1.5 text-right text-ink-secondary">
                        {money(row.net)}
                      </td>
                    )}
                  </tr>
                ))}
                <tr className="border-t border-line-strong">
                  <td className="px-2 pt-2 font-medium" colSpan={3}>
                    Total
                  </td>
                  <td
                    className="tnum px-2 pt-2 text-right font-medium"
                    colSpan={showTax ? 3 : 1}
                  >
                    {money(total)}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

function Vendas({ report }: { report: IrpfReport }) {
  const { months, result_by_bucket: results, exemption_limit: limit } = report.sales;
  const buckets = useMemo(() => {
    const seen = new Set<string>();
    for (const month of months)
      for (const key of Object.keys(month)) if (key !== "period") seen.add(key);
    for (const key of Object.keys(results)) seen.add(key);
    return BUCKET_ORDER.filter((key) => seen.has(key));
  }, [months, results]);

  if (!buckets.length) {
    return (
      <Card className="p-8">
        <p className="text-center text-sm text-ink-muted">
          Nenhuma alienação em {report.year}.
        </p>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {buckets.map((bucket) => (
          <StatTile
            key={bucket}
            label={BUCKET_LABELS[bucket] ?? bucket}
            value={money(results[bucket] ?? 0)}
            tone={Number(results[bucket] ?? 0) >= 0 ? "positive" : "negative"}
            hint="resultado apurado no ano"
          />
        ))}
      </div>

      <Card className="p-5">
        <SectionTitle
          title="Alienações por mês"
          subtitle="A regra de isenção é diferente em cada coluna — o relatório mostra os valores, a conclusão é sua"
        />
        <div className="-mx-2 mt-2 overflow-x-auto">
          <table className="w-full min-w-[520px] text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-ink-muted">
              <tr>
                <th className="px-2 pb-2">Mês</th>
                {buckets.map((bucket) => (
                  <th key={bucket} className="px-2 pb-2 text-right">
                    {BUCKET_LABELS[bucket] ?? bucket}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {months.map((month) => (
                <tr key={String(month.period)} className="table-row">
                  <td className="px-2 py-1.5">{periodLabel(String(month.period))}</td>
                  {buckets.map((bucket) => {
                    const value = Number(month[bucket] ?? 0);
                    // Only ações à vista have the R$ 20.000 rule; highlighting
                    // a FII month against it would say the opposite of the
                    // truth.
                    const over = bucket === "acoes" && value > Number(limit);
                    return (
                      <td
                        key={bucket}
                        className={clsx(
                          "tnum px-2 py-1.5 text-right",
                          over ? "font-medium text-warning" : "text-ink-secondary",
                        )}
                      >
                        {value ? money(value) : "—"}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-xs text-ink-muted">
          A isenção de {money(limit)} por mês vale para ações negociadas à vista. FII não tem
          isenção, cripto tem limite próprio e o exterior segue a Lei 14.754.
        </p>
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------------- page */

export default function Irpf() {
  const [params, setParams] = useSearchParams();
  const report = useQuery({
    queryKey: ["irpf", params.get("ano")],
    queryFn: () => api.irpf(Number(params.get("ano")) || undefined),
  });
  const data = report.data;

  const setQueryParam = (key: string, value: string) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.set(key, value);
        return next;
      },
      { replace: true },
    );

  const blockParam = (params.get("bloco") ?? "bens") as Block;
  const totals = useMemo(() => {
    if (!data) return null;
    const sum = (rows: { net: number }[]) => rows.reduce((acc, row) => acc + Number(row.net), 0);
    return {
      bens: data.bens.reduce((acc, row) => acc + Number(row.cost), 0),
      isentos: sum(data.isentos),
      exclusiva: sum(data.exclusiva),
      exterior: sum(data.exterior),
    };
  }, [data]);

  if (report.isError) return <ErrorState error={report.error} retry={() => report.refetch()} />;

  const tabs = data
    ? ([
        {
          value: "conferir",
          label: "Conferir",
          icon: AlertTriangle,
          count: data.gaps.length || undefined,
        },
        { value: "bens", label: "Bens e Direitos", icon: Landmark, count: data.bens.length },
        {
          value: "rendimentos",
          label: "Rendimentos",
          icon: Coins,
          count: data.isentos.length + data.exclusiva.length + data.exterior.length,
        },
        { value: "vendas", label: "Vendas", icon: TrendingUp },
      ] as const)
    : [];
  const block = tabs.some((tab) => tab.value === blockParam) ? blockParam : "bens";

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">Análises</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">IRPF</h1>
        <p className="mt-2 max-w-2xl text-sm text-ink-secondary">
          O que declarar de cada bloco, montado a partir das suas movimentações. É uma folha de
          conferência para transcrever — nada aqui é enviado a lugar nenhum.
        </p>
      </header>

      {report.isLoading && !data ? (
        <Skeleton className="h-96 w-full" />
      ) : !data?.year ? (
        <Card className="p-8">
          <p className="text-center text-sm text-ink-muted">
            Ainda não há um ano fechado para declarar. Importe as movimentações e volte no ano
            que vem.
          </p>
        </Card>
      ) : (
        <>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Segmented
              value={String(data.year)}
              onChange={(value) => setQueryParam("ano", value)}
              options={data.years.map((option) => ({
                value: String(option),
                label: String(option),
              }))}
            />
            <p className="text-xs text-ink-muted">
              Posição em {shortDate(data.closing)}, comparada com {shortDate(data.opening)}
            </p>
          </div>

          {/* Above the tabs on purpose: switching block must never cost you the
              figure you were checking against. */}
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile
              label={`Bens em 31/12/${data.year}`}
              value={money(totals?.bens ?? 0)}
              hint="pelo custo de aquisição"
              icon={Landmark}
            />
            <StatTile
              label="Rendimentos isentos"
              value={money(totals?.isentos ?? 0)}
              hint="dividendos e rendimentos de fundos"
              icon={Coins}
              tone="positive"
            />
            <StatTile
              label="Tributação exclusiva"
              value={money(totals?.exclusiva ?? 0)}
              hint="JCP e juros, líquidos de IRRF"
              icon={ClipboardList}
            />
            <StatTile
              label="Pendências"
              value={String(data.gaps.length)}
              hint={data.gaps.length ? "confira antes de transcrever" : "nada a resolver"}
              icon={data.gaps.length ? AlertTriangle : CheckCircle2}
              tone={data.gaps.length ? "negative" : "positive"}
            />
          </div>

          <Tabs
            value={block}
            options={[...tabs]}
            onChange={(next) => setQueryParam("bloco", next)}
          />

          {block === "conferir" ? (
            <Conferir gaps={data.gaps} />
          ) : block === "bens" ? (
            <Bens report={data} />
          ) : block === "rendimentos" ? (
            <Rendimentos report={data} />
          ) : (
            <Vendas report={data} />
          )}
        </>
      )}
    </div>
  );
}
