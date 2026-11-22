// SPDX-License-Identifier: Apache-2.0

/**
 * The state of a records table (search, column filters, sort, page, page size)
 * and its round trip through the URL: the pure half of `components/ui/data-table.tsx`,
 * so a link and the back button restore a filtered, sorted view.
 *
 * A table names its keys with a prefix (`urlKey`), so two tables on one page
 * keep separate states: `<prefix>q`, `<prefix>sort` (`key:asc`), `<prefix>page`
 * (1-based, absent on the first page), `<prefix>size` and one repeated
 * `<prefix>f_<filter>` per selected value. A value equal to its default is
 * never written, so an untouched table leaves the URL as it was.
 */
import type { SortDirection } from "./table-rows.ts";

export interface DataTableSort {
  key: string;
  direction: SortDirection;
}

export interface DataTableState {
  search: string;
  sort: DataTableSort | null;
  /** 0-based. */
  page: number;
  pageSize: number;
  /** Selected values per filter key; a key with no value is no filter. */
  filters: Record<string, string[]>;
}

export interface DataTableDefaults {
  sort: DataTableSort | null;
  pageSize: number;
}

/** The localStorage prefix of a table's per-user preferences (page size, hidden columns). */
export const TABLE_STORAGE_PREFIX = "copilot-table:";

/**
 * The page size the user kept for the table stored under `storageKey`, else `fallback`.
 * A server-paged page asks the API with it, so the fetch, the picker, the footer and the
 * rows agree after a reload (one source for the size, UX round 3 on #10980, B1).
 */
export function storedPageSize(storageKey: string | undefined, fallback: number): number {
  if (!storageKey || typeof localStorage === "undefined") return fallback;
  try {
    const raw = localStorage.getItem(TABLE_STORAGE_PREFIX + storageKey);
    const size = raw ? (JSON.parse(raw) as { pageSize?: unknown }).pageSize : undefined;
    return typeof size === "number" && Number.isInteger(size) && size > 0 ? size : fallback;
  } catch {
    return fallback;
  }
}

export function initialTableState(defaults: DataTableDefaults): DataTableState {
  return { search: "", sort: defaults.sort, page: 0, pageSize: defaults.pageSize, filters: {} };
}

/** How many filters narrow the rows (a filter with several values counts once). */
export function activeFilterCount(filters: Record<string, readonly string[]>): number {
  return Object.values(filters).filter((values) => values.length > 0).length;
}

/**
 * Whether a row passes every active filter: for each one, one of the row's
 * values is among the selected ones (OR inside a filter, AND across filters).
 */
export function matchesFilters(
  filters: Record<string, readonly string[]>,
  rowValues: (key: string) => readonly string[],
): boolean {
  return Object.entries(filters).every(([key, selected]) => {
    if (selected.length === 0) return true;
    const values = rowValues(key);
    return values.some((value) => selected.includes(value));
  });
}

function parseSort(raw: string | null): DataTableSort | null {
  if (!raw) return null;
  const index = raw.lastIndexOf(":");
  const key = index > 0 ? raw.slice(0, index) : raw;
  const direction = index > 0 ? raw.slice(index + 1) : "asc";
  if (!key || (direction !== "asc" && direction !== "desc")) return null;
  return { key, direction };
}

function sameSort(a: DataTableSort | null, b: DataTableSort | null): boolean {
  return a?.key === b?.key && a?.direction === b?.direction;
}

/** The state a table's keys of `params` describe, its defaults for the keys absent. */
export function readTableState(params: URLSearchParams, prefix: string, defaults: DataTableDefaults): DataTableState {
  const filterPrefix = `${prefix}f_`;
  const filters: Record<string, string[]> = {};
  for (const name of new Set(params.keys())) {
    if (!name.startsWith(filterPrefix)) continue;
    const values = params.getAll(name).filter(Boolean);
    if (values.length > 0) filters[name.slice(filterPrefix.length)] = values;
  }
  const page = Number(params.get(`${prefix}page`));
  const size = Number(params.get(`${prefix}size`));
  const sortParam = params.get(`${prefix}sort`);
  return {
    search: params.get(`${prefix}q`) ?? "",
    sort: sortParam === "none" ? null : (parseSort(sortParam) ?? defaults.sort),
    page: Number.isInteger(page) && page > 1 ? page - 1 : 0,
    pageSize: Number.isInteger(size) && size > 0 ? size : defaults.pageSize,
    filters,
  };
}

/** `params` with the table's keys replaced by `state`; the other keys are kept. */
export function writeTableState(
  params: URLSearchParams,
  prefix: string,
  state: DataTableState,
  defaults: DataTableDefaults,
): URLSearchParams {
  const next = new URLSearchParams(params);
  const filterPrefix = `${prefix}f_`;
  for (const name of [...new Set(next.keys())]) {
    if (name.startsWith(filterPrefix)) next.delete(name);
  }
  const set = (name: string, value: string | null) => {
    if (value) next.set(`${prefix}${name}`, value);
    else next.delete(`${prefix}${name}`);
  };
  set("q", state.search.trim() ? state.search : null);
  set("sort", sameSort(state.sort, defaults.sort) ? null : state.sort ? `${state.sort.key}:${state.sort.direction}` : "none");
  set("page", state.page > 0 ? String(state.page + 1) : null);
  set("size", state.pageSize !== defaults.pageSize ? String(state.pageSize) : null);
  for (const [key, values] of Object.entries(state.filters)) {
    for (const value of values) next.append(`${filterPrefix}${key}`, value);
  }
  return next;
}

/**
 * `params` with every filter of a page dropped in one write: the page's own filter keys
 * (`keys`: a status, a view, a fleet, an owner...), the table's search, column filters and
 * page under `prefix`, and the legacy unprefixed `q` and `page`. What is not a filter stays:
 * the sort, the page size, the visibility segment, an open item. A page whose filters
 * live outside the table passes this as the table's `onClearAll`, so "Clear all filters"
 * clears what its count counts (#1001: the count stayed and the button looked dead).
 */
export function clearFilterParams(params: URLSearchParams, keys: readonly string[], prefix = ""): URLSearchParams {
  const next = new URLSearchParams(params);
  const filterPrefix = `${prefix}f_`;
  for (const name of [...new Set(next.keys())]) {
    if (name.startsWith(filterPrefix)) next.delete(name);
  }
  for (const name of [...keys, `${prefix}q`, `${prefix}page`, "q", "page"]) next.delete(name);
  return next;
}
