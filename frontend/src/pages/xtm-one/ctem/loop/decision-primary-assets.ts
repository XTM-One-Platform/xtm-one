// SPDX-License-Identifier: Apache-2.0

/**
 * What the decision record says about primary assets and the scope threshold
 * (#1591 S3, dev-docs/ctem-scope-gate.md): the primary assets a signal reached
 * through the inventory links, each with its chain (software, asset, asset
 * group, primary asset, role), and the one threshold the gate compared with,
 * with where it comes from (the policy, or the scope default).
 *
 * The backend writes `reconcile.primary_assets` (one hit per primary asset key,
 * through and source), `reconcile.inventory_links_read` and, per primary asset
 * kept as a criticality source, `scope.sources[].chain`; `scope.threshold_source`
 * is "policy" or "default" (backend/app/services/ctem_scope_gate.py).
 */
import type { CtemDecisionRecord } from "@/hooks/api/useCtemSignalsApi";

type Translate = (id: string, values?: Record<string, string | number>) => string;

/** One hit of `reconcile.primary_assets` (`PrimaryAssetIndex.reach`). */
interface PrimaryAssetHit {
  primary_asset_key: string;
  software?: string;
  asset?: string;
  through?: string;
  through_kind?: string;
  source?: string;
  roles?: string[];
  via?: string;
}

/** One primary asset reached, its name as the policy writes it, and its chains. */
export interface PrimaryAssetReached {
  name: string;
  /** The key the record and the supporting asset read use (#1979). */
  key: string;
  criticality: string | null;
  chains: PrimaryAssetHit[];
  /** How many chains the record counted, kept or not; null on a record older than #1979. */
  chainTotal: number | null;
  /** How many inventory assets reach it; null on a record older than #1979. */
  assetsTotal: number | null;
  /** Of those, linked to it directly and through an asset group only. */
  direct: number | null;
  viaGroup: number | null;
}

function isHit(value: unknown): value is PrimaryAssetHit {
  return !!value && typeof value === "object" && typeof (value as PrimaryAssetHit).primary_asset_key === "string";
}

const keyOf = (name: string) => name.split(/\s+/).filter(Boolean).join(" ").toLowerCase().slice(0, 200);

/**
 * The primary assets reached: the criticality sources first (they carry the
 * name and the criticality the gate used), then any hit no source names, by key.
 */
export function primaryAssetsReached(decision: CtemDecisionRecord | null | undefined): PrimaryAssetReached[] {
  if (!decision) return [];
  const hits = (Array.isArray(decision.reconcile?.primary_assets) ? decision.reconcile.primary_assets : []).filter(isHit);
  const sources = decision.scope?.sources ?? [];
  // #1979: per key, how many inventory assets reach it (absent on an older record).
  const totalsRaw = decision.reconcile?.primary_asset_totals;
  const totals: Record<string, { assets?: number; direct?: number; via_group?: number }> =
    totalsRaw && typeof totalsRaw === "object" ? (totalsRaw as Record<string, { assets?: number; direct?: number; via_group?: number }>) : {};
  const counted = (key: string, chainTotal: number | null, sourceAssets?: number) => {
    const row = totals[key];
    const assets = typeof sourceAssets === "number" ? sourceAssets : typeof row?.assets === "number" ? row.assets : null;
    return {
      chainTotal,
      assetsTotal: assets,
      direct: typeof row?.direct === "number" ? row.direct : null,
      viaGroup: typeof row?.via_group === "number" ? row.via_group : null,
    };
  };
  const out: PrimaryAssetReached[] = [];
  const seen = new Set<string>();
  for (const source of sources) {
    if (source.from !== "primary_asset") continue;
    const key = keyOf(source.name);
    const chains = Array.isArray(source.chain) ? source.chain.filter(isHit) : hits.filter((h) => h.primary_asset_key === key);
    if (chains.length === 0) continue;
    seen.add(key);
    const chainTotal = typeof source.chain_total === "number" ? source.chain_total : null;
    out.push({ name: source.name, key, criticality: source.criticality ?? null, chains, ...counted(key, chainTotal, source.assets_total) });
  }
  for (const hit of hits) {
    if (seen.has(hit.primary_asset_key)) continue;
    const existing = out.find((p) => p.key === hit.primary_asset_key);
    if (existing) existing.chains.push(hit);
    else out.push({ name: hit.primary_asset_key, key: hit.primary_asset_key, criticality: null, chains: [hit], ...counted(hit.primary_asset_key, null) });
    // A key no criticality source names keeps every one of its hits in one entry.
  }
  for (const p of out) if (p.chainTotal === null && p.assetsTotal !== null) p.chainTotal = p.chains.length;
  return out;
}

