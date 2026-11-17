// SPDX-License-Identifier: Apache-2.0

/**
 * The one builder of CTEM deep links into OpenCTI lists (#11954).
 *
 * The backend sends each figure's filter as the FilterGroup it counted with,
 * in OpenCTI's GraphQL shape: `key` is a list of keys (`["regardingOf"]`) and
 * a filter may omit `operator` and `mode`. OpenCTI's list pages read the
 * `filters` URL parameter in their own frontend shape instead: `key` is ONE
 * string, and the list keeps only the filters whose key is available for the
 * entity type (`keepAvailableFilterKeys` in opencti-front `filtersUtils.tsx`
 * compares the key with `includes`). A list key never matches, so every
 * filter was dropped and the page opened on every vulnerability.
 *
 * `toOpenCtiUrlFilterGroup` converts the GraphQL shape to the URL shape,
 * recursively, keeping every nested group (the exploitable KEV / EPSS group
 * rides in `filterGroups`). A filter whose key is not a single key cannot be
 * expressed in the URL: the whole link is refused (null), so a figure never
 * opens a wider list than the one it counts.
 */

/** A filter as OpenCTI's list URL reads it. */
interface OpenCtiUrlFilter {
  key: string;
  values: unknown[];
  operator: string;
  mode: "and" | "or";
}

/** A filter group as OpenCTI's list URL reads it. */
export interface OpenCtiUrlFilterGroup {
  mode: "and" | "or";
  filters: OpenCtiUrlFilter[];
  filterGroups: OpenCtiUrlFilterGroup[];
}

/** OpenCTI's Arsenal > Vulnerabilities list, where every vulnerability figure leads. */
export const OPENCTI_VULNERABILITIES_PATH = "/dashboard/arsenal/vulnerabilities";

/**
 * The FilterGroup each Command Center vulnerability KPI counted with, pinned
 * to the backend's `_F_KEV` and `_F_CRITICAL` (ctem_metrics_collector.py),
 * so a KPI opens exactly the list it counts (#11954).
 */
const CTEM_KPI_OPENCTI_FILTERS: Partial<Record<string, Record<string, unknown>>> = {
  kev_count: { mode: "and", filters: [{ key: ["x_opencti_cisa_kev"], values: ["true"] }], filterGroups: [] },
  critical_count: {
    mode: "and",
    filters: [{ key: ["x_opencti_cvss_base_score"], values: ["9"], operator: "gte" }],
    filterGroups: [],
  },
};

/** The OpenCTI list a Command Center KPI counts, filtered; null when the KPI has no OpenCTI vulnerability filter. */
export function ctemKpiOpenCtiHref(headlineKey: string, openctiUrl: string | null | undefined): string | null {
  const filters = CTEM_KPI_OPENCTI_FILTERS[headlineKey];
  if (!filters) return null;
  return openCtiListHref(openctiUrl, OPENCTI_VULNERABILITIES_PATH, filters);
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const modeOf = (value: unknown, fallback: "and" | "or"): "and" | "or" | null => {
  if (value === undefined || value === null) return fallback;
  return value === "and" || value === "or" ? value : null;
};

function toUrlFilter(raw: unknown): OpenCtiUrlFilter | null {
  if (!isRecord(raw)) return null;
  const key = Array.isArray(raw.key) ? (raw.key.length === 1 ? raw.key[0] : null) : raw.key;
  if (typeof key !== "string" || key.length === 0) return null;
  if (!Array.isArray(raw.values)) return null;
  const mode = modeOf(raw.mode, "or");
  if (!mode) return null;
  const operator = raw.operator ?? "eq";
  if (typeof operator !== "string") return null;
  return { key, values: [...raw.values], operator, mode };
}

/**
 * The FilterGroup in the shape OpenCTI's list URL keeps, or null when any
 * filter of it cannot be carried (a multi-key filter, a malformed group).
 */
export function toOpenCtiUrlFilterGroup(raw: unknown): OpenCtiUrlFilterGroup | null {
  if (!isRecord(raw)) return null;
  const mode = modeOf(raw.mode, "and");
  if (!mode) return null;
  const filters: OpenCtiUrlFilter[] = [];
  for (const item of Array.isArray(raw.filters) ? raw.filters : []) {
    const filter = toUrlFilter(item);
    if (!filter) return null;
    filters.push(filter);
  }
  const filterGroups: OpenCtiUrlFilterGroup[] = [];
  for (const item of Array.isArray(raw.filterGroups) ? raw.filterGroups : []) {
    const group = toOpenCtiUrlFilterGroup(item);
    if (!group) return null;
    filterGroups.push(group);
  }
  return { mode, filters, filterGroups };
}

/**
 * The absolute link to an OpenCTI list: unfiltered when `filters` is
 * undefined, filtered with the converted group otherwise, null when the
 * filter cannot be carried or OpenCTI's URL is unknown.
 */
export function openCtiListHref(
  openctiUrl: string | null | undefined,
  path: string,
  filters?: unknown,
): string | null {
  if (!openctiUrl) return null;
  const base = `${openctiUrl.replace(/\/+$/, "")}${path}`;
  if (filters === undefined) return base;
  const group = toOpenCtiUrlFilterGroup(filters);
  if (!group) return null;
  return `${base}?filters=${encodeURIComponent(JSON.stringify(group))}`;
}
