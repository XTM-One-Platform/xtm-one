// SPDX-License-Identifier: Apache-2.0

/**
 * Provider-contributed detail tabs (integration seam).
 *
 * An integration can add its own sub-page(s) to the `/integrations/:id/:tab?`
 * detail view by registering here, keyed on the provider name. The detail page
 * merges these into its tab bar and renders the active one, so an activated
 * integration owns its admin UI (e.g. Nova's rules) without IntegrationDetail
 * having to know about it.
 *
 * The route, URL persistence and ⌘K registration already come from `DetailTabs`
 * — a contributed tab gets all of that for free. A tab is never mirrored in the
 * left nav: an integration is reached from the Integrations page, whatever
 * pages it owns. Empty by default, so every existing integration
 * behaves exactly as before.
 *
 * A tab's component is loaded lazily (`provider-tab-pages.tsx`): registering
 * one must not pull its page into the entry bundle.
 */
import type { ReactNode } from "react";
import { Boxes, Database, FileCode2, Gauge, KeyRound, ShieldAlert, type LucideIcon } from "lucide-react";
import type { Integration } from "@/types";
import {
  LMStudioModelsTab,
  LMStudioPlatformProviderCard,
  NovaRulesTab,
  OpenAPISpecTab,
  OpenCrqVersionCard,
  OpenRouterKeysTab,
  OpenRouterModelsTab,
  OpenRouterPlatformProviderCard,
  OpenRouterUsageTab,
  SupabaseDatabaseAccessTab,
} from "./provider-tab-pages";
import {
  adminsOnly,
  integrationManagersOnly,
  isRestrictedEntry,
  visibleEntries,
  type ProviderTabViewer,
  type ProviderTabVisibility,
} from "./provider-tab-visibility";

interface ProviderExtraTab {
  /** URL segment + tab id, e.g. "rules" → /integrations/:id/rules. */
  key: string;
  /** Tab label (run through i18n by the detail page, like the built-in tabs). */
  label: string;
  icon: LucideIcon;
  /** Whether the viewer is shown this tab. Omitted =
   *  everyone who can open the integration. The server still enforces its own
   *  rule on every call the tab makes; this only keeps a tab the viewer cannot
   *  use out of sight. */
  visible?: ProviderTabVisibility;
  /** Rendered when this tab is active, inside a Suspense boundary. */
  render: (integration: Integration) => ReactNode;
}

/** provider name → extra detail tabs it contributes. */
export const PROVIDER_EXTRA_TABS: Record<string, ProviderExtraTab[]> = {
  nova: [
    {
      key: "rules",
      label: "Rules",
      icon: ShieldAlert,
      // The rules the guard loads are company-managed, and the sync source and
      // model routes are admin-only: a non-admin would open on a 403.
      visible: adminsOnly,
      render: (integration) => <NovaRulesTab integration={integration} />,
    },
  ],
  // The imported Swagger / OpenAPI document its tools come from.
  openapi: [
    {
      key: "api-spec",
      label: "API Spec",
      icon: FileCode2,
      render: (integration) => <OpenAPISpecTab integration={integration} />,
    },
  ],
  // The LM Studio console: the server's models (loaded or not), loading,
  // unloading and downloading them, and the platform LLM provider it feeds -
  // all through the integration's relay group. For the people who manage it.
  lmstudio: [
    {
      key: "models",
      label: "Models",
      icon: Boxes,
      visible: integrationManagersOnly,
      render: (integration) => <LMStudioModelsTab integration={integration} />,
    },
  ],
  // The OpenRouter console: the account's spend, its catalog and the platform
  // LLM provider it feeds, its API keys. Every route is for the people who
  // manage the integration, so the tabs are too.
  openrouter: [
    {
      key: "usage",
      label: "Usage",
      icon: Gauge,
      visible: integrationManagersOnly,
      render: (integration) => <OpenRouterUsageTab integration={integration} />,
    },
    {
      key: "models",
      label: "Models",
      icon: Boxes,
      visible: integrationManagersOnly,
      render: (integration) => <OpenRouterModelsTab integration={integration} />,
    },
    {
      key: "keys",
      label: "API Keys",
      icon: KeyRound,
      visible: integrationManagersOnly,
      render: (integration) => <OpenRouterKeysTab integration={integration} />,
    },
  ],
  // The Postgres role a Supabase integration's data calls log in as, chosen
  // from what the probe reads, and the functions agents may call. The probe
  // route is for the people who manage the integration, so the tab is too.
  supabase: [
    {
      key: "database",
      label: "Database access",
      icon: Database,
      visible: integrationManagersOnly,
      render: (integration) => <SupabaseDatabaseAccessTab integration={integration} />,
    },
  ],
};

/** The tabs `integration`'s provider contributes that `viewer` is shown. */
export function visibleProviderTabs(
  integration: Integration,
  viewer: ProviderTabViewer,
): ProviderExtraTab[] {
  return visibleEntries(PROVIDER_EXTRA_TABS[integration.provider] ?? [], integration, viewer);
}

/** Whether `tab` is one of the provider's tabs that `viewer` is not shown,
 *  opened from a link: the detail page says so instead of an empty body. */
export function isProviderTabRestricted(
  integration: Integration,
  tab: string,
  viewer: ProviderTabViewer,
): boolean {
  return isRestrictedEntry(PROVIDER_EXTRA_TABS[integration.provider] ?? [], tab, integration, viewer);
}

/** A card an integration adds to its Overview tab, after the connection status. */
interface ProviderOverviewCard {
  key: string;
  /** Whether the viewer is shown the card. Omitted = everyone who can open the integration. */
  visible?: ProviderTabVisibility;
  /** Rendered inside a Suspense boundary; may render nothing. */
  render: (integration: Integration) => ReactNode;
}

/** provider name -> the cards it adds to the Overview tab. */
export const PROVIDER_OVERVIEW_CARDS: Record<string, ProviderOverviewCard[]> = {
  // The OpenCRQ version connected and what it cannot serve. No
  // `visible`: the route answers everyone who can open the integration, and
  // says a version and tool names, nothing else.
  opencrq: [
    {
      key: "version",
      render: (integration) => <OpenCrqVersionCard integration={integration} />,
    },
  ],
  // Whether the platform runs on this LM Studio server, and the way to its Models tab.
  lmstudio: [
    {
      key: "platform-provider",
      visible: integrationManagersOnly,
      render: (integration) => <LMStudioPlatformProviderCard integration={integration} />,
    },
  ],
  // Whether the platform runs on this account, and the way to the Models tab
  // that makes it so: the Overview is where an integration opens. Read from the
  // console overview, so for the same viewers as the console tabs.
  openrouter: [
    {
      key: "platform-provider",
      visible: integrationManagersOnly,
      render: (integration) => <OpenRouterPlatformProviderCard integration={integration} />,
    },
  ],
};

/** The Overview cards `integration`'s provider adds that `viewer` is shown. */
export function visibleProviderOverviewCards(
  integration: Integration,
  viewer: ProviderTabViewer,
): ProviderOverviewCard[] {
  return visibleEntries(PROVIDER_OVERVIEW_CARDS[integration.provider] ?? [], integration, viewer);
}
