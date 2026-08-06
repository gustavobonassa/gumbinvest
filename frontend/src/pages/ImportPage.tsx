/** Upload (B3 CSV or broker statement PDF), coverage map and import log. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { CalendarCheck, CheckCircle2, CloudUpload, FileText, History, Loader2, XCircle } from "lucide-react";
import { useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import StatementCoverage from "@/components/StatementCoverage";
import { useToast } from "@/components/Toast";
import { Badge, Card, EmptyState, ErrorState, Pager, SectionTitle, Skeleton, Tabs } from "@/components/ui";
import { api, type ImportBatch } from "@/lib/api";
import { dateTime, opLabel, shortDate } from "@/lib/format";

const PAGE_SIZE = 5;

const TABS = [
  { value: "enviar", label: "Enviar arquivos", icon: CloudUpload },
  { value: "cobertura", label: "Cobertura dos extratos", icon: CalendarCheck },
  { value: "historico", label: "Histórico", icon: History },
] as const;

type TabValue = (typeof TABS)[number]["value"];

/**
 * The accepted formats, as a list rather than a paragraph.
 *
 * Each entry says where in the broker's own interface the file comes from —
 * that, not the extension, is the part people get wrong.
 */
const SOURCES = [
  {
    title: "B3 — Área do Investidor",
    detail: "Extrato de movimentações em .csv ou .xlsx, tanto faz. Até 64 MB.",
  },
  {
    title: "Avenue e Nomad",
    detail: "Extratos mensais em PDF, um arquivo por mês e por conta. Até 32 MB cada.",
  },
  {
    title: "Binance",
    detail:
      "Carteira → Histórico de transações, que cobre depósitos, saques, Earn e futuros. Prefira-o ao histórico spot, que só traz negociações.",
  },
  {
    title: "Backup .gumbinvest",
    detail: "Exportação completa de outra instalação, com carteiras, movimentações e ajustes.",
  },
];

function ImportSummary({ batch }: { batch: Pick<ImportBatch, "summary"> }) {
  const operations = Object.entries(batch.summary?.operations ?? {}).sort((a, b) => b[1] - a[1]);
  if (!operations.length) return null;
  return (
    <div className="mt-3 flex flex-wrap gap-1.5">
      {operations.map(([type, count]) => (
        <Badge key={type}>
          {opLabel(type)} · {count}
        </Badge>
      ))}
    </div>
  );
}

/**
 * What a statement import deliberately left out.
 *
 * Every one of these is a decision the importer made on the user's behalf, so
 * each is reported rather than left implicit: money moving in and out of the
 * broker's cash account, an event the same broker already reported in another
 * report, and rows the statement gave no security for.
 */
function SkippedSummary({ batch }: { batch: Pick<ImportBatch, "summary"> }) {
  const skipped = batch.summary?.skipped;
  if (!skipped) return null;
  const items = [
    skipped.cash_movements ? `${skipped.cash_movements} movimentos de caixa (depósitos, saques, sweeps)` : null,
    skipped.cancelled_orders ? `${skipped.cancelled_orders} ordens não executadas` : null,
    skipped.cross_source_duplicates
      ? `${skipped.cross_source_duplicates} já presentes em outro relatório do mesmo período`
      : null,
    skipped.unattributed
      ? `${skipped.unattributed} linhas sem ativo identificado no extrato (${skipped.unattributed_amount})`
      : null,
    skipped.provisional_tickers ? `${skipped.provisional_tickers} com código provisório` : null,
    skipped.unconverted_fees ? `${skipped.unconverted_fees} taxas em moeda diferente da operação` : null,
  ].filter(Boolean);
  if (!items.length) return null;
  return <p className="mt-3 text-sm text-ink-muted">Ignorado: {items.join(" · ")}.</p>;
}

/**
 * What an exchange export covers — and what it leaves out.
 *
 * Worth saying out loud rather than leaving in the log: a *spot* export lists
 * trades and only trades, so a coin that arrived by depósito, Convert, Earn or
 * cartão has no purchase on file and shows up as sold from nowhere. Reading the
 * numbers without knowing that is the difference between "I lost money" and
 * "half the history is missing". A full transaction history has none of that
 * problem, so its note says the opposite and is not styled as a warning.
 */
