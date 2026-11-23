// SPDX-License-Identifier: Apache-2.0

/**
 * Shared words of CTEM > Inventory (#1178). The page title and the menu label are one constant (Sam, 22 Nov 2026:
 * "Inventory"), so a rename is one line.
 */
import { ASSET_CRITICALITY_LABELS, taxonomyValueLabel, typeLabel } from "@/lib/asset-taxonomy";
import { ENVIRONMENT_LABELS } from "@/lib/organization-import-filters";
import { IMPORT_SOURCE_NAMES } from "@/lib/organization-profile";
import { SOURCE_KIND_LABELS } from "@/lib/organization-provenance";

type Translate = (text: string) => string;

/** The page title and the CTEM menu row (English, translated where drawn). */
export const INVENTORY_LABEL = "Inventory";

/** The page's route. */
export const INVENTORY_PATH = "/ctem/inventory";

/** A code repository asset (GitHub or GitLab source, #1331): its software list is its dependencies. */
export function isCodeRepository(asset: { category: string | null; subcategory: string | null } | undefined): boolean {
  return asset?.category === "source_code" && asset.subcategory === "code_repository";
}

/** The names of the sources only the Inventory lists (SBOM and the platforms). @i18n-keys */
const INVENTORY_SOURCE_LABELS = {
  sbom: "SBOM",
  opencti: "OpenCTI",
  openaev: "OpenAEV",
  opencrq: "OpenCRQ",
} as const;

/**
 * The source names a record carries, as people read them; an unknown source keeps its own name. The hand, CSV and API
 * names are the shared SOURCE_KIND_LABELS, so the Sources tab, its chips and the list they land on say the same word
 * (UX round 6 on #1254, B12: "Entered by hand" on the chip, "Manual" on the landed filter).
 */
const SOURCE_LABELS: Record<string, string> = {
  manual: SOURCE_KIND_LABELS.manual,
  csv: SOURCE_KIND_LABELS.csv,
  api: SOURCE_KIND_LABELS.api,
  ...INVENTORY_SOURCE_LABELS,
};

/**
 * Product names, never translated: the endpoint sources read through their Microsoft integrations (`intune`,
 * `defender`, #1262) read as the Sources and syncs table names them (review of #1254, P3).
 */
const PRODUCT_SOURCE_NAMES: Record<string, string> = { intune: IMPORT_SOURCE_NAMES.intune, defender: IMPORT_SOURCE_NAMES.defender };

/** The prefix of a source an agent wrote (`agent:<name>`), as the backend's AGENT_SOURCE_PREFIX. */
const AGENT_SOURCE_PREFIX = "agent:";

/**
 * The translated name of a record's source (`csv` reads "Import CSV" in French; UX round 3 on #1254, B10). An agent
 * source reads with the agent's own name, the name the Sources tab shows (the backend's display_name of
 * `agent:<name>`), so two agents never share one label (UX round 7 on #1254, B13); a bare `agent:` reads "Agent". A
 * source outside the known set is shown as stored.
 */
export function sourceLabel(t: Translate, source: string): string {
  if (source.startsWith(AGENT_SOURCE_PREFIX)) return source.slice(AGENT_SOURCE_PREFIX.length).trim() || t(SOURCE_KIND_LABELS.agent);
  if (PRODUCT_SOURCE_NAMES[source]) return PRODUCT_SOURCE_NAMES[source];
  const label = SOURCE_LABELS[source];
  return label ? t(label) : source;
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

/** The type of a software (organization_software_store.SOFTWARE_TYPES, #1495). @i18n-keys */
export const SOFTWARE_TYPE_LABELS = {
  application: "Application",
  operating_system: "Operating system",
  library: "Library",
  firmware: "Firmware",
  container_image: "Container image",
  saas: "SaaS application",
} as const;

/** How sure a software's CPE is (organization_asset_identity.CPE_CONFIDENCES). @i18n-keys */
export const CPE_STATE_LABELS = {
  override: "Set by hand",
  reported: "Reported by a source",
  high: "High confidence",
  medium: "Medium confidence",
  low: "Low confidence",
  unresolved: "Not resolved",
} as const;

/** Who owns a software: a source (read-only) or an administrator. @i18n-keys */
export const SOFTWARE_ORIGIN_LABELS = { synced: "Synced", manual: "Entered by hand" } as const;

const FIELD_LABELS: Record<string, Record<string, string>> = {
  criticality: ASSET_CRITICALITY_LABELS,
  internet_facing: INTERNET_FACING_LABELS,
  environment: ENVIRONMENT_LABELS,
  software_type: SOFTWARE_TYPE_LABELS,
  cpe_state: CPE_STATE_LABELS,
  origin: SOFTWARE_ORIGIN_LABELS,
};

/** A software's page. */
export function softwarePath(id: string): string {
  return `${INVENTORY_PATH}/software/${id}`;
}

/**
 * The version of the software a page shows that one dependency edge was reported for, or `unknown`. The API puts
 * that version in `from_version_id` in both directions (Depends on and Used by); `version_id` is always the other
 * software's version (UX round 3 on #1549, B1: Used by read `version_id` and always showed Unknown).
 */
export function edgeOwnVersion(edge: { from_version_id: string }, versionOf: ReadonlyMap<string, string>): string {
  return versionOf.get(edge.from_version_id) ?? "unknown";
}

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
