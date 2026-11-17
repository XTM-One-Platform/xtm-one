// SPDX-License-Identifier: Apache-2.0

import type { Integration, ProviderCatalogItem } from "@/types";

/**
 * Whether this integration runs on the admin's OAuth app, whose events reach
 * agents through the provider-level webhook the administrator configured.
 * The integration records it when its credentials are set, and the
 * API answers for an older one from its client id; only when that could not
 * be read (null) does it keep the provider-wide answer. `edit` is what the edit form is about to
 * save, read the way `update_integration` records it: a typed client ID is a
 * new app, the user's own since the admin's is never served, and leaving
 * OAuth leaves the admin's app too.
 */
export function usesAdminOAuthApp(
  integration: Pick<Integration, "uses_admin_oauth_credentials" | "auth_type">,
  catalogEntry?: Pick<ProviderCatalogItem, "has_oauth_defaults">,
  edit?: { clientId?: string; authType?: string },
): boolean {
  if (edit?.clientId?.trim()) return false;
  if (integration.auth_type === "oauth" && edit?.authType && edit.authType !== "oauth") return false;
  const recorded = integration.uses_admin_oauth_credentials;
  if (recorded === true || recorded === false) return recorded;
  return catalogEntry?.has_oauth_defaults ?? false;
}

/**
 * Whether an integration created with these values will run on the admin's
 * OAuth app: `create_integration` copies the admin's OAuth defaults into an
 * OAuth integration created without a client ID of its own.
 */
export function createsOnAdminOAuthApp(
  catalogEntry: Pick<ProviderCatalogItem, "has_oauth_defaults">,
  authType: string | undefined,
  clientId?: string,
): boolean {
  return catalogEntry.has_oauth_defaults && authType === "oauth" && !clientId?.trim();
}
