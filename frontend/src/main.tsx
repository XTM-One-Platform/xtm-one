// SPDX-License-Identifier: Apache-2.0

import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { ToastProvider } from "@/components/ui/toast";
import { restoreDesktopSession } from "@/lib/api";
import { BASE_PATH } from "@/lib/base-path";
import { createAppMutationCache } from "@/lib/mutation-cache";
import { retryQuery, throwQueryError } from "@/lib/query-outage-policy";
import { startPwa } from "@/lib/pwa";
import { REFETCH_INTERVAL_SLOW } from "@/lib/constants";
import { PlatformProvider } from "@/hooks/usePlatformConfig";
import { AppIntlProvider } from "@/i18n";
import App from "./App";
import "./fonts";
import "./index.css";

const queryClient: QueryClient = new QueryClient({
  mutationCache: createAppMutationCache(() => queryClient),
  defaultOptions: {
    queries: {
      // An outage (a restart, a deploy) is read again for about two minutes,
      // any other error once (lib/query-outage-policy.ts).
      retry: retryQuery,
      staleTime: REFETCH_INTERVAL_SLOW,
      // Throw query errors so the ErrorBoundary catches them instead
      // of silently showing empty / skeleton states.
      // - Auth (/me) failure → fullPage ErrorBoundary replaces entire app
      // - Page-level failure → per-page ErrorBoundary replaces that page
      // Except an outage met by a read whose data is on screen: the page
      // stays and catches up once the backend answers (#10910).
      throwOnError: throwQueryError,
    },
  },
});

function renderApp() {
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <QueryClientProvider client={queryClient}>
        {/* The URL path prefix the platform is served under is the
            router's basename, so every route path, <Link> and navigate() in the
            app stays root-relative. */}
        <BrowserRouter basename={BASE_PATH || undefined}>
          <PlatformProvider>
            {/* Toasts inside the translations: they name themselves to screen
                readers. Neither provider above uses a toast. */}
            <AppIntlProvider>
              <ToastProvider>
                <App />
              </ToastProvider>
            </AppIntlProvider>
          </PlatformProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </React.StrictMode>,
  );
}

// Inside XTM One Desktop the session the shell kept is put back before the
// first request reads the token; in a browser this resolves at once.
// Before React: the browser may offer the install prompt right away.
startPwa();
void restoreDesktopSession().then(renderApp);
