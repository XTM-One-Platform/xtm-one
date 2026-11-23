// SPDX-License-Identifier: Apache-2.0

/**
 * The filters of an organization profile import (#11909) and of the sync saved
 * from it (#11913): their names, the words of their values, and a sync's filters
 * in one line, shared by the import stepper and Settings > Organization > Syncs.
 */
import { ASSET_CRITICALITY_LABELS, taxonomyValueLabel } from "@/lib/asset-taxonomy";

/** @i18n-keys */
export const FILTER_LABELS = {
  labels: "Labels",
  markings: "Markings",
  created_by: "Author",
  creator: "Creator",
  asset_groups: "Asset groups",
  tags: "Tags",
  category: "Category",
  subcategory: "Subcategory",
  criticality: "Criticality",
  internet_facing: "Exposure",
  environment: "Environment",
  platform: "Platform or OS",
  type: "Security platform type",
  created_after: "Created on or after",
  min_criticality: "Lowest criticality",
  tenant_id: "Tenant",
  pir_ids: "PIRs",
  search: "Name contains",
  // Microsoft Intune and Defender for Endpoint (#1251).
  operating_system: "Operating system",
  compliance: "Compliance",
  ownership: "Ownership",
  last_seen_days: "Last seen",
  include_owner: "Device owner",
  os_platform: "OS platform",
  health_status: "Sensor health",
  exposure_level: "Exposure level",
  // GitHub and GitLab repositories.
  organizations: "Organizations",
  groups: "Groups",
  include_archived: "Archived repositories",
  include_forks: "Forks",
  skip_dependencies: "Skip dependencies",
  // Tanium, CrowdStrike Falcon and ServiceNow CMDB.
  skip_software: "Skip software",
  filter_query: "FQL filter",
  ci_class: "CI class",
  operational_status: "Operational status",
  query: "Encoded query",
} as const;
export type FilterKey = keyof typeof FILTER_LABELS;

/** OpenCRQ's criticality levels of the "Lowest criticality" filter. @i18n-keys */
export const MIN_CRITICALITY_LABELS = { low: "Low", medium: "Medium", high: "High", very_high: "Very high" } as const;

/** @i18n-keys */
export const EXPOSURE_LABELS = { yes: "Internet-facing", no: "Not internet-facing", unknown: "Exposure unknown" } as const;

/** @i18n-keys */
export const ENVIRONMENT_LABELS = { production: "Production", staging: "Staging", development: "Development", test: "Test", other: "Other" } as const;

/** The words of the Microsoft endpoint filter values (the APIs' own enumerations, #1251). @i18n-keys */
const MICROSOFT_VALUE_LABELS = {
  compliant: "Compliant",
  noncompliant: "Not compliant",
  ingraceperiod: "In grace period",
  conflict: "Conflict",
  error: "Error",
  unknown: "Unknown",
  company: "Company",
  personal: "Personal",
  active: "Active",
  inactive: "Inactive",
  impairedcommunication: "Impaired communication",
  nosensordata: "No sensor data",
  high: "High",
  medium: "Medium",
  low: "Low",
  none: "None",
} as const;

/** The Microsoft filters whose values are enumerations worded above. */
const MICROSOFT_WORDED: ReadonlySet<string> = new Set(["compliance", "ownership", "health_status", "exposure_level"]);

/** ServiceNow's operational status choice values. @i18n-keys */
const OPERATIONAL_STATUS_LABELS = { "1": "Operational", "2": "Non-operational", "3": "Repair in progress", "4": "DR standby", "5": "Ready", "6": "Retired (ServiceNow status)" } as const;

/**
 * The picked filters whose values keep the source's own order instead of an A to Z
 * sort of their translated words: a scale (criticality) or a lifecycle (ServiceNow's
 * operational status, Operational first, Retired last), so the order never changes
 * with the language (UX round 2 of #1565).
 */
const SOURCE_ORDERED_FILTERS: ReadonlySet<string> = new Set(["criticality", "operational_status"]);

/** Whether the picker of a filter keeps the order the source gives its values. */
export function keepsSourceOrder(key: string): boolean {
  return SOURCE_ORDERED_FILTERS.has(key);
}

