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

/** An asset group's page (#1556). */
export function assetGroupPath(id: string): string {
  return `${INVENTORY_PATH}/asset-groups/${id}`;
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

/**
 * The criticality of an asset in a column or a facet whose heading already says "Criticality": "Unknown" instead of
 * "Criticality unknown", so the cell keeps one line at 1080 and reads like the Internet facing column (#1501 UX r1 B2).
 */
export function criticalityShortLabel(t: Translate, value: string | null | undefined): string {
  return (value ?? "").toLowerCase() === "unknown" ? t("Unknown") : assetFieldLabel(t, "criticality", value);
}

/** OpenAEV's criticality scale, most severe first: the order a reader expects (#1501 UX r1 B3). */
const CRITICALITY_ORDER: readonly string[] = ["very_high", "high", "medium", "low", "unknown"];

/** Internet facing as a reader asks it: Yes, No, then Unknown last, as Criticality ends (#1501 UX r2 B1). */
const INTERNET_FACING_ORDER: readonly string[] = ["yes", "no", "unknown"];

/** The enum facets listed on their own scale, never by key. */
const SCALE_ORDERS: Readonly<Record<string, readonly string[]>> = {
  criticality: CRITICALITY_ORDER,
  internet_facing: INTERNET_FACING_ORDER,
};

/** The category a facet lists last, whatever its translated label: the assets still to classify. */
const UNCLASSIFIED_CATEGORY = "generic_asset";

/**
 * Facet values in the order a first-time reader expects, never by key (#1501 UX r1 B3, r2 B1): criticality on its
 * scale, most severe first; internet facing Yes, No, Unknown; categories by their translated label with "Other
 * asset" last; a value outside a scale after the known ones. Every other facet keeps the order it came in.
 */
export function orderFacetValues<T extends { value: string }>(facet: string, values: readonly T[], label: (value: string) => string): T[] {
  const out = [...values];
  const scale = SCALE_ORDERS[facet];
  if (scale) {
    const rank = (v: string) => {
      const i = scale.indexOf(v.toLowerCase());
      return i === -1 ? scale.length : i;
    };
    return out.sort((a, b) => rank(a.value) - rank(b.value));
  }
  if (facet === "category") {
    return out.sort((a, b) => {
      const lastA = a.value === UNCLASSIFIED_CATEGORY ? 1 : 0;
      const lastB = b.value === UNCLASSIFIED_CATEGORY ? 1 : 0;
      return lastA - lastB || label(a.value).localeCompare(label(b.value));
    });
  }
  return out;
}

/** An asset's category in words: "Category > Subcategory", or the category alone. */
export function assetCategoryLabel(t: Translate, asset: { category?: string | null; subcategory?: string | null }): string {
  if (!asset.category) return asset.subcategory ? taxonomyValueLabel(t, asset.subcategory) : "";
  return typeLabel(t, asset.category.toLowerCase(), (asset.subcategory ?? "").toLowerCase());
}
