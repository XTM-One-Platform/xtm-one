/**
 * The providers `main.tsx` mounts around the app, for component tests
 * (`*.test.tsx`, vitest + happy-dom): a QueryClient that never retries, a
 * catalog (English by default: its ids are the messages) and the toaster.
 *
 * A test that reads another language passes `locale`:
 * `renderWithProviders(ui, { locale: "fr" })` mounts the `src/i18n/lang/`
 * catalog of that language, so no test builds its own provider stack.
 */

import type { ReactElement, ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { IntlProvider } from "react-intl";
import { render } from "@testing-library/react";
import { ToastProvider } from "@/components/ui/toast";
import { createAppMutationCache } from "@/lib/mutation-cache";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";

/** The catalogs a test can mount; English renders the message ids. */
const CATALOGS: Record<string, Record<string, string>> = { en: {}, es, fr, ja };

/**
 * `appMutationCache` mounts the app's MutationCache (`lib/mutation-cache.ts`): its
 * error toast shows beside the component's own, for a test that counts them.
 */
export function testQueryClient({ appMutationCache = false }: { appMutationCache?: boolean } = {}): QueryClient {
  const client: QueryClient = new QueryClient({
    ...(appMutationCache ? { mutationCache: createAppMutationCache(() => client) } : {}),
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
  return client;
}

/** The catalog of a locale ("fr", "fr-fr"), by its primary subtag. */
function catalogFor(locale: string): Record<string, string> {
  const catalog = CATALOGS[locale.toLowerCase().split("-")[0]];
  if (!catalog) throw new Error(`No catalog for locale "${locale}" in src/i18n/lang/`);
  return catalog;
}

export function withProviders(client: QueryClient, locale = "en") {
  const messages = catalogFor(locale);
  return function Providers({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <IntlProvider locale={locale} messages={messages} onError={() => {}}>
          <ToastProvider>{children}</ToastProvider>
        </IntlProvider>
      </QueryClientProvider>
    );
  };
}

export interface RenderWithProvidersOptions {
  client?: QueryClient;
  /** A language of `src/i18n/lang/` ("fr", "es", "ja", or a region form such as "fr-fr"). */
  locale?: string;
}

export function renderWithProviders(
  ui: ReactElement,
  clientOrOptions: QueryClient | RenderWithProvidersOptions = {},
) {
  const options = clientOrOptions instanceof QueryClient ? { client: clientOrOptions } : clientOrOptions;
  const client = options.client ?? testQueryClient();
  return { client, ...render(ui, { wrapper: withProviders(client, options.locale) }) };
}
