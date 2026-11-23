// SPDX-License-Identifier: Apache-2.0

/**
 * The canonical asset store of the organization (#1178, dev-docs/asset-model.md section 9): one asset per real
 * thing across sources, its software, the tech stack built from it, the merge and split decisions and the SBOMs.
 * Read by the CTEM > Inventory page; every list is paginated on the server (`limit` at most 200).
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

/** One field of a canonical asset: the value shown, the source that set it and every source's value. */
export interface AssetFieldOrigin {
  value: unknown;
  source?: string | null;
  values?: Record<string, unknown>;
}

export interface CanonicalAssetSummary {
  id: string;
  name: string;
  category: string | null;
  subcategory: string | null;
  criticality: string | null;
  internet_facing: string | null;
  owner: string | null;
  os_family: string | null;
  os_version: string | null;
  sources: string[];
  software_count: number;
  merged_on: string[];
  /** The source records merged into it (what a merge names); absent on an older API. */
  records?: AssetRecordRef[];
}

export interface AssetSoftware {
  vendor: string;
  product: string;
  /** `unknown` when no source knows it, never empty. */
  version: string;
  cpe: string | null;
  purl: string | null;
  install_date: string | null;
  /** The component that bundles this one (an image), when it came from an SBOM. */
  bundled_in: string | null;
  first_seen: string | null;
  last_seen: string | null;
  sources: string[];
}

export interface AssetSourceRecord {
  source: string;
  external_id: string;
  identifiers: Record<string, unknown>;
  fields: Record<string, unknown>;
  last_seen: string | null;
}

export interface CanonicalAssetDetail extends CanonicalAssetSummary {
  fields: Record<string, AssetFieldOrigin>;
  explanation: string;
  possible_matches: unknown[];
  records: AssetSourceRecord[];
  software: { items: AssetSoftware[]; total: number; offset: number; limit: number };
}

export interface Page<T> {
  items: T[];
  total: number;
  offset: number;
  limit: number;
}

interface TechStackVersion {
  version: string;
  asset_count: number;
}

export interface TechStackItem {
  vendor: string;
  product: string;
  /** installed, saas or cloud_service. */
  kind: string;
  versions: TechStackVersion[];
  asset_count: number;
  sources: string[];
  manual: boolean;
  cpe?: string;
  cpe_source?: string;
  cpe_confidence?: string;
}

const KEY = ["organization-assets"] as const;

function pageQuery(params: Record<string, string | number | readonly string[] | undefined>): string {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    // A list is the same name repeated, one value each (the API's list filters).
    if (Array.isArray(value)) value.forEach((one) => search.append(name, one));
    else search.set(name, String(value));
  }
  return search.toString();
}

/** The asset fields the list filters on, and the source of a record (`GET /assets/facets`). */
export type AssetFilterKey = "category" | "subcategory" | "criticality" | "internet_facing" | "source";

/** One facet value with the assets it holds; a subcategory names its category. */
export interface AssetFacetValue {
  value: string;
  count: number;
  category?: string;
}

export type AssetFacets = Record<AssetFilterKey, AssetFacetValue[]>;

/** A page of canonical assets, filtered on a name substring and on the filter values picked. */
export function useCanonicalAssets(query: {
  q?: string;
  offset: number;
  limit: number;
  filters?: Partial<Record<AssetFilterKey, readonly string[]>>;
}) {
  const qs = pageQuery({ q: query.q?.trim(), offset: query.offset, limit: query.limit, ...query.filters });
  return useQuery<Page<CanonicalAssetSummary>>({
    queryKey: [...KEY, "list", qs],
    queryFn: () => api.get(`/organization-context/assets?${qs}`),
    placeholderData: keepPreviousData,
  });
}

/**
 * The values each asset filter offers with their counts, read from the store: a facet's counts apply the search and
 * the other facets picked (#1501), so the sidebar says how many assets a value adds.
 */
export function useAssetFacets(query: { q?: string; filters?: Partial<Record<AssetFilterKey, readonly string[]>> } = {}) {
  const qs = pageQuery({ q: query.q?.trim(), ...query.filters });
  return useQuery<AssetFacets>({
    queryKey: [...KEY, "facets", qs],
    queryFn: () => api.get(`/organization-context/assets/facets${qs ? `?${qs}` : ""}`),
    placeholderData: keepPreviousData,
    throwOnError: false,
  });
}

