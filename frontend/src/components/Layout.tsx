/** Application shell: sidebar, top bar, global search, notifications. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  Radar,
  ArrowUpDown,
  BarChart3,
  Bot,
  Calculator,
  ChevronDown,
  Coins,
  GitCompareArrows,
  Landmark,
  ChartPie,
  FileUp,
  LayoutDashboard,
  Menu,
  MessagesSquare,
  Search,
  Settings as SettingsIcon,
  Users,
  Wallet,
  Wrench,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Suspense, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { api } from "@/lib/api";
import { currencyLabel, dateTime, decimal, kindLabel, money, opLabel, percent, shortDate, toneOf } from "@/lib/format";
import AssetChat, { AI_CHAT_SLOT_ID } from "@/components/AssetChat";
import ErrorBoundary from "@/components/ErrorBoundary";
import Notifications from "@/components/Notifications";
import TitleBar from "@/components/TitleBar";
import { Badge, Skeleton } from "@/components/ui";

/** Query families whose answers move with quotes. New prices must not refetch
    everything else (import history, fundamentals cached for an hour,
    settings…), only what a new price can change. */
const MARKET_QUERY_KEYS = [
  "market-status",
  // A refresh either clears the retry queue or fills it, so the bell is stale
  // as soon as quotes are written.
  "notifications",
  "overview",
  "positions",
  "allocation",
  "history",
  "profit-history",
  "performers",
  "reports",
  "assets",
  "asset",
  "asset-prices",
  "watchlist",
];

type NavLeaf = { to: string; label: string; icon: LucideIcon; end?: boolean };
type NavGroupEntry = { label: string; icon: LucideIcon; children: NavLeaf[] };
type NavEntry = NavLeaf | NavGroupEntry;

/**
 * Ordered by how far the reader is zoomed in, not by when each page was built.
 *
 * The whole portfolio first — how much there is, then how it did — because
 * those are the two questions anyone opens the app to answer. Then what it is
 * made of, from the broadest cut to the narrowest: every asset, the income they
 * pay, and the one class with rules of its own. Then the ledger the three of
 * them are derived from. Tools and the ways data gets in come after all of
 * that: they are things you do, not things you look at.
 */
const NAV: NavEntry[] = [
  // The portfolio as one thing
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { to: "/rentabilidade", label: "Rentabilidade", icon: BarChart3 },
  // What it is made of
  { to: "/ativos", label: "Ativos", icon: Wallet },
  { to: "/proventos", label: "Proventos", icon: Coins },
  { to: "/renda-fixa", label: "Renda fixa", icon: Landmark },
  // Where all of the above comes from
  { to: "/transacoes", label: "Transações", icon: ArrowUpDown },
  // Things you do
  {
    label: "Ferramentas",
    icon: Wrench,
    children: [
      { to: "/comparador", label: "Comparador de ativos", icon: GitCompareArrows },
      { to: "/carteiras", label: "Carteiras públicas", icon: Users },
      { to: "/carteira-ia", label: "Carteira IA", icon: Bot },
      { to: "/universo", label: "Universo de ativos", icon: Radar },
      { to: "/calculadora", label: "Calculadora de juros compostos", icon: Calculator },
    ],
  },
  { to: "/conversas", label: "Conversas IA", icon: MessagesSquare },
  { to: "/importar", label: "Importar", icon: FileUp },
  { to: "/configuracoes", label: "Configurações", icon: SettingsIcon },
];

function SidebarLink({ item, nested = false }: { item: NavLeaf; nested?: boolean }) {
  return (
    <NavLink
      to={item.to}
      end={item.end}
      className={({ isActive }) =>
        clsx(
          "group flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-all duration-200 ease-premium",
          nested && "py-2 pl-4 text-[13px]",
          isActive
            ? "bg-accent-soft text-ink shadow-[inset_0_0_0_1px_rgba(57,135,229,0.25)]"
            : "text-ink-secondary hover:bg-surface-hover hover:text-ink",
        )
      }
    >
      {({ isActive }) => (
        <>
          <item.icon
            size={nested ? 15 : 17}
            strokeWidth={2}
            className={isActive ? "text-accent" : "text-ink-muted"}
            aria-hidden
          />
          {item.label}
        </>
      )}
    </NavLink>
  );
}

