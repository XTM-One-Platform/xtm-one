// SPDX-License-Identifier: Apache-2.0

/**
 * Entity management of CTEM > Inventory (#1367, point 2): add, update and delete the rows of the organization profile
 * the Inventory lists (assets, asset groups), one at a time from the row menu or in bulk from the selection. A row a
 * platform sync wrote stays read-only by hand but for its local fields (`LOCAL_FIELDS`, the backend's
 * `SOURCED_FIELDS`), and a synced row is never deleted here: its sync owns it (exclude it from the sync instead). The
 * backend refuses the same changes (`organization_context_locks.synced_edit_problems`), so this module only spares the
 * person a refusal. Pure, so the page and its tests share it.
 */
import type { AssetRecordRef } from "@/hooks/api/useOrganizationAssetsApi";
import type { AssetGroupRow, AssetRow } from "@/lib/organization-profile";
import { LOCAL_FIELDS, isPlatformSource } from "@/lib/organization-provenance";

/** The profile lists the Inventory manages. */
export type InventorySection = "assets_exposure" | "asset_groups";

/** The fields a person sets on an asset from the Inventory. */
export type AssetPatch = Partial<Pick<AssetRow, "name" | "category" | "subcategory" | "criticality" | "internet_facing" | "owner" | "environment" | "group">>;

/** The fields a person sets on an asset group from the Inventory. */
export type AssetGroupPatch = Partial<Pick<AssetGroupRow, "name" | "description" | "environment" | "internet_facing" | "owner">>;

/** The address the asset store gives a row typed by hand (no `external_id`): its position (`identity.section_record`). */
const positionalId = (index: number) => `row-${index}`;

/** Whether *row* is owned by a platform sync (read-only by hand but for its local fields). */
export function isSyncedRow(row: { source?: string; external_id?: string }): boolean {
  return isPlatformSource(row.source) && Boolean((row.external_id ?? "").trim());
}

/** Whether the field *key* of *row* of *section* takes a hand edit. */
export function fieldEditable(section: InventorySection, row: { source?: string; external_id?: string }, key: string): boolean {
  return !isSyncedRow(row) || (LOCAL_FIELDS[section] as readonly string[]).includes(key);
}

/**
 * The indexes in `assets_exposure` of the source records of one canonical asset. A record of a row typed by hand is
 * addressed by its position (`row-<index>`), as the store addresses it.
 */
export function assetRowIndexes(rows: readonly AssetRow[], records: readonly AssetRecordRef[]): number[] {
  const out: number[] = [];
  rows.forEach((row, index) => {
    const source = (row.source || "manual").trim().toLowerCase();
    const id = (row.external_id ?? "").trim() || positionalId(index);
    if (records.some((r) => r.source === source && r.external_id === id)) out.push(index);
  });
  return out;
}

/** The outcome of a change: the rows to save, how many rows it changed and how many it left for their sync. */
export interface EntryChange<R> {
  rows: R[];
  changed: number;
  /** Synced rows the change could not touch (deleted, or every field it sets locked). */
  locked: number;
}

/**
 * Apply *patch* to the rows at *indexes* of *section*: a synced row takes only its local fields. A row where nothing
 * applies counts as locked; a row whose values do not move counts as neither.
 */
export function patchRows<R extends { source?: string; external_id?: string }>(
  section: InventorySection,
  rows: readonly R[],
  indexes: readonly number[],
  patch: Partial<R>,
): EntryChange<R> {
  const wanted = new Set(indexes);
  let changed = 0;
  let locked = 0;
  const next = rows.map((row, index) => {
    if (!wanted.has(index)) return row;
    const keys = (Object.keys(patch) as (keyof R & string)[]).filter((key) => patch[key] !== undefined);
    const allowed = keys.filter((key) => fieldEditable(section, row, key));
    if (allowed.length === 0) {
      if (keys.length > 0) locked += 1;
      return row;
    }
    const moved = allowed.filter((key) => row[key] !== patch[key]);
    if (moved.length === 0) return row;
    changed += 1;
    return { ...row, ...Object.fromEntries(moved.map((key) => [key, patch[key]])) } as R;
  });
  return { rows: next, changed, locked };
}

/** Delete the rows at *indexes* of a section; a synced row stays (its sync owns it) and counts as locked. */
export function deleteRows<R extends { source?: string; external_id?: string }>(rows: readonly R[], indexes: readonly number[]): EntryChange<R> {
  const wanted = new Set(indexes);
  let locked = 0;
  const next = rows.filter((row, index) => {
    if (!wanted.has(index)) return true;
    if (isSyncedRow(row)) {
      locked += 1;
      return true;
    }
    return false;
  });
  return { rows: next, changed: rows.length - next.length, locked };
}

/** A new asset entered by hand, with the profile's defaults for every field the dialog leaves out. */
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
    tech_stack: [],
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
    tech_stack: [],
    tags: [],
    source: "manual",
    external_id: "",
  };
}

/** Why *name* cannot be a new row of *rows* (empty, or another row of the hand already holds it), else undefined. */
export function nameProblem(rows: readonly { name: string; source?: string }[], name: string, keepIndex?: number): "empty" | "taken" | undefined {
  const key = name.trim().toLowerCase();
  if (!key) return "empty";
  const taken = rows.some((row, index) => index !== keepIndex && !isPlatformSource(row.source) && row.name.trim().toLowerCase() === key);
  return taken ? "taken" : undefined;
}
