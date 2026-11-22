// SPDX-License-Identifier: Apache-2.0

/**
 * Shared words of CTEM > Inventory (#1178). The page title and the menu label are one constant (Sam, 22 Nov 2026:
 * "Inventory"), so a rename is one line.
 */
import { ASSET_CRITICALITY_LABELS, taxonomyValueLabel, typeLabel } from "@/lib/asset-taxonomy";
import { ENVIRONMENT_LABELS } from "@/lib/organization-import-filters";

type Translate = (text: string) => string;

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

/** Whether an asset is reachable from the internet, in the column's words. @i18n-keys */
const INTERNET_FACING_LABELS = { yes: "Yes", no: "No", unknown: "Unknown" } as const;

/** How the tech stack holds a product (`installed`, `saas`, `cloud_service`). @i18n-keys */
const TECH_KIND_LABELS = { installed: "Installed software", saas: "SaaS application", cloud_service: "Cloud service" } as const;

const FIELD_LABELS: Record<string, Record<string, string>> = {
  criticality: ASSET_CRITICALITY_LABELS,
  internet_facing: INTERNET_FACING_LABELS,
  environment: ENVIRONMENT_LABELS,
  kind: TECH_KIND_LABELS,
};

/**
 * The translated words of one stored value of an inventory field (`host`, `very_high`, `yes`, `production`), the
 * same labels the profile's edit cards use (UX round 2 on #1254, B6). A value outside the known set is shown as
 * stored, never re-cased into a phrase no catalog holds.
 */
export function assetFieldLabel(t: Translate, field: string, value: string | null | undefined): string {
  if (!value) return "";
  if (field === "category" || field === "subcategory") return taxonomyValueLabel(t, value);
  const label = FIELD_LABELS[field]?.[value.toLowerCase()];
  return label ? t(label) : value;
}

/** An asset's category in words: "Category > Subcategory", or the category alone. */
export function assetCategoryLabel(t: Translate, asset: { category?: string | null; subcategory?: string | null }): string {
  if (!asset.category) return asset.subcategory ? taxonomyValueLabel(t, asset.subcategory) : "";
  return typeLabel(t, asset.category.toLowerCase(), (asset.subcategory ?? "").toLowerCase());
}