/** Collapsible section: opens on click and always when one of its pages is
 *  the current route, so the active highlight can never hide. */
function SidebarGroup({ item }: { item: NavGroupEntry }) {
  const { pathname } = useLocation();
  const childActive = item.children.some((child) => pathname.startsWith(child.to));
  const [open, setOpen] = useState(childActive);
  useEffect(() => {
    if (childActive) setOpen(true);
  }, [childActive]);

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
        className={clsx(
          "flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-all duration-200 ease-premium",
          childActive && !open
            ? "bg-accent-soft text-ink shadow-[inset_0_0_0_1px_rgba(57,135,229,0.25)]"
            : "text-ink-secondary hover:bg-surface-hover hover:text-ink",
        )}
      >
        <item.icon
          size={17}
          strokeWidth={2}
          className={childActive ? "text-accent" : "text-ink-muted"}
          aria-hidden
        />
        {item.label}
        <ChevronDown
          size={15}
          className={clsx("ml-auto text-ink-muted transition-transform duration-200", open && "rotate-180")}
          aria-hidden
        />
      </button>
      {open ? (
        <div className="ml-4 mt-1 space-y-1 border-l border-line pl-2">
          {item.children.map((child) => (
            <SidebarLink key={child.to} item={child} nested />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function SearchDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [term, setTerm] = useState("");
  const navigate = useNavigate();
  const { data, isFetching } = useQuery({
    queryKey: ["search", term],
    queryFn: () => api.search(term),
    enabled: term.trim().length >= 2,
    staleTime: 20_000,
  });

  // The market search leaves the machine (Yahoo), so it runs on a debounced
  // copy of the term — the local results above stay per-keystroke instant.
  const [marketTerm, setMarketTerm] = useState("");
  useEffect(() => {
    const handle = setTimeout(() => setMarketTerm(term.trim()), 300);
    return () => clearTimeout(handle);
  }, [term]);
  const market = useQuery({
    queryKey: ["search-market", marketTerm],
    queryFn: () => api.searchMarket(marketTerm),
    enabled: marketTerm.length >= 2,
    staleTime: 5 * 60_000,
  });
  const localTickers = new Set((data?.assets ?? []).map((asset) => asset.ticker));
  const marketHits = (market.data?.items ?? []).filter((hit) => !localTickers.has(hit.ticker));

  useEffect(() => {
    if (!open) setTerm("");
  }, [open]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 px-4 pt-[12vh] backdrop-blur-sm animate-fade-in">
      <button type="button" className="absolute inset-0 cursor-default" aria-label="Fechar busca" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Busca global"
        className="relative w-full max-w-xl animate-scale-in overflow-hidden rounded-2xl border border-line-strong bg-surface-raised shadow-raised"
      >
        <div className="flex items-center gap-3 border-b border-line px-4 py-3">
          <Search size={18} className="text-ink-muted" aria-hidden />
          <input
            autoFocus
            value={term}
            onChange={(event) => setTerm(event.target.value)}
            placeholder="Buscar ticker, empresa, movimento ou data (AAAA-MM-DD)…"
            className="w-full bg-transparent text-sm text-ink outline-none placeholder:text-ink-muted"
          />
          <kbd className="hidden rounded border border-line px-1.5 py-0.5 text-[10px] text-ink-muted sm:block">ESC</kbd>
        </div>
        <div className="max-h-[52vh] overflow-auto p-2">
          {term.trim().length < 2 ? (
            <p className="px-3 py-6 text-center text-sm text-ink-muted">Digite ao menos 2 caracteres.</p>
          ) : isFetching && !data ? (
            <p className="px-3 py-6 text-center text-sm text-ink-muted">Buscando…</p>
          ) : !data?.assets.length && !data?.transactions.length && !marketHits.length ? (
            <p className="px-3 py-6 text-center text-sm text-ink-muted">
              {market.isFetching ? "Buscando no mercado…" : "Nada encontrado."}
            </p>
          ) : (
            <>
              {data?.assets.length ? (
                <div className="mb-2">
                  <p className="px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">Ativos</p>
                  {data.assets.map((asset) => (
                    <button
                      key={asset.ticker}
                      type="button"
                      onClick={() => {
                        navigate(`/ativos/${asset.ticker}`);
                        onClose();
                      }}
                      className="flex w-full items-center justify-between gap-3 rounded-xl px-3 py-2 text-left transition-colors hover:bg-surface-hover"
                    >
                      <span className="min-w-0">
                        <span className="font-medium text-ink">{asset.ticker}</span>
                        <span className="ml-2 truncate text-xs text-ink-muted">{asset.name}</span>
                      </span>
                      <Badge>{kindLabel(asset.kind)}</Badge>
                    </button>
                  ))}
                </div>
              ) : null}
              {marketHits.length ? (
                <div className="mb-2">
                  <p className="px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                    Mercado (não está na carteira)
                  </p>
                  {marketHits.map((hit) => (
                    <button
                      key={hit.ticker}
                      type="button"
                      onClick={() => {
                        navigate(`/ativos/${hit.ticker}`);
                        onClose();
                      }}
                      className="flex w-full items-center justify-between gap-3 rounded-xl px-3 py-2 text-left transition-colors hover:bg-surface-hover"
                    >
                      <span className="min-w-0">
                        <span className="font-medium text-ink">{hit.ticker}</span>
                        <span className="ml-2 truncate text-xs text-ink-muted">{hit.name}</span>
                      </span>
                      <span className="flex shrink-0 items-center gap-1.5">
                        <span className="text-[11px] text-ink-muted">{hit.exchange}</span>
                        <Badge>{kindLabel(hit.kind)}</Badge>
                      </span>
                    </button>
                  ))}
                </div>
              ) : null}
              {data?.transactions.length ? (
                <div>
                  <p className="px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                    Transações
                  </p>
                  {data.transactions.map((transaction) => (
                    <button
                      key={transaction.id}
                      type="button"
                      onClick={() => {
                        navigate(`/ativos/${transaction.ticker}`);
                        onClose();
                      }}
                      className="flex w-full items-center justify-between gap-3 rounded-xl px-3 py-2 text-left transition-colors hover:bg-surface-hover"
                    >
                      <span className="min-w-0 truncate text-sm text-ink-secondary">
                        <span className="font-medium text-ink">{transaction.ticker}</span> · {opLabel(transaction.op_type)}
                        <span className="ml-2 text-xs text-ink-muted">{shortDate(transaction.date)}</span>
                      </span>
                      <span className="tnum shrink-0 text-sm text-ink-secondary">{money(transaction.gross_amount)}</span>
                    </button>
                  ))}
                </div>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default function Layout() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const location = useLocation();
  const queryClient = useQueryClient();

  const { data: status } = useQuery({
    queryKey: ["market-status"],
    queryFn: api.marketStatus,
    staleTime: 60_000,
    // The sidebar shows live rates; without this they freeze until a reload,
    // since focus refetching is off globally. This poll is also what tells the
    // rest of the app that new prices landed — see below.
    refetchInterval: 5 * 60_000,
  });

  // Prices arriving is a *server* event: the scheduled refresh runs on its own
  // cadence, and the retry queue lands late ones minutes after that. Rather
  // than every screen polling on a guessed timer — or the user pressing a
  // button, which is what this replaces — the one endpoint already being
  // polled reports when the quotes were last written, and a change in that
  // timestamp invalidates exactly the queries a new price can move.
  const lastQuoteAt = status?.last_update ?? null;
  const seenQuoteAt = useRef<string | null>(null);
  useEffect(() => {
    if (!lastQuoteAt) return;
    // The first reading is the baseline, not an update: invalidating here
    // would refetch everything the page had only just loaded.
    if (seenQuoteAt.current === null || seenQuoteAt.current === lastQuoteAt) {
      seenQuoteAt.current = lastQuoteAt;
      return;
    }
    seenQuoteAt.current = lastQuoteAt;
    MARKET_QUERY_KEYS.filter((key) => key !== "market-status").forEach((key) =>
      queryClient.invalidateQueries({ queryKey: [key] }),
    );
  }, [lastQuoteAt, queryClient]);

  useEffect(() => setSidebarOpen(false), [location.pathname]);

  // With the scroll on <main> rather than on the document, PageDown and the
  // arrow keys only reach it once focus is inside it — landing on a fresh page
  // with focus still on <body> would leave the keyboard unable to scroll at
  // all. Focusing the content region on every navigation is also what tells a
  // screen reader that the page under the unchanged chrome has been replaced.
  const contentRef = useRef<HTMLElement>(null);
  useEffect(() => {
    contentRef.current?.focus({ preventScroll: true });
  }, [location.pathname]);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearchOpen(true);
      }
      if (event.key === "Escape") setSearchOpen(false);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  // The shell is exactly as tall as its box and never scrolls: scrolling belongs
  // to <main> alone, so the scrollbar starts below the top bar and stops at the
  // content column instead of running the full height of the window.
  return (
    <div className="flex h-full">
      <TitleBar />
      {/* Sidebar */}
      <aside
        className={clsx(
          "desktop-shell-anchored fixed inset-y-0 left-0 z-40 flex w-[248px] flex-col border-r border-line bg-surface/85 backdrop-blur-xl transition-transform duration-300 ease-premium lg:translate-x-0",
          sidebarOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex h-16 items-center gap-2.5 px-5">
          <span className="grid h-9 w-9 place-items-center rounded-xl bg-accent text-white shadow-glow">
            <ChartPie size={18} strokeWidth={2.2} aria-hidden />
          </span>
          <div className="leading-tight">
            <p className="font-semibold tracking-tight">GumbInvest</p>
            <p className="text-[11px] text-ink-muted">Gestão de carteira</p>
          </div>
          <button
            type="button"
            className="ml-auto text-ink-muted lg:hidden"
            onClick={() => setSidebarOpen(false)}
            aria-label="Fechar menu"
          >
            <X size={18} />
          </button>
        </div>

        <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-4">
          {NAV.map((item) =>
            "children" in item ? (
              <SidebarGroup key={item.label} item={item} />
            ) : (
              <SidebarLink key={item.to} item={item} />
            ),
          )}
        </nav>

        <div className="border-t border-line p-4 text-[11px] text-ink-muted">
          {/* Part of the portfolio is priced in dollars, so the rate every
              converted figure on screen depends on is worth showing next to
              the market-data status rather than buried in settings.

              Currencies only: the same table also stores coin closes, which
              exist so that a trade priced in Bitcoin can reach reais and are
              not something anyone comes to a sidebar to read. Each row is
              named after its own pair — every one of them said "Dólar hoje"
              until the crypto import gave the list more than one entry. */}
          {/* Above the rates: a price, and the day's move alongside it. The
              two blocks are deliberately not merged — one says what a coin is
              worth, the other says what a currency converts at. */}
          {/* Index levels lead: the Ibovespa is the number a Brazilian
              portfolio is read against, and it is points rather than money —
              hence its own row shape, with no currency in front of it. */}
          {(status?.indices ?? []).map((index) => (
            <p key={index.code} className="mb-2 flex items-baseline justify-between gap-2">
              <span className="text-ink-secondary">{index.label}</span>
              <span className="flex items-baseline gap-1.5">
                {index.change_percent === null ? null : (
                  <span
                    className={clsx(
                      "tnum",
                      toneOf(index.change_percent) === "positive" && "text-positive",
                      toneOf(index.change_percent) === "negative" && "text-negative",
                      toneOf(index.change_percent) === "neutral" && "text-ink-muted",
                    )}
                  >
                    {percent(index.change_percent, 2, true)}
                  </span>
                )}
                <span className="tnum font-medium text-ink" title={`Fechamento de ${shortDate(index.date)}`}>
                  {decimal(index.value, 0)}
                </span>
              </span>
            </p>
          ))}
          {(status?.benchmarks ?? [])
            .filter((coin) => coin.price_base !== null)
            .map((coin) => (
              <p key={coin.symbol} className="mb-2 flex items-baseline justify-between gap-2">
                <span className="text-ink-secondary">{coin.name}</span>
                <span className="flex items-baseline gap-1.5">
                  {coin.change_percent === null ? null : (
                    <span
                      className={clsx(
                        "tnum",
                        toneOf(coin.change_percent) === "positive" && "text-positive",
                        toneOf(coin.change_percent) === "negative" && "text-negative",
                        toneOf(coin.change_percent) === "neutral" && "text-ink-muted",
                      )}
                    >
                      {percent(coin.change_percent, 2, true)}
                    </span>
                  )}
                  <span className="tnum font-medium text-ink" title={`Cotação de ${dateTime(coin.fetched_at)}`}>
                    {money(coin.price_base, { decimals: 2 })}
                  </span>
                </span>
              </p>
            ))}
          {(status?.fx ?? [])
            .filter((series) => series.rate && series.is_currency)
            .map((series) => (
              <p key={series.pair} className="mb-2 flex items-baseline justify-between gap-2">
                <span className="text-ink-secondary">{currencyLabel(series.base)}</span>
                <span className="tnum font-medium text-ink" title={`PTAX de ${shortDate(series.end)}`}>
                  {money(series.rate, { decimals: 4 })}
                </span>
              </p>
            ))}
          <p className="flex items-center gap-1.5">
            <span
              className={clsx(
                "h-1.5 w-1.5 rounded-full",
                status?.provider && status.provider !== "none" ? "bg-positive" : "bg-ink-muted",
              )}
              aria-hidden
            />
            Cotações: {status?.provider ?? "-"}
          </p>
          <p className="mt-1">Atualizado {dateTime(status?.last_update ?? null)}</p>
        </div>
      </aside>

      {sidebarOpen ? (
        <button
          type="button"
          aria-label="Fechar menu"
          className="fixed inset-0 z-30 bg-black/50 backdrop-blur-sm lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      ) : null}

      {/* Content */}
      <div className="flex min-w-0 flex-1 flex-col lg:pl-[248px]">
        {/* Not sticky: it is a sibling of the scroll container, so it holds its
            place without any content ever passing under it. */}
        <header className="z-20 flex h-16 shrink-0 items-center gap-3 border-b border-line bg-canvas/80 px-4 backdrop-blur-xl sm:px-6">
          <button
            type="button"
            className="btn-ghost px-2 py-2 lg:hidden"
            onClick={() => setSidebarOpen(true)}
            aria-label="Abrir menu"
          >
            <Menu size={18} />
          </button>

          <button
            type="button"
            onClick={() => setSearchOpen(true)}
            className="flex h-10 flex-1 items-center gap-2.5 rounded-xl border border-line bg-surface-raised px-3 text-sm text-ink-muted transition-colors hover:border-line-strong sm:max-w-sm"
          >
            <Search size={16} aria-hidden />
            <span className="flex-1 text-left">Buscar…</span>
            <kbd className="hidden rounded border border-line px-1.5 py-0.5 text-[10px] sm:block">Ctrl K</kbd>
          </button>

          <div className="ml-auto flex items-center gap-2">
            {/* Filled by whichever AssetChat is mounted — see AI_CHAT_SLOT_ID. */}
            <div id={AI_CHAT_SLOT_ID} className="flex items-center" />
            <Notifications />
          </div>
        </header>

        {/* The one scroll container in the app. `min-h-0` so it may shrink below
            its content inside the column flex; `overflow-x-clip` because naming
            one axis makes the other compute to `auto`, which would let a wide
            table drag the whole shell sideways instead of scrolling in place.

            `tabIndex` -1 so the route effect can hand it the keyboard: it is a
            landmark, not a control, so it stays out of the tab order and shows
            no focus ring.

            pb larger than pt: the last card of a page should not end flush
            against the window edge. */}
        <main
          ref={contentRef}
          tabIndex={-1}
          className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-clip px-4 pb-12 pt-6 focus-visible:ring-0 sm:px-6 lg:px-8"
        >
          {/* Keyed by route so a crash on one page doesn't stick to the next. */}
          <ErrorBoundary key={location.pathname}>
            <Suspense
              fallback={
                <div className="space-y-4">
                  <Skeleton className="h-8 w-64" />
                  <Skeleton className="h-40 w-full" />
                  <Skeleton className="h-64 w-full" />
                </div>
              }
            >
              <Outlet />
            </Suspense>
          </ErrorBoundary>
        </main>
      </div>

      <SearchDialog open={searchOpen} onClose={() => setSearchOpen(false)} />
      {/* Portfolio-wide AI analyst on every page except asset detail, which
          mounts its own asset-scoped panel — only ever one of the two, so they
          never contend for the top-bar slot. */}
      {!/^\/ativos\/./.test(location.pathname) ? <AssetChat ticker={null} /> : null}
    </div>
  );
}
