// SPDX-License-Identifier: Apache-2.0

/**
 * The canonical asset store of the organization (#1178, dev-docs/asset-model.md section 9): one asset per real
 * thing across sources, its software, the tech stack built from it, the merge and split decisions and the SBOMs.
 * Read by the CTEM > Inventory page; every list is paginated on the server (`limit` at most 200).
 */

import { useCallback } from "react";
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

/** One source record as a merge or split names it; `id` is the asset record the row APIs write (#1543). */
export interface AssetRecordRef {
  source: string;
  external_id: string;
  id?: string;
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

// ── Row APIs of the asset records and groups (#1543): one row in, one row out, never the whole profile ──

/** An asset record as the row APIs answer it: its row, id and row version (what `If-Match` takes). */
export interface AssetRecord {
  id: string;
  asset_id: string | null;
  row_version: number;
  name: string;
  category: string;
  subcategory: string;
  criticality: string;
  environment: string;
  internet_facing: string;
  owner: string;
  group: string;
  source: string;
  external_id: string;
  [field: string]: unknown;
}

/** An asset group as the row APIs answer it, with the number of assets in it. */
export interface AssetGroupRecord {
  id: string;
  row_version: number;
  name: string;
  description: string;
  environment: string;
  internet_facing: string;
  owner: string;
  tags: string[];
  tech_stack: string[];
  source: string;
  external_id: string;
  member_count: number;
}

const GROUPS_KEY = [...KEY, "groups"] as const;

/**
 * Every asset group of the organization (`GET /asset-groups`). `throwOnError: false` keeps a failed read with the
 * consumer that draws its own error state (the entry dialog), instead of the section error boundary.
 */
export function useAssetGroups(options: { throwOnError?: false } = {}) {
  return useQuery<{ items: AssetGroupRecord[] }>({
    queryKey: GROUPS_KEY,
    queryFn: () => api.get("/organization-context/asset-groups"),
    ...options,
  });
}

/** One asset record, for the update form (`GET /assets/records/{id}`). */
export function useAssetRecord(id: string | undefined) {
  return useQuery<AssetRecord>({
    queryKey: [...KEY, "record", id],
    queryFn: () => api.get(`/organization-context/assets/records/${id}`),
    enabled: Boolean(id),
    // A record a sync or another admin removed since the list loaded answers 404: the update dialog draws its own
    // error state (Retry, Close), never the section error boundary, and a retry would not bring it back (UX round 1
    // on #1551, B1).
    retry: (failureCount, error) => !isAssetNotFound(error) && failureCount < 2,
    throwOnError: false,
  });
}

/** The fields a person sets on a record or a group from the Inventory. */
export type RowPatch = Record<string, string>;

/** What a write of several rows answers: the rows written and the rows the API refused, with its reason. */
export interface RowsOutcome {
  written: number;
  refused: { id: string; reason: string }[];
}

/** The asset lists and pages read again after a row write (never the whole profile). */
function invalidateAssets(qc: ReturnType<typeof useQueryClient>) {
  void qc.invalidateQueries({ queryKey: KEY });
}

/** Reads the asset lists and pages again: a dialog that found its row gone refreshes the list behind it. */
export function useRefreshAssets(): () => void {
  const qc = useQueryClient();
  // The record the dialog failed to read is left out: reading it again would only fail again.
  return useCallback(() => void qc.invalidateQueries({ queryKey: KEY, predicate: (q) => q.queryKey[1] !== "record" }), [qc]);
}

/** Add one asset by hand (`POST /assets`). */
export function useCreateAssetRecord() {
  const qc = useQueryClient();
  return useMutation<AssetRecord, Error, RowPatch>({
    mutationFn: (row) => api.post("/organization-context/assets", { ...row, source: "manual" }),
    onSuccess: () => invalidateAssets(qc),
  });
}

/**
 * Change records one PATCH each (`PATCH /assets/records/{id}`): one row in, one row out. A refused row (a field its
 * sync sends, a stale version) is reported, the others are written.
 */
export function useUpdateAssetRecords() {
  const qc = useQueryClient();
  return useMutation<RowsOutcome, Error, { ids: string[]; patch: RowPatch }>({
    mutationFn: async ({ ids, patch }) => {
      const settled = await Promise.allSettled(ids.map((id) => api.patch(`/organization-context/assets/records/${id}`, patch)));
      return outcome(ids, settled);
    },
    onSettled: () => invalidateAssets(qc),
  });
}

/**
 * Delete records in one call (`POST /assets/bulk-delete`, at most 1,000). The assets whose every record goes leave the
 * open list pages at once; the pages read again once the server answers.
 */
export function useDeleteAssetRecords() {
  const qc = useQueryClient();
  return useMutation<{ removed: string[] }, Error, { ids: string[]; assetIds: string[] }, { pages: [readonly unknown[], Page<CanonicalAssetSummary> | undefined][] }>({
    mutationFn: ({ ids }) => api.post("/organization-context/assets/bulk-delete", { ids }),
    onMutate: async ({ assetIds }) => {
      const listKey = [...KEY, "list"];
      await qc.cancelQueries({ queryKey: listKey });
      const pages = qc.getQueriesData<Page<CanonicalAssetSummary>>({ queryKey: listKey });
      const gone = new Set(assetIds);
      qc.setQueriesData<Page<CanonicalAssetSummary>>({ queryKey: listKey }, (page) =>
        page ? { ...page, items: page.items.filter((a) => !gone.has(a.id)), total: Math.max(0, page.total - page.items.filter((a) => gone.has(a.id)).length) } : page,
      );
      return { pages };
    },
    onError: (_error, _vars, context) => context?.pages.forEach(([key, page]) => qc.setQueryData(key, page)),
    onSettled: () => invalidateAssets(qc),
  });
}

/** Add an asset group (`POST /asset-groups`). */
export function useCreateAssetGroup() {
  const qc = useQueryClient();
  return useMutation<AssetGroupRecord, Error, RowPatch>({
    mutationFn: (row) => api.post("/organization-context/asset-groups", { ...row, source: "manual" }),
    onSuccess: () => invalidateAssets(qc),
  });
}

/** Change asset groups, one PATCH each (`PATCH /asset-groups/{id}`). */
export function useUpdateAssetGroups() {
  const qc = useQueryClient();
  return useMutation<RowsOutcome, Error, { ids: string[]; patch: RowPatch }>({
    mutationFn: async ({ ids, patch }) => outcome(ids, await Promise.allSettled(ids.map((id) => api.patch(`/organization-context/asset-groups/${id}`, patch)))),
    onSettled: () => invalidateAssets(qc),
  });
}

/** Delete asset groups, one DELETE each (`DELETE /asset-groups/{id}`); their assets stay and lose the group. */
export function useDeleteAssetGroups() {
  const qc = useQueryClient();
  return useMutation<RowsOutcome, Error, string[], { groups?: { items: AssetGroupRecord[] } }>({
    mutationFn: async (ids) => outcome(ids, await Promise.allSettled(ids.map((id) => api.delete(`/organization-context/asset-groups/${id}`)))),
    onMutate: async (ids) => {
      await qc.cancelQueries({ queryKey: GROUPS_KEY });
      const groups = qc.getQueryData<{ items: AssetGroupRecord[] }>(GROUPS_KEY);
      const gone = new Set(ids);
      if (groups) qc.setQueryData(GROUPS_KEY, { items: groups.items.filter((g) => !gone.has(g.id)) });
      return { groups };
    },
    onError: (_error, _ids, context) => context?.groups && qc.setQueryData(GROUPS_KEY, context.groups),
    onSettled: () => invalidateAssets(qc),
  });
}

function outcome(ids: readonly string[], settled: readonly PromiseSettledResult<unknown>[]): RowsOutcome {
  const refused = settled.flatMap((result, i) => (result.status === "rejected" ? [{ id: ids[i], reason: (result.reason as Error)?.message ?? "" }] : []));
  return { written: settled.length - refused.length, refused };
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
