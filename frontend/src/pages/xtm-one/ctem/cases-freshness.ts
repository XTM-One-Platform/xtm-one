// SPDX-License-Identifier: Apache-2.0

/** The rules of the Cases freshness line (dev-docs/ctem-cases.md s.4.3), kept out of the component. */
import type { CtemCasesFreshness } from "@/hooks/api/useCtemCasesApi";

const PRODUCT_LABEL: Record<string, string> = { opencti: "OpenCTI", openaev: "OpenAEV" };

/** The product keys that did not answer, once each, in a stable order. */
export function missingProductKeys(freshness: CtemCasesFreshness | undefined): string[] {
  return [...new Set(freshness?.missing ?? [])].sort();
}

/** The products named in the outage line, in a stable order, unknown ones as given. */
export function missingProducts(freshness: CtemCasesFreshness | undefined): string[] {
  return missingProductKeys(freshness).map((p) => PRODUCT_LABEL[p] ?? p);
}

/** Whether an outage is newer than the last applied recompute (an older one was already caught up). */
export function outageIsCurrent(freshness: CtemCasesFreshness | undefined): boolean {
  if (!freshness?.missing_at || !freshness.missing?.length) return false;
  if (!freshness.updated_at) return true;
  return Date.parse(freshness.missing_at) >= Date.parse(freshness.updated_at);
}

/**
 * Where the reader fixes the outage: the integration of the one missing product
 * when the reader sees exactly one of that provider, else the integrations list.
 * Never an admin-only route: the Cases page is open to non-admins.
 */
export function outageLink(keys: string[], integrations: { id: string; provider: string }[] | undefined): string {
  if (keys.length !== 1) return "/integrations";
  const matches = (integrations ?? []).filter((i) => i.provider === keys[0]);
  return matches.length === 1 ? `/integrations/${matches[0].id}` : "/integrations";
}
