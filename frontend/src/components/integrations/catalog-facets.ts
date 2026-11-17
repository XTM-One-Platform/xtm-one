/**
 * Catalog facet vocabulary — how a provider entry is described in the
 * catalog sidebar, on its card, and in its details.
 *
 * Its own module so the browser file exports components only (Fast Refresh),
 * and so the details dialog names auth modes exactly as the facets do.
 */

import type { ProviderCatalogItem } from "@/types";

/** How each auth mode is named; every value is a catalog key, read through `t_i18n`. @i18n-keys */
const AUTH_FACET_LABEL_KEYS = {
  oauth: "OAuth account",
  api_key: "API key",
  client_credentials: "OAuth client credentials",
  none: "No credentials",
  platform_managed: "Managed by the platform",
} as const;

export const AUTH_FACET_LABELS: Record<string, string> = AUTH_FACET_LABEL_KEYS;

export const AUTH_FACET_ORDER = ["oauth", "api_key", "client_credentials", "none", "platform_managed"];

/** All auth modes a provider can be connected with (facet values only). */
export function authModesOf(item: ProviderCatalogItem): string[] {
  const modes = new Set(item.supported_auth_types ?? []);
  modes.add(item.auth_type);
  return [...modes].filter((m) => m in AUTH_FACET_LABELS);
}

export function isReadyToConnect(item: ProviderCatalogItem): boolean {
  return item.has_oauth_defaults && !item.is_disabled;
}
