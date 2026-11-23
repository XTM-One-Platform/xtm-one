// SPDX-License-Identifier: Apache-2.0

/**
 * The canonical asset store of the organization (#1178, dev-docs/asset-model.md section 9): one asset per real
 * thing across sources, its software (one entity per vendor and product, #1495), the merge and split decisions and the SBOMs.
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
  /** The software this row installs (its page), absent before the first rebuild links it (#1495). */
  software_id?: string | null;
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

/** The types of a software (organization_software_store.SOFTWARE_TYPES). */
export type SoftwareType = "application" | "operating_system" | "library" | "firmware" | "container_image" | "saas";

/** How sure its CPE is (organization_asset_identity.CPE_CONFIDENCES). */
export type CpeState = "override" | "reported" | "high" | "medium" | "low" | "unresolved";

/** One software of the organization (#1495): one per vendor and product across every asset and SBOM. */
export interface SoftwareSummary {
  id: string;
  name: string;
  vendor: string;
  product: string;
  type: SoftwareType;
  description: string;
  cpe: string;
  cpe_state: CpeState;
  cpe_override: string;
  purl: string;
  /** `synced`: a source reports it, read-only; `manual`: typed by an administrator. */
  origin: "synced" | "manual";
  editable: boolean;
  sources: string[];
  version_count: number;
  asset_count: number;
  has_unknown_version: boolean;
  first_seen: string | null;
  last_seen: string | null;
}

export interface SoftwareVersion {
  id: string;
  /** `unknown` when no source knows it, never empty. */
  version: string;
  cpe: string;
  purl: string;
  eol: string | null;
  asset_count: number;
  first_seen: string | null;
  last_seen: string | null;
}

export interface SoftwareInstallation {
  asset_id: string;
  asset_name: string;
  version_id: string | null;
  version: string;
  install_date: string | null;
  bundled_in: string | null;
  sources: string[];
  first_seen: string | null;
  last_seen: string | null;
}

/** One dependency edge: a version of this software and the version of another it depends on (or that uses it). */
export interface SoftwareEdge {
  /** `depends_on` or `bundled_in`. */
  kind: string;
  scope: string;
  from_version_id: string;
  version_id: string;
  version: string;
  software_id: string;
  software_name: string;
}

export interface SoftwareDetail extends SoftwareSummary {
  versions: SoftwareVersion[];
  installations: Page<SoftwareInstallation>;
  depends_on: SoftwareEdge[];
  used_by: SoftwareEdge[];
}

/** The server filters of the software list. */
export type SoftwareFilterKey = "type" | "origin" | "cpe_state";

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
export type AssetFilterKey = "category" | "criticality" | "internet_facing" | "source";

export type AssetFacets = Record<AssetFilterKey, string[]>;

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

/** The values each asset filter offers, read from the store. */
export function useAssetFacets() {
  return useQuery<AssetFacets>({
    queryKey: [...KEY, "facets"],
    queryFn: () => api.get("/organization-context/assets/facets"),
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
type IdentityKey = "serial" | "cloud_resource_id" | "device_id" | "mac" | "fqdn" | "hostname";

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

/** A page of the organization's software (#1495), filtered on a name and on one value per filter. */
export function useSoftwareList(query: {
  q?: string;
  offset: number;
  limit: number;
  filters?: Partial<Record<SoftwareFilterKey, readonly string[]>>;
}) {
  const qs = pageQuery({ q: query.q?.trim(), offset: query.offset, limit: query.limit, ...query.filters });
  return useQuery<Page<SoftwareSummary>>({
    queryKey: [...KEY, "software", qs],
    queryFn: () => api.get(`/organization-context/software?${qs}`),
    placeholderData: keepPreviousData,
  });
}

/** One software with its versions, a page of its installations and its dependencies both ways. */
export function useSoftware(id: string | undefined, installations: { offset: number; limit: number; q?: string }) {
  const qs = pageQuery({
    installations_offset: installations.offset,
    installations_limit: installations.limit,
    installations_q: installations.q?.trim(),
  });
  return useQuery<SoftwareDetail>({
    queryKey: [...KEY, "software-detail", id, qs],
    queryFn: () => api.get(`/organization-context/software/${id}?${qs}`),
    enabled: Boolean(id),
    placeholderData: keepPreviousData,
    // A software a rebuild removed answers 404: the page draws its not-found state, never the error boundary.
    retry: (failureCount, error) => !isAssetNotFound(error) && failureCount < 2,
    throwOnError: (error) => !isAssetNotFound(error),
  });
}