/**
 * The link roles OpenCRQ writes (`LINK_ROLE_INHERITANCE` of
 * backend/app/services/organization_context_opencrq_links.py), each with its
 * label; a role outside this list reads as "unknown", as the backend
 * canonicalises it (UX r1 B2 on #1796: no raw role word on screen).
 */
const LINK_ROLE_LABEL = {
  stores: "stores",
  processes: "processes",
  reads: "reads",
  transmits: "transmits",
  administers: "administers",
  monitors: "monitors",
  supports_availability: "supports availability",
  performs: "performs",
  unknown: "unknown",
} as const;
const LINK_ROLE_LABELS: Readonly<Record<string, string>> = LINK_ROLE_LABEL;

/** The label of one link role; an unknown word reads as "unknown". */
export function linkRoleLabel(role: string, t_i18n: Translate): string {
  return t_i18n(LINK_ROLE_LABELS[role.trim().toLowerCase()] ?? LINK_ROLE_LABEL.unknown);
}

/** At most this many chains per primary asset; the rest is counted on a "+N more" line. */
export const CHAINS_PER_PRIMARY_ASSET = 5;

/** One chain in words: CVE software, then the asset, the group it sits in, the primary asset and the role. */
export function chainText(primary: string, hit: PrimaryAssetHit, t_i18n: Translate): string {
  const steps: string[] = [];
  if (hit.software) steps.push(hit.software);
  if (hit.asset) steps.push(hit.asset);
  if (hit.through_kind === "asset_group" && hit.through) steps.push(t_i18n("group {group}", { group: hit.through }));
  const roles = [...new Set((hit.roles ?? []).filter(Boolean).map((role) => linkRoleLabel(role, t_i18n)))];
  steps.push(roles.length ? t_i18n("{primary} (role: {roles})", { primary, roles: roles.join(", ") }) : primary);
  return steps.join(" → ");
}

/**
 * The "Primary assets reached" row, one line per chain (at most five per
 * primary asset, then a "+N more" line), or the one line saying why none was
 * reached; no line when the record says nothing.
 */
export function primaryAssetsRow(decision: CtemDecisionRecord | null | undefined, t_i18n: Translate): string[] {
  const reached = primaryAssetsReached(decision);
  if (reached.length > 0) {
    return reached.flatMap((p) => {
      const lines = p.chains.slice(0, CHAINS_PER_PRIMARY_ASSET).map((hit) => chainText(p.name, hit, t_i18n));
      // #1979: the record's own count when it has one, never the chains it kept.
      const rest = Math.max(p.chains.length, p.chainTotal ?? 0) - Math.min(p.chains.length, CHAINS_PER_PRIMARY_ASSET);
      return rest > 0 ? [...lines, t_i18n("+{count} more", { count: rest })] : lines;
    });
  }
  if (decision?.reconcile?.inventory_links_read === true) return [t_i18n("None: no inventory link reaches a primary asset")];
  return [];
}

/** Where the scope threshold comes from: "policy", "default", or undefined on a record older than #1591 S3. */
export function thresholdSource(decision: CtemDecisionRecord | null | undefined): unknown {
  const scope: Record<string, unknown> | null | undefined = decision?.scope;
  return scope?.threshold_source;
}

/** The one threshold of the scope gate, with where it comes from. */
export function thresholdWithSource(threshold: string, source: unknown, t_i18n: Translate): string {
  if (!threshold) return "";
  if (source === "policy") return t_i18n("{threshold}, set by the prioritization policy", { threshold });
  if (source === "default") return t_i18n("{threshold}, scope default", { threshold });
  return threshold;
}