/** ServiceNow's CMDB hardware classes a sync may read. @i18n-keys */
const CI_CLASS_LABELS = { cmdb_ci_computer: "Computers", cmdb_ci_server: "Servers", cmdb_ci_hardware: "All hardware", cmdb_ci_netgear: "Network gear" } as const;

/** The filters whose values are ids of the source (labels, markings, authors, groups, tags, PIRs, tenant): counted, never printed. */
const ID_FILTERS: ReadonlySet<string> = new Set(["labels", "markings", "created_by", "creator", "asset_groups", "tags", "pir_ids", "tenant_id"]);

/** The on/off filters: a switch row of their own, "true" when on. @i18n-keys */
const SWITCH_VALUE_LABELS = {
  include_owner: "Kept as the asset owner",
  include_archived: "Included",
  include_forks: "Included",
  skip_dependencies: "Not read",
  skip_software: "Not read (software)",
} as const;

type Translate = (text: string, values?: Record<string, string | number>) => string;

/** The words of one filter value as the source writes it: a taxonomy, criticality, exposure or environment in the profile's words, else the value. */
export function filterValueLabel(t: Translate, key: string, value: string): string {
  const lower = value.toLowerCase();
  if (key === "category" || key === "subcategory" || key === "type") return taxonomyValueLabel(t, value);
  if (key === "criticality" && lower in ASSET_CRITICALITY_LABELS) return t(ASSET_CRITICALITY_LABELS[lower as keyof typeof ASSET_CRITICALITY_LABELS]);
  if (key === "min_criticality" && lower in MIN_CRITICALITY_LABELS) return t(MIN_CRITICALITY_LABELS[lower as keyof typeof MIN_CRITICALITY_LABELS]);
  if (key === "internet_facing" && lower in EXPOSURE_LABELS) return t(EXPOSURE_LABELS[lower as keyof typeof EXPOSURE_LABELS]);
  if (key === "environment" && lower in ENVIRONMENT_LABELS) return t(ENVIRONMENT_LABELS[lower as keyof typeof ENVIRONMENT_LABELS]);
  if (MICROSOFT_WORDED.has(key) && lower in MICROSOFT_VALUE_LABELS) return t(MICROSOFT_VALUE_LABELS[lower as keyof typeof MICROSOFT_VALUE_LABELS]);
  if (key === "operational_status" && value in OPERATIONAL_STATUS_LABELS) return t(OPERATIONAL_STATUS_LABELS[value as keyof typeof OPERATIONAL_STATUS_LABELS]);
  if (key === "ci_class" && value in CI_CLASS_LABELS) return t(CI_CLASS_LABELS[value as keyof typeof CI_CLASS_LABELS]);
  if (key === "last_seen_days" && /^\d+$/.test(value)) return t("{count, plural, one {Within # day} other {Within # days}}", { count: Number(value) });
  if (key in SWITCH_VALUE_LABELS) return t(SWITCH_VALUE_LABELS[key as keyof typeof SWITCH_VALUE_LABELS]);
  return value;
}

/**
 * A sync's filters in one line, in the viewer's language: "Category: Host, Server; Labels: 2 values".
 * An id filter is counted (its ids mean nothing to a reader); a date is written in the viewer's locale;
 * a filter this screen does not know is left out. Null when no filter is set.
 */
export function filtersSummary(t: Translate, locale: string, filters: Record<string, unknown> | null | undefined): string | null {
  const parts: string[] = [];
  for (const [key, raw] of Object.entries(filters ?? {})) {
    if (!(key in FILTER_LABELS)) continue;
    const values = (Array.isArray(raw) ? raw : raw ? [raw] : []).map(String).filter(Boolean);
    if (values.length === 0) continue;
    let shown: string;
    if (ID_FILTERS.has(key)) shown = t("{count, plural, one {# value} other {# values}}", { count: values.length });
    else if (key === "created_after") {
      const day = new Date(`${values[0]}T00:00:00Z`);
      shown = Number.isNaN(day.getTime()) ? values[0] : new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" }).format(day);
    } else shown = values.map((value) => filterValueLabel(t, key, value)).join(", ");
    parts.push(t("{filter}: {values}", { filter: t(FILTER_LABELS[key as FilterKey]), values: shown }));
  }
  return parts.length > 0 ? parts.join("; ") : null;
}
