// SPDX-License-Identifier: Apache-2.0

/**
 * Sorting, searching and paging the rows a table already holds: the ONE copy
 * shared by `EntityTable` (asset index pages) and `DataTable` (LLM Analytics
 * breakdowns). A server-paged table sorts in SQL instead
 * (`TableSortHeader` + the API's `sort_by`).
 */

/** Values a column can sort on. Empty values always sort last. */
export type SortValue = string | number | boolean | null | undefined;

export type SortDirection = "asc" | "desc";

function isEmptySortValue(value: SortValue): boolean {
  return value === null || value === undefined || value === "" || (typeof value === "number" && Number.isNaN(value));
}

function compare(a: SortValue, b: SortValue): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") {
    return a === b ? 0 : a ? -1 : 1;
  }
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

/**
 * *rows* ordered by *getValue*, stable (ties keep the incoming order). Empty
 * values stay at the bottom in both directions: flipping them to the top on a
 * descending sort would bury the rows asked for.
 */
export function sortRows<T>(rows: readonly T[], getValue: (row: T) => SortValue, direction: SortDirection): T[] {
  const factor = direction === "asc" ? 1 : -1;
  return [...rows].sort((left, right) => {
    const a = getValue(left);
    const b = getValue(right);
    if (isEmptySortValue(a) || isEmptySortValue(b)) {
      if (isEmptySortValue(a) && isEmptySortValue(b)) return 0;
      return isEmptySortValue(a) ? 1 : -1;
    }
    return factor * compare(a, b);
  });
}

/** Whether every word of *query* appears in one of *fields*, case-insensitively. */
export function matchesSearch(fields: readonly (string | null | undefined)[], query: string): boolean {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const haystack = fields.filter(Boolean).join(" \u0000 ").toLowerCase();
  return words.every((word) => haystack.includes(word));
}

export interface RowPage<T> {
  rows: T[];
  /** The page shown: the one asked for, brought back within the rows. */
  page: number;
  lastPage: number;
}

export function pageOf<T>(rows: readonly T[], page: number, pageSize: number): RowPage<T> {
  const size = Math.max(1, Math.floor(pageSize));
  const lastPage = Math.max(0, Math.ceil(rows.length / size) - 1);
  const current = Math.min(Math.max(0, Math.floor(page)), lastPage);
  return { rows: rows.slice(current * size, (current + 1) * size), page: current, lastPage };
}