/** One canonical asset with its field origins, its records and a page of its software. */
export function useCanonicalAsset(id: string | undefined, software: { offset: number; limit: number }) {
  const qs = pageQuery({ software_offset: software.offset, software_limit: software.limit });
  return useQuery<CanonicalAssetDetail>({
    queryKey: [...KEY, "detail", id, qs],
    queryFn: () => api.get(`/organization-context/assets/${id}?${qs}`),
    enabled: Boolean(id),
    placeholderData: keepPreviousData,
    // An asset a sync, a merge or a split replaced answers 404: the page draws its not-found state, never the
    // section error boundary, and a retry would not bring it back (UX round 1 on #1254, B5).
    retry: (failureCount, error) => !isAssetNotFound(error) && failureCount < 2,
    throwOnError: (error) => !isAssetNotFound(error),
  });
}

/** A 404 on one asset: it does not exist (any more). */
export function isAssetNotFound(error: unknown): boolean {
  return (error as { status?: number } | null)?.status === 404;
}

/** The strong identifier kinds, most reliable first (organization_asset_identity.DEFAULT_KEY_ORDER). */
type IdentityKey = "serial" | "cloud_resource_id" | "repository" | "device_id" | "mac" | "fqdn" | "hostname";

interface IdentitySettings {
  key_order: IdentityKey[];
  /** Per field, the sources or source classes in order; `*` is any other source. */
  precedence: Record<string, string[]>;
}

/** How one asset is matched across sources and which source each field shows (#1178): saved, defaults, effective. */
export interface AssetIdentityResponse {
  saved: IdentitySettings;
  defaults: IdentitySettings & { weak_keys: string[]; fallback_precedence: string[]; local_fields: string[] };
  effective: IdentitySettings;
}

export function useAssetIdentity() {
  return useQuery<AssetIdentityResponse>({
    queryKey: [...KEY, "identity"],
    queryFn: () => api.get("/organization-context/assets/identity"),
    throwOnError: false,
    meta: { suppressGlobalErrorToast: true },
  });
}

/** What an SBOM import answers (`POST /assets/{id}/sbom`). */
export interface SbomImportResult {
  id: string;
  format: string;
  document: string;
  components: number;
  unknown_versions: number;
  bundled: number;
  replaced: boolean;
}

/** Import a CycloneDX or SPDX JSON SBOM on an asset (administrators); the asset's software reads again. */
export function useImportAssetSbom(assetId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation<SbomImportResult, Error, File>({
    mutationFn: (file) => {
      const form = new FormData();
      form.append("file", file);
      return api.postForm(`/organization-context/assets/${assetId}/sbom`, form);
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: KEY }),
  });
}

/** One source record as a merge or split names it. */
export interface AssetRecordRef {
  source: string;
  external_id: string;
}

/** An administrator's merge or split the store applies (`GET /assets/decisions`). */
export interface AssetDecision {
  id: string;
  kind: "merge" | "split";
  records: AssetRecordRef[];
  reason: string;
  author_user_id: string | null;
  created_at: string | null;
}

/** Merge records into one asset or keep two apart (administrators, audited); the store rebuilds. */
export function useRecordAssetDecision() {
  const queryClient = useQueryClient();
  return useMutation<AssetDecision, Error, { kind: "merge" | "split"; records: AssetRecordRef[]; reason?: string }>({
    mutationFn: (body) => api.post("/organization-context/assets/decisions", body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: KEY }),
  });
}

/** The tech stack built from the assets' software: per product its kind, versions with asset counts, CPE. */
export function useTechStackInventory(query: { q?: string; offset: number; limit: number }) {
  const qs = pageQuery({ q: query.q?.trim(), offset: query.offset, limit: query.limit });
  return useQuery<Page<TechStackItem>>({
    queryKey: [...KEY, "tech-stack", qs],
    queryFn: () => api.get(`/organization-context/inventory/tech-stack?${qs}`),
    placeholderData: keepPreviousData,
  });
}