function ExchangeSummary({ batch }: { batch: Pick<ImportBatch, "summary"> }) {
  const exchange = batch.summary?.exchange;
  if (!exchange) return null;
  const events = Object.entries(exchange.events ?? {});
  const complete = events.length > 0;
  return (
    <div className="mt-3 space-y-1 text-sm">
      <p className="text-ink-secondary">
        {exchange.name} · {exchange.trades} negociações em {exchange.pairs.length} pares
        {complete ? ` · ${events.reduce((sum, [, n]) => sum + n, 0)} movimentos não negociais` : ""}
      </p>
      <p className={complete ? "text-ink-muted" : "text-warning"}>{exchange.note}.</p>
      {complete ? (
        <div className="flex flex-wrap gap-1.5 pt-1">
          {events
            .sort((a, b) => b[1] - a[1])
            .map(([type, count]) => (
              <Badge key={type}>
                {opLabel(type)} · {count}
              </Badge>
            ))}
        </div>
      ) : null}
      {batch.summary?.warnings?.map((warning) => (
        <p key={warning.message} className="text-ink-muted">
          {warning.message}.
        </p>
      ))}
      {exchange.native_currency.length ? (
        <p className="text-ink-muted">
          Registrados na moeda do par (sem cotação em dólar no arquivo):{" "}
          {exchange.native_currency.map((item) => `${item.currency} (${item.count})`).join(", ")}
        </p>
      ) : null}
    </div>
  );
}

