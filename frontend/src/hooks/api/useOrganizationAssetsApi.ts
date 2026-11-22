// SPDX-License-Identifier: Apache-2.0

/**
 * The canonical asset store of the organization (#1178, dev-docs/asset-model.md section 9): one asset per real
 * thing across sources, its software, the tech stack built from it, the merge and split decisions and the SBOMs.
 * Read by the CTEM > Inventory page; every list is paginated on the server (`limit` at most 200).
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

/** One field of a canonical asset: the value shown, the source that set it and every source's value. */
interface AssetFieldOrigin {
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

/** The tech stack built from the assets' software: per product its kind, versions with asset counts, CPE. */
export function useTechStackInventory(query: { q?: string; offset: number; limit: number }) {
  const qs = pageQuery({ q: query.q?.trim(), offset: query.offset, limit: query.limit });
  return useQuery<Page<TechStackItem>>({
    queryKey: [...KEY, "tech-stack", qs],
    queryFn: () => api.get(`/organization-context/inventory/tech-stack?${qs}`),
    placeholderData: keepPreviousData,
  });
}
