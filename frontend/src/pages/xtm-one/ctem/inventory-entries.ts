// SPDX-License-Identifier: Apache-2.0

/**
 * Entity management of CTEM > Inventory (#1367, point 2; row APIs since #1543): add, update and delete the asset
 * records and asset groups the Inventory lists, one at a time from the row menu or in bulk from the selection. Each
 * write is one row API call addressed by the row's id, never the whole profile. A row a platform sync wrote stays
 * read-only by hand but for its local fields (`LOCAL_FIELDS`, the backend's `SOURCED_FIELDS`), and a synced row is never
 * deleted here: its sync owns it (exclude it from the sync instead). The backend refuses the same changes (422), so
 * this module only spares the person a refusal. Pure, so the page and its tests share it.
 */
import type { AssetGroupRow, AssetRow, GroupMembershipRule } from "@/lib/organization-profile";
import { LOCAL_FIELDS, isPlatformSource } from "@/lib/organization-provenance";

/** The lists the Inventory manages (the profile section names the backend's locks use). */
export type InventorySection = "assets_exposure" | "asset_groups";

/** The fields a person sets on an asset from the Inventory. */
export type AssetPatch = Partial<Pick<AssetRow, "name" | "category" | "subcategory" | "criticality" | "internet_facing" | "owner" | "environment" | "group">>;

/** The fields a person sets on an asset group from the Inventory. */
export type AssetGroupPatch = Partial<Pick<AssetGroupRow, "name" | "description" | "environment" | "internet_facing" | "owner" | "criticality" | "membership_rule">>;

/** The membership rule a form's three fields set (#1773): each list trimmed, each value once. */
export function membershipRuleOf(tags: readonly string[], categories: readonly string[], nameContains: string): GroupMembershipRule {
  const list = (values: readonly string[]) => [...new Set(values.map((value) => value.trim()).filter(Boolean))];
  return { tags: list(tags), categories: list(categories), name_contains: nameContains.trim() };
}

/** A row a write addresses: its id (an asset record or an asset group) and where it comes from. */
export interface RowRef {
  id: string;
  source?: string;
  external_id?: string;
}

/** Whether *row* is owned by a platform sync (read-only by hand but for its local fields). */
export function isSyncedRow(row: { source?: string; external_id?: string }): boolean {
  return isPlatformSource(row.source) && Boolean((row.external_id ?? "").trim());
}

/** Whether the field *key* of *row* of *section* takes a hand edit. */
export function fieldEditable(section: InventorySection, row: { source?: string; external_id?: string }, key: string): boolean {
  return !isSyncedRow(row) || (LOCAL_FIELDS[section] as readonly string[]).includes(key);
}

/** The asset records behind canonical assets (what a row write addresses); a ref of an older API without id is skipped. */
export function recordRefs(assets: readonly { records?: readonly { id?: string; source: string; external_id: string }[] }[]): RowRef[] {
  return assets.flatMap((a) => (a.records ?? []).flatMap((r) => (r.id ? [{ id: r.id, source: r.source, external_id: r.external_id }] : [])));
}

/** The patch of each row: a synced row takes only its local fields; a row where nothing applies counts as locked. */
export function rowPatches<P extends Record<string, unknown>>(
  section: InventorySection,
  rows: readonly RowRef[],
  patch: P,
): { patches: { id: string; patch: Partial<P> }[]; locked: number } {
  const keys = Object.keys(patch).filter((key) => patch[key] !== undefined);
  let locked = 0;
  const patches = rows.flatMap((row) => {
    const allowed = keys.filter((key) => fieldEditable(section, row, key));
    if (allowed.length === 0) {
      if (keys.length > 0) locked += 1;
      return [];
    }
    return [{ id: row.id, patch: Object.fromEntries(allowed.map((key) => [key, patch[key]])) as Partial<P> }];
  });
  return { patches, locked };
}

/** The rows a delete removes (a synced row stays: its sync owns it) and how many it leaves for their sync. */
export function deletableRows(rows: readonly RowRef[]): { ids: string[]; locked: number } {
  const ids = rows.filter((row) => !isSyncedRow(row)).map((row) => row.id);
  return { ids, locked: rows.length - ids.length };
}

/** The software a delete removes (#1729): the ones typed by hand; a synced one stays, its sources own it. */
export function softwareDeletable(rows: readonly { id: string; origin: string }[]): { ids: string[]; locked: number } {
  const ids = rows.filter((row) => row.origin === "manual").map((row) => row.id);
  return { ids, locked: rows.length - ids.length };
}

/** A new asset entered by hand, with the defaults for every field the dialog leaves out. */
export function newAssetRow(patch: AssetPatch): AssetRow {
  return {
    name: (patch.name ?? "").trim(),
    category: patch.category || "generic_asset",
    subcategory: patch.subcategory ?? "",
    criticality: patch.criticality || "unknown",
    environment: patch.environment ?? "",
    internet_facing: patch.internet_facing ?? "unknown",
    owner: (patch.owner ?? "").trim(),
    group: patch.group ?? "",
    products: [],
    tags: [],
    source: "manual",
    external_id: "",
  };
}

/** A new asset group entered by hand. */
export function newAssetGroupRow(patch: AssetGroupPatch): AssetGroupRow {
  return {
    name: (patch.name ?? "").trim(),
    description: (patch.description ?? "").trim(),
    environment: patch.environment ?? "",
    internet_facing: patch.internet_facing ?? "unknown",
    owner: (patch.owner ?? "").trim(),
    criticality: patch.criticality ?? "unknown",
    membership_rule: patch.membership_rule ?? { tags: [], categories: [], name_contains: "" },
    products: [],
    tags: [],
    source: "manual",
    external_id: "",
  };
}

/** Why *name* cannot be the name of a group (empty, or another group holds it), else undefined. */
export function nameProblem(rows: readonly { id: string; name: string }[], name: string, keepId?: string): "empty" | "taken" | undefined {
  const key = name.trim().toLowerCase();
  if (!key) return "empty";
  const taken = rows.some((row) => row.id !== keepId && row.name.trim().toLowerCase() === key);
  return taken ? "taken" : undefined;
}
