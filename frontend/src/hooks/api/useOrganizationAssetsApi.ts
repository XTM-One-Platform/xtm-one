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

export interface TechStackVersion {
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

export interface AssetDecision {
  id: string;
  kind: "merge" | "split";
  records: { source: string; external_id: string }[];
  reason: string;
  created_at?: string;
}

export interface AssetIdentitySettings {
  saved: { key_order: string[]; precedence: Record<string, string[]> };
  defaults: {
    key_order: string[];
    weak_keys: string[];
    precedence: Record<string, string[]>;
    fallback_precedence: string[];
    local_fields: string[];
  };
  effective: { key_order: string[]; precedence: Record<string, string[]> };
}

export interface SbomImportResult {
  id: string;
  format: string;
  document: string;
  components: number;
  unknown_versions: number;
  bundled: number;
  replaced: boolean;
}

const KEY = ["organization-assets"] as const;

function pageQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    search.set(name, String(value));
  }
  return search.toString();
}

/** A page of canonical assets, filtered on a name substring. */
export function useCanonicalAssets(query: { q?: string; offset: number; limit: number }) {
  const qs = pageQuery({ q: query.q?.trim(), offset: query.offset, limit: query.limit });
  return useQuery<Page<CanonicalAssetSummary>>({
    queryKey: [...KEY, "list", qs],
    queryFn: () => api.get(`/organization-context/assets?${qs}`),
    placeholderData: keepPreviousData,
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

/** How assets are matched and which source each field shows (saved, defaults, effective). */
export function useAssetIdentitySettings() {
  return useQuery<AssetIdentitySettings>({
    queryKey: [...KEY, "identity"],
    queryFn: () => api.get("/organization-context/assets/identity"),
  });
}

/** The administrators' merge and split decisions, newest first. */
export function useAssetDecisions() {
  return useQuery<{ items: AssetDecision[] }>({
    queryKey: [...KEY, "decisions"],
    queryFn: () => api.get("/organization-context/assets/decisions"),
  });
}

/** Merge source records into one asset, or keep two apart (administrators, audited). */
export function useRecordAssetDecision() {
  const qc = useQueryClient();
  return useMutation<AssetDecision, Error, { kind: "merge" | "split"; records: { source: string; external_id: string }[]; reason?: string }>({
    mutationFn: (body) => api.post("/organization-context/assets/decisions", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}

/** Drop a merge or split: the identity keys decide again. */
export function useRemoveAssetDecision() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/organization-context/assets/decisions/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}

/** Import a CycloneDX or SPDX JSON SBOM on an asset (administrators, audited). */
export function useImportAssetSbom(assetId: string | undefined) {
  const qc = useQueryClient();
  return useMutation<SbomImportResult, Error, File>({
    mutationFn: (file) => {
      const form = new FormData();
      form.append("file", file, file.name);
      return api.postForm(`/organization-context/assets/${assetId}/sbom`, form);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
    // The dialog says every failure inline (refused over 10,000 components, not CycloneDX or SPDX).
    meta: { suppressGlobalErrorToast: true },
  });
}
