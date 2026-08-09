import { useQuery } from "@tanstack/react-query";
import { lazy, useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import Layout from "@/components/Layout";
import { api } from "@/lib/api";
import { configureFormatting } from "@/lib/format";

// Lazy routes: each page (and Recharts, which only pages import) stays out of
// the entry bundle, so the shell paints before chart code even downloads.
const AiChats = lazy(() => import("@/pages/AiChats"));
const AporteInteligente = lazy(() => import("@/pages/AporteInteligente"));
const AssetDetail = lazy(() => import("@/pages/AssetDetail"));
const Assets = lazy(() => import("@/pages/Assets"));
const Calculadora = lazy(() => import("@/pages/Calculadora"));
const CarteiraIA = lazy(() => import("@/pages/CarteiraIA"));
const CarteirasPublicas = lazy(() => import("@/pages/CarteirasPublicas"));
const Comparador = lazy(() => import("@/pages/Comparador"));
const Dashboard = lazy(() => import("@/pages/Dashboard"));
const Dividends = lazy(() => import("@/pages/Dividends"));
const FixedIncome = lazy(() => import("@/pages/FixedIncome"));
const ImportPage = lazy(() => import("@/pages/ImportPage"));
const Irpf = lazy(() => import("@/pages/Irpf"));
const Reports = lazy(() => import("@/pages/Reports"));
const Settings = lazy(() => import("@/pages/Settings"));
const Universo = lazy(() => import("@/pages/Universo"));
const Transactions = lazy(() => import("@/pages/Transactions"));

export default function App() {
  // Apply the stored locale/currency/theme once, then let pages render.
  const { data } = useQuery({ queryKey: ["settings"], queryFn: api.settings, staleTime: 5 * 60_000 });

  useEffect(() => {
    if (!data) return;
    configureFormatting(String(data.values.number_format ?? "pt-BR"), String(data.values.currency ?? "BRL"));
    document.documentElement.dataset.theme = String(data.values.theme ?? "dark");
  }, [data]);

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="ativos" element={<Assets />} />
        <Route path="ativos/:ticker" element={<AssetDetail />} />
        <Route path="proventos" element={<Dividends />} />
        <Route path="renda-fixa" element={<FixedIncome />} />
        <Route path="transacoes" element={<Transactions />} />
        <Route path="rentabilidade" element={<Reports />} />
        <Route path="irpf" element={<Irpf />} />
        {/* The page was /relatorios until 2026-08; old bookmarks keep working. */}
        <Route path="relatorios" element={<Navigate to="/rentabilidade" replace />} />
        <Route path="calculadora" element={<Calculadora />} />
        <Route path="comparador" element={<Comparador />} />
        <Route path="carteiras" element={<CarteirasPublicas />} />
        <Route path="carteira-ia" element={<CarteiraIA />} />
        <Route path="aporte" element={<AporteInteligente />} />
        <Route path="conversas" element={<AiChats />} />
        <Route path="importar" element={<ImportPage />} />
        <Route path="configuracoes" element={<Settings />} />
        <Route path="universo" element={<Universo />} />
          <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
