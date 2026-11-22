// SPDX-License-Identifier: Apache-2.0

/** The rules of the Cases freshness line (dev-docs/ctem-cases.md s.4.3), kept out of the component. */
import type { CtemCasesFreshness } from "@/hooks/api/useCtemCasesApi";

const PRODUCT_LABEL: Record<string, string> = { opencti: "OpenCTI", openaev: "OpenAEV" };

/** The products named in the outage line, in a stable order, unknown ones as given. */
export function missingProducts(freshness: CtemCasesFreshness | undefined): string[] {
  return [...new Set(freshness?.missing ?? [])].sort().map((p) => PRODUCT_LABEL[p] ?? p);
}

/** Whether an outage is newer than the last applied recompute (an older one was already caught up). */
export function outageIsCurrent(freshness: CtemCasesFreshness | undefined): boolean {
  if (!freshness?.missing_at || !freshness.missing?.length) return false;
  if (!freshness.updated_at) return true;
  return Date.parse(freshness.missing_at) >= Date.parse(freshness.updated_at);
}
