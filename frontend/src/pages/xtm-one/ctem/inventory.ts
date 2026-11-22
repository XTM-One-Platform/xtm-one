// SPDX-License-Identifier: Apache-2.0

/**
 * Shared words of CTEM > Inventory (#1178). The page title and the menu label are one constant (Sam, 22 Nov 2026:
 * "Inventory"), so a rename is one line.
 */

/** The page title and the CTEM menu row (English, translated where drawn). */
export const INVENTORY_LABEL = "Inventory";

/** The page's route; `/ctem/assets` redirects to it. */
export const INVENTORY_PATH = "/ctem/inventory";

/** The source names a record carries, as people read them; an unknown source keeps its own name. */
const SOURCE_LABELS: Record<string, string> = {
  manual: "Manual",
  csv: "CSV import",
  api: "API",
  sbom: "SBOM",
  opencti: "OpenCTI",
  openaev: "OpenAEV",
  opencrq: "OpenCRQ",
};

export function sourceLabel(source: string): string {
  if (source.startsWith("agent:")) return "Agent";
  return SOURCE_LABELS[source] ?? source;
}

/**
 * The category an asset shows: its subcategory when it has one, else its category. The API answers "" for no
 * subcategory, so `??` would keep the empty string (UX round 1 on #1254, B1).
 */
export function assetCategory(asset: { category?: string | null; subcategory?: string | null }): string {
  return asset.subcategory || asset.category || "";
}

/** A stored enum value (`web_application`, `very_high`) as a phrase; empty for a missing value. */
export function assetLabel(value: string | null | undefined): string {
  if (!value) return "";
  const words = value.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}