export default function ImportPage() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  // The tab lives in the URL so a link can point straight at the coverage map
  // or at the log, and a reload keeps the reader where they were.
  const [params, setParams] = useSearchParams();
  const requested = params.get("aba");
  const tab: TabValue = TABS.some((option) => option.value === requested)
    ? (requested as TabValue)
    : "enviar";
  const setTab = (next: TabValue) =>
    setParams(next === "enviar" ? {} : { aba: next }, { replace: true });

  const [page, setPage] = useState(1);
  const history = useQuery({ queryKey: ["imports", page], queryFn: () => api.imports(page, PAGE_SIZE) });
  // Same query key as StatementCoverage's, so this only feeds the tab counter.
  const coverage = useQuery({ queryKey: ["import-coverage"], queryFn: api.coverage });
  const upload = useMutation({
    mutationFn: (file: File) => api.upload(file),
    // One toast per file: a batch of statements only leaves the last result in
    // the panel below, so without this the earlier files land silently.
    onSuccess: (result, file) => {
      setPage(1); // the new batch is at the top of the log
      queryClient.invalidateQueries();
      toast.success(`${file.name} importado.`, {
        description: result.rows_imported
          ? `${result.rows_imported} de ${result.rows_total} linhas novas.`
          : "Nenhuma linha nova — tudo já estava importado.",
      });
    },
    onError: (error, file) => toast.error(`Falha ao importar ${file.name}.`, error),
  });

  // Uploads run one at a time and in name order: when two statements describe
  // the same month, whichever lands first sets the amounts, so a predictable
  // order keeps repeated imports reproducible.
  const handleFiles = async (files: FileList | null) => {
    const list = Array.from(files ?? []).sort((a, b) => a.name.localeCompare(b.name));
    for (const file of list) {
      try {
        await upload.mutateAsync(file);
      } catch {
        /* the error is surfaced by the mutation state; keep going */
      }
    }
  };

  return (
    <div className="space-y-6">
      <header className="animate-fade-up">
        <p className="text-sm text-ink-muted">Dados</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Importar movimentações</h1>
        <p className="mt-2 max-w-2xl text-sm text-ink-secondary">
          Movimentos já importados são ignorados automaticamente — pode reenviar o mesmo arquivo, subir vários de
          uma vez, ou enviar dois relatórios diferentes do mesmo mês sem duplicar nada.
        </p>
      </header>

      <Tabs
        value={tab}
        onChange={setTab}
        options={TABS.map((option) =>
          // A zero would read as a broken counter on a fresh install; the tab's
          // own empty state says it better.
          option.value === "cobertura"
            ? { ...option, count: coverage.data?.accounts.length || undefined }
            : option.value === "historico"
              ? { ...option, count: history.data?.total || undefined }
              : { ...option },
        )}
      />

      {tab === "enviar" ? (
        <>
          <Card
            className={clsx(
              "border-2 border-dashed p-10 text-center transition-all duration-300 ease-premium",
              dragging ? "border-accent bg-accent-soft" : "border-line",
            )}
            hover={false}
          >
            <div
              onDragOver={(event) => {
                event.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(event) => {
                event.preventDefault();
                setDragging(false);
                handleFiles(event.dataTransfer.files);
              }}
              className="flex flex-col items-center gap-3"
            >
              <span className="grid h-14 w-14 place-items-center rounded-2xl bg-surface-raised text-accent">
                {upload.isPending ? <Loader2 size={24} className="animate-spin" /> : <CloudUpload size={24} />}
              </span>
              <div>
                <p className="font-medium">
                  {upload.isPending ? "Processando arquivo…" : "Arraste os arquivos aqui ou clique para selecionar"}
                </p>
                <p className="mt-1 text-sm text-ink-muted">
                  CSV ou XLSX da B3, extratos PDF da Avenue e da Nomad, exportações da Binance ou um backup
                  .gumbinvest
                </p>
              </div>
              <button
                type="button"
                className="btn-primary"
                onClick={() => inputRef.current?.click()}
                disabled={upload.isPending}
              >
                Selecionar arquivo
              </button>
              <input
                ref={inputRef}
                type="file"
                accept=".csv,text/csv,.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,.pdf,application/pdf,.gumbinvest,application/gzip"
                multiple
                className="hidden"
                onChange={(event) => handleFiles(event.target.files)}
              />
            </div>
          </Card>

          {upload.isError ? (
            <Card className="flex items-start gap-3 border-negative/30 bg-negative/5 p-4" hover={false}>
              <XCircle size={18} className="mt-0.5 shrink-0 text-negative" />
              <div>
                <p className="font-medium text-negative">Falha na importação</p>
                <p className="mt-1 text-sm text-ink-secondary">
                  {upload.error instanceof Error ? upload.error.message : "Erro desconhecido"}
                </p>
              </div>
            </Card>
          ) : null}

          {upload.isSuccess && upload.data ? (
            <Card className="border-positive/30 bg-positive/5 p-5" hover={false}>
              <div className="flex items-start gap-3">
                <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-positive" />
                <div className="min-w-0 flex-1">
                  <p className="font-medium text-ink">Importação concluída</p>
                  <div className="mt-2 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
                    <div>
                      <p className="text-ink-muted">Linhas lidas</p>
                      <p className="tnum text-lg font-semibold">{upload.data.rows_total}</p>
                    </div>
                    <div>
                      <p className="text-ink-muted">Novas</p>
                      <p className="tnum text-lg font-semibold text-positive">{upload.data.rows_imported}</p>
                    </div>
                    <div>
                      <p className="text-ink-muted">Duplicadas</p>
                      <p className="tnum text-lg font-semibold text-ink-secondary">{upload.data.rows_duplicate}</p>
                    </div>
                    <div>
                      <p className="text-ink-muted">Com erro</p>
                      <p
                        className={clsx(
                          "tnum text-lg font-semibold",
                          upload.data.rows_failed ? "text-negative" : "text-ink-secondary",
                        )}
                      >
                        {upload.data.rows_failed}
                      </p>
                    </div>
                  </div>
                  <ImportSummary batch={upload.data as unknown as ImportBatch} />
                  <ExchangeSummary batch={upload.data as unknown as ImportBatch} />
                  <SkippedSummary batch={upload.data as unknown as ImportBatch} />
                  {upload.data.summary?.unknown_movements?.length ? (
                    <p className="mt-3 text-sm text-warning">
                      Movimentos não mapeados (registrados mas não aplicados):{" "}
                      {upload.data.summary.unknown_movements
                        .map((item) => `${item.movement} (${item.count})`)
                        .join(", ")}
                    </p>
                  ) : null}
                </div>
              </div>
            </Card>
          ) : null}

          <Card className="p-5">
            <SectionTitle
              title="Arquivos aceitos"
              subtitle="Onde encontrar cada um na interface da corretora"
            />
            <ul className="grid gap-4 sm:grid-cols-2">
              {SOURCES.map((source) => (
                <li key={source.title} className="flex gap-3">
                  <FileText size={15} className="mt-0.5 shrink-0 text-ink-muted" aria-hidden />
                  <div className="min-w-0">
                    <p className="text-sm font-medium">{source.title}</p>
                    <p className="mt-0.5 text-sm text-ink-secondary">{source.detail}</p>
                  </div>
                </li>
              ))}
            </ul>
          </Card>
        </>
      ) : null}

      {tab === "cobertura" ? <StatementCoverage /> : null}

      {tab === "historico" ? (
        <Card className="p-5">
          <SectionTitle
            title="Histórico de importações"
            subtitle="Registro completo, com contagens e erros por arquivo"
            action={
              history.data?.total ? (
                <span className="tnum text-sm text-ink-muted">{history.data.total} importações</span>
              ) : null
            }
          />
          {/* An error is not an empty log — "nenhuma importação" after a
              failed fetch would tell the owner their history vanished. */}
          {history.isError ? (
            <ErrorState error={history.error} retry={() => history.refetch()} />
          ) : history.isLoading ? (
            <div className="space-y-3">
              {Array.from({ length: 4 }).map((_, index) => (
                <Skeleton key={index} className="h-20 w-full" />
              ))}
            </div>
          ) : !history.data?.items.length ? (
            <EmptyState icon={FileText} title="Nenhuma importação ainda" />
          ) : (
            <>
              <ul className="space-y-3">
                {history.data.items.map((batch) => (
                  <li key={batch.id} className="rounded-xl border border-line bg-surface-raised/50 p-4">
                    <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                      {/* Statement PDFs carry the broker's own long filenames;
                          the name truncates so it can never push the status
                          and the date off the card. */}
                      <span className="flex min-w-0 flex-1 items-center gap-2 font-medium">
                        <FileText size={15} className="shrink-0 text-ink-muted" />
                        <span className="truncate" title={batch.filename}>
                          {batch.filename}
                        </span>
                        <Badge
                          className="shrink-0"
                          tone={
                            batch.status === "COMPLETED"
                              ? "positive"
                              : batch.status === "FAILED"
                                ? "negative"
                                : "neutral"
                          }
                        >
                          {batch.status === "COMPLETED"
                            ? "concluída"
                            : batch.status === "FAILED"
                              ? "falhou"
                              : batch.status === "PENDING"
                                ? "processando"
                                : batch.status.toLowerCase()}
                        </Badge>
                      </span>
                      <span className="shrink-0 text-xs text-ink-muted">{dateTime(batch.created_at)}</span>
                    </div>
                    <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-sm text-ink-secondary">
                      <span className="tnum">{batch.rows_total} linhas</span>
                      <span className="tnum text-positive">{batch.rows_imported} novas</span>
                      <span className="tnum text-ink-muted">{batch.rows_duplicate} duplicadas</span>
                      {batch.rows_failed ? (
                        <span className="tnum text-negative">{batch.rows_failed} com erro</span>
                      ) : null}
                      {batch.summary?.date_range?.start ? (
                        <span className="text-ink-muted">
                          {shortDate(batch.summary.date_range.start)} →{" "}
                          {shortDate(batch.summary.date_range.end ?? null)}
                        </span>
                      ) : null}
                    </div>
                    <ImportSummary batch={batch} />
                  </li>
                ))}
              </ul>
              <div className="mt-4">
                <Pager
                  page={history.data.page}
                  pages={history.data.pages}
                  total={history.data.total}
                  pageSize={history.data.page_size}
                  onChange={setPage}
                  noun="importações"
                />
              </div>
            </>
          )}
        </Card>
      ) : null}
    </div>
  );
}
