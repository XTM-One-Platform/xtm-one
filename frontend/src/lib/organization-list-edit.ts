// SPDX-License-Identifier: Apache-2.0

/**
 * The editing model of a list of the organization profile (primary assets, tech stack, AI
 * systems, assets, asset groups, accepted risks, enterprise knowledge; #12256): the rows of
 * an open edit with their state (new, edited, removed), the values Save writes, and the
 * checks a row "needs attention" for. Pure, so the page and its tests share it.
 *
 * A row keeps three values: `value` (what is typed), `base` (the value when the edit
 * started: the table sorts, searches and filters on it too, so a row never moves or
 * disappears under the cursor while typed in) and `original` (the saved value; null for a
 * row added in this edit).
 */

export interface EditRow<V> {
  key: string;
  value: V;
  base: V;
  original: V | null;
  removed: boolean;
}

export type RowState = "new" | "edited" | "removed" | "unchanged";

/** The catalogued reasons a row needs attention, translated where drawn. @i18n-keys */
export const LIST_ISSUES = {
  emptyName: "Empty name: the row is dropped at save",
  duplicate: "Same as another row",
  vendor: "Vendor is empty",
  cpe: "Not a valid CPE name",
  repository: "A repository is not owner/name",
} as const;

export type ListIssue = (typeof LIST_ISSUES)[keyof typeof LIST_ISSUES];

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** The rows of an edit opened on the saved *values*. */
export function startRows<V>(values: readonly V[]): EditRow<V>[] {
  return values.map((value, index) => ({ key: `r${index}`, value, base: value, original: value, removed: false }));
}

/** A new row, first in the list (the table pins new rows above the others). */
export function addRow<V>(rows: readonly EditRow<V>[], value: V): { rows: EditRow<V>[]; key: string } {
  const next = 1 + rows.reduce((max, row) => (row.key.startsWith("n") ? Math.max(max, Number(row.key.slice(1)) || 0) : max), 0);
  const key = `n${next}`;
  return { rows: [{ key, value, base: value, original: null, removed: false }, ...rows], key };
}

export function rowState<V>(row: EditRow<V>): RowState {
  if (row.original === null) return "new";
  if (row.removed) return "removed";
  return same(row.value, row.original) ? "unchanged" : "edited";
}

export function changeCounts<V>(rows: readonly EditRow<V>[]): { added: number; edited: number; removed: number } {
  const counts = { added: 0, edited: 0, removed: 0 };
  for (const row of rows) {
    const state = rowState(row);
    if (state === "new") counts.added += 1;
    else if (state === "edited") counts.edited += 1;
    else if (state === "removed") counts.removed += 1;
  }
  return counts;
}

/** What Save writes: the saved rows in their order (removed ones left out), then the added ones in the order they were added. */
export function committedValues<V>(rows: readonly EditRow<V>[]): V[] {
  const kept = rows.filter((row) => row.original !== null && !row.removed).map((row) => row.value);
  const added = rows.filter((row) => row.original === null).map((row) => row.value).reverse();
  return [...kept, ...added];
}

export function patchRow<V>(rows: readonly EditRow<V>[], key: string, patch: Partial<V>): EditRow<V>[] {
  return rows.map((row) => (row.key === key ? { ...row, value: { ...row.value, ...patch } } : row));
}

/** Sets a row back to *value* (Esc on an open row: the value it had when opened). */
export function setRowValue<V>(rows: readonly EditRow<V>[], key: string, value: V): EditRow<V>[] {
  return rows.map((row) => (row.key === key ? { ...row, value } : row));
}

/** A saved row is marked removed (struck through, Undo until Save); a row added in this edit goes at once. */
export function removeRow<V>(rows: readonly EditRow<V>[], key: string): EditRow<V>[] {
  return rows.flatMap((row) => (row.key !== key ? [row] : row.original === null ? [] : [{ ...row, removed: true }]));
}

export function undoRemove<V>(rows: readonly EditRow<V>[], key: string): EditRow<V>[] {
  return rows.map((row) => (row.key === key ? { ...row, removed: false } : row));
}

/** New rows for *values*, added in that order (each pinned first, written after the saved rows in the order given). */
export function addRows<V>(rows: readonly EditRow<V>[], values: readonly V[]): EditRow<V>[] {
  return values.reduce<EditRow<V>[]>((next, value) => addRow(next, value).rows, [...rows]);
}

/**
 * Bulk edit (#12256 slice 2): *patch* set on every row of *keys* that is not removed. A row
 * *locked* on one of the patched fields (a synced row's sourced field, #1134) is left as it is,
 * as its open row keeps that field as text.
 */
export function patchRows<V>(
  rows: readonly EditRow<V>[],
  keys: readonly string[],
  patch: Partial<V>,
  locked?: (value: V, field: string) => boolean,
): EditRow<V>[] {
  const wanted = new Set(keys);
  const fields = Object.keys(patch);
  const takes = (row: EditRow<V>) => wanted.has(row.key) && !row.removed && !(locked && fields.some((field) => locked(row.value, field)));
  return rows.map((row) => (takes(row) ? { ...row, value: { ...row.value, ...patch } } : row));
}

/** Bulk remove: each row of *keys* removed as `removeRow` does (saved rows struck through with Undo, added rows gone). */
export function removeRows<V>(rows: readonly EditRow<V>[], keys: readonly string[]): EditRow<V>[] {
  const wanted = new Set(keys);
  return rows.flatMap((row) => (!wanted.has(row.key) ? [row] : row.original === null ? [] : [{ ...row, removed: true }]));
}

