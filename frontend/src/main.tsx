import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "@/App";
import { ToastProvider } from "@/components/Toast";
// Fonts are bundled, not loaded from Google: no render-blocking cross-origin
// stylesheet, no request leaving the machine on every page load, and the
// desktop app keeps its typeface offline. Latin subset only — it covers
// pt-BR accents, the "•" privacy mask and the dashes/ellipsis the UI prints.
import "@fontsource/inter/latin-400.css";
import "@fontsource/inter/latin-500.css";
import "@fontsource/inter/latin-600.css";
import "@fontsource/inter/latin-700.css";
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-500.css";
import "@/styles.css";

// A lazy-loaded chunk fails to fetch when the running page is older than the
// server (a hashed filename from the previous build no longer exists). The
// server now tells caches never to keep index.html, but this is the backstop
// for whatever still slips through — reload once to pick up the current
// build instead of leaving the page broken.
window.addEventListener("unhandledrejection", (event) => {
  const message = String(event.reason?.message ?? "");
  if (!/fetch dynamically imported module|Importing a module script failed/i.test(message)) return;
  if (sessionStorage.getItem("chunk-reload-attempted")) return;
  sessionStorage.setItem("chunk-reload-attempted", "1");
  window.location.reload();
});

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
);
