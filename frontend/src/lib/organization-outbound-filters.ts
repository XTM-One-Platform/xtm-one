// SPDX-License-Identifier: Apache-2.0

/**
 * The filters of an outbound sync (#1982 S4): which XTM One rows the run pushes to the platform.
 * The keys are the import step's, read on the XTM One row by the backend's outbound_row_matches
 * (backend/app/services/organization_context_outbound.py, OUTBOUND_FILTER_FIELDS); their values
 * are the profile's own, so the picker lists them without reading the platform.
 */
import type { ImportSectionKey, ImportSourceKey, SyncDirectionSupport } from "@/hooks/api/useOrganizationContextApi";
import { ASSET_CATEGORY_LABELS, ASSET_CRITICALITY_LABELS } from "@/lib/asset-taxonomy";
import { ENVIRONMENT_LABELS, EXPOSURE_LABELS, type FilterKey } from "@/lib/organization-import-filters";

/** The filters an outbound sync of a section offers: a group has no category. */
const OUTBOUND_FILTERS: Partial<Record<ImportSectionKey, FilterKey[]>> = {
  assets: ["category", "criticality", "internet_facing", "environment", "search"],
  asset_groups: ["criticality", "internet_facing", "environment", "search"],
};

export function outboundFilterKeys(section: ImportSectionKey | null): FilterKey[] {
  return (section && OUTBOUND_FILTERS[section]) || [];
}

/** The values of an outbound picker, in the profile's order (a scale keeps its order). */
export function outboundFilterValues(key: FilterKey): string[] {
  if (key === "category") return Object.keys(ASSET_CATEGORY_LABELS);
  if (key === "criticality") return Object.keys(ASSET_CRITICALITY_LABELS);
  if (key === "internet_facing") return Object.keys(EXPOSURE_LABELS);
  if (key === "environment") return Object.keys(ENVIRONMENT_LABELS);
  return [];
}

/** The sections, then the platforms of a section, an outbound sync may take (the backend's DIRECTIONS). */
export function outboundSections(matrix: SyncDirectionSupport[] | undefined, offered?: readonly ImportSectionKey[]): ImportSectionKey[] {
  // An entry read before the directions field existed (an older answer, a test double) carries none: no outbound.
  const sections = (matrix ?? []).filter((entry) => entry.directions?.includes("outbound")).map((entry) => entry.section);
  return [...new Set(sections)].filter((section) => !offered || offered.includes(section));
}

export function outboundSources(matrix: SyncDirectionSupport[] | undefined, section: ImportSectionKey | null): ImportSourceKey[] {
  return (matrix ?? []).filter((entry) => entry.section === section && entry.directions?.includes("outbound")).map((entry) => entry.source);
}