/**
 * The distinct non-empty values of *field* across the rows (removed ones left out), most used
 * first then alphabetical: the suggestions of a free-text field (vendor, owner), so a long list
 * stays consistent.
 */
export function fieldSuggestions<V>(rows: readonly EditRow<V>[], field: (value: V) => string): string[] {
  const counts = new Map<string, { text: string; count: number }>();
  for (const row of rows) {
    if (row.removed) continue;
    const text = field(row.value).trim();
    if (!text) continue;
    const key = text.toLowerCase();
    const entry = counts.get(key);
    counts.set(key, { text: entry?.text ?? text, count: (entry?.count ?? 0) + 1 });
  }
  return [...counts.values()].sort((a, b) => b.count - a.count || a.text.localeCompare(b.text)).map((entry) => entry.text);
}

/** A CPE name: 2.3 formatted string (13 parts) or the 2.2 URI form; empty is valid (optional). */
export function isCpe(value: string): boolean {
  const text = value.trim();
  if (!text) return true;
  if (/^cpe:\/[aho]:[^:\s]+(:[^:\s]*){0,5}$/i.test(text)) return true;
  if (/\s/.test(text)) return false;
  const parts = text.split(/(?<!\\):/);
  return parts.length === 13 && parts[0].toLowerCase() === "cpe" && parts[1] === "2.3" && /^[aho*-]$/.test(parts[2]) && parts.slice(3).every(Boolean);
}

/**
 * A source repository as `owner/name` (#1432), a GitHub address read as its `owner/name` as the backend does;
 * empty is not one.
 */
export function isRepository(value: string): boolean {
  const text = value.trim().replace(/^(https?:\/\/)?github\.com\//i, "").replace(/\.git$/i, "").replace(/^\/+|\/+$/g, "");
  return text.length <= 200 && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(text);
}

const norm = (text: string) => text.trim().toLowerCase();

/**
 * The reasons each row needs attention, by row key: the row's own checks, plus "Same as
 * another row" when its identity (trimmed, case-insensitive, never empty) is shared by
 * another row kept. Removed rows are never flagged.
 */
export function rowIssues<V>(
  rows: readonly EditRow<V>[],
  check: (value: V, all: readonly V[]) => ListIssue[],
  identity?: (value: V) => string,
): Map<string, ListIssue[]> {
  const kept = rows.filter((row) => !row.removed);
  const values = kept.map((row) => row.value);
  const seen = new Map<string, number>();
  if (identity) {
    for (const row of kept) {
      const id = norm(identity(row.value));
      if (id) seen.set(id, (seen.get(id) ?? 0) + 1);
    }
  }
  const issues = new Map<string, ListIssue[]>();
  for (const row of kept) {
    const found = [...check(row.value, values)];
    if (identity && (seen.get(norm(identity(row.value))) ?? 0) > 1) found.push(LIST_ISSUES.duplicate);
    if (found.length) issues.set(row.key, found);
  }
  return issues;
}

/** How many saved rows need attention (the count on the card header in read mode). */
export function attentionCount<V>(values: readonly V[], check: (value: V, all: readonly V[]) => ListIssue[], identity?: (value: V) => string): number {
  return rowIssues(startRows(values), check, identity).size;
}

type Jewel = { name: string; business_service: string; owner: string; criticality: string };
type Tech = { product: string; vendor: string; cpe: string };
type Shipped = { product: string; vendor: string; cpe: string; repositories: readonly string[] };

/** The checks and the identity of each list of the identity half of the profile. */
export const LIST_RULES = {
  crown_jewels: {
    check: (value: Jewel): ListIssue[] => (value.name.trim() ? [] : [LIST_ISSUES.emptyName]),
    identity: (value: Jewel) => value.name.trim(),
  },
  tech_stack: {
    check: (value: Tech): ListIssue[] => [
      ...(value.product.trim() ? [] : [LIST_ISSUES.emptyName]),
      ...(value.product.trim() && !value.vendor.trim() ? [LIST_ISSUES.vendor] : []),
      ...(isCpe(value.cpe) ? [] : [LIST_ISSUES.cpe]),
    ],
    identity: (value: Tech) => (value.product.trim() ? `${value.product.trim()}\u0000${value.vendor.trim()}` : ""),
  },
  // What the organization ships (#1432): identified as the tech stack is, by product and vendor.
  shipped_products: {
    check: (value: Shipped): ListIssue[] => [
      ...(value.product.trim() ? [] : [LIST_ISSUES.emptyName]),
      ...(isCpe(value.cpe) ? [] : [LIST_ISSUES.cpe]),
      ...(value.repositories.every(isRepository) ? [] : [LIST_ISSUES.repository]),
    ],
    identity: (value: Shipped) => (value.product.trim() ? `${value.product.trim()}\u0000${value.vendor.trim()}` : ""),
  },
} as const;

/** Whether *value* matches the search *words* on the fields of either its live or its base value (an open row never drops out). */
export function rowSearchFields<V>(row: EditRow<V>, fields: (value: V) => readonly (string | null | undefined)[]): (string | null | undefined)[] {
  return row.value === row.base ? [...fields(row.value)] : [...fields(row.value), ...fields(row.base)];
}
