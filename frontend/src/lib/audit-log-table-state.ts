// SPDX-License-Identifier: Apache-2.0

/**
 * The URL state of an audit log table (#9378 slice 6y): the keys `/logs` always had
 * (`q`, `user`, `action`, `entity`, `dp`, `df`, `dt`, `page`) plus `sort`, `dir` and
 * `size`, each under a prefix (`""` on the admin page, `audit_` on a user's tab), so an
 * old link keeps its search, filters, range and page. A filter holds one value or several
 * comma-separated; the API takes the same form.
 */
import type { DataTableState } from "./data-table-state.ts";
import type { DateRange } from "./date-range.ts";

/** The columns the API sorts on (`GET /audit-logs?sort=`), newest first by default. */
const AUDIT_LOG_SORT_KEYS = ["time", "action", "entity", "user"] as const;
export const AUDIT_LOG_DEFAULT_SORT = { key: "time", direction: "desc" } as const;
export const AUDIT_LOG_DEFAULT_PAGE_SIZE = 25;

/** The table filter keys and their URL keys. */
const FILTER_KEYS = { action: "action", entity: "entity", user: "user" } as const;

export interface AuditLogParams {
  table: DataTableState;
  range: DateRange;
}

const values = (raw: string | null): string[] =>
  (raw ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);

/** The table state and the time range the URL holds under `prefix`. */
export function readAuditLogParams(params: URLSearchParams, prefix: string, fallbackPageSize: number): AuditLogParams {
  const get = (key: string) => params.get(prefix + key);
  const sortKey = get("sort");
  const known = (AUDIT_LOG_SORT_KEYS as readonly string[]).includes(sortKey ?? "");
  const direction = get("dir") === "asc" ? "asc" : get("dir") === "desc" ? "desc" : null;
  const size = Number(get("size"));
  const page = Number(get("page"));
  const filters: Record<string, string[]> = {};
  for (const [key, urlKey] of Object.entries(FILTER_KEYS)) {
    const selected = values(get(urlKey));
    if (selected.length > 0) filters[key] = selected;
  }
  return {
    table: {
      search: get("q") ?? "",
      sort: known
        ? { key: sortKey as string, direction: direction ?? "desc" }
        : { key: AUDIT_LOG_DEFAULT_SORT.key, direction: direction ?? AUDIT_LOG_DEFAULT_SORT.direction },
      page: Number.isInteger(page) && page > 0 ? page : 0,
      pageSize: Number.isInteger(size) && size > 0 ? size : fallbackPageSize,
      filters,
    },
    range: { preset: get("dp") ?? "", from: get("df") ?? "", to: get("dt") ?? "" },
  };
}

/**
 * `prev` with the table state `next` written under `prefix`: the default sort and page
 * size leave no key, and a new search, filter or page size starts again from page one.
 */
export function writeAuditLogTable(
  prev: URLSearchParams,
  prefix: string,
  current: DataTableState,
  next: DataTableState,
  defaultPageSize: number,
): URLSearchParams {
  const params = new URLSearchParams(prev);
  const set = (key: string, value: string | undefined) => (value ? params.set(prefix + key, value) : params.delete(prefix + key));
  set("q", next.search || undefined);
  for (const [key, urlKey] of Object.entries(FILTER_KEYS)) set(urlKey, (next.filters[key] ?? []).join(",") || undefined);
  const sortKey = next.sort && (AUDIT_LOG_SORT_KEYS as readonly string[]).includes(next.sort.key) ? next.sort.key : AUDIT_LOG_DEFAULT_SORT.key;
  const direction = next.sort?.direction ?? AUDIT_LOG_DEFAULT_SORT.direction;
  const isDefault = sortKey === AUDIT_LOG_DEFAULT_SORT.key && direction === AUDIT_LOG_DEFAULT_SORT.direction;
  set("sort", isDefault ? undefined : sortKey);
  set("dir", isDefault ? undefined : direction);
  set("size", next.pageSize === defaultPageSize ? undefined : String(next.pageSize));
  const narrowed =
    next.search !== current.search ||
    next.pageSize !== current.pageSize ||
    JSON.stringify(next.filters) !== JSON.stringify(current.filters);
  set("page", !narrowed && next.page > 0 ? String(next.page) : undefined);
  return params;
}

/** `prev` with the time range written under `prefix`, back to page one. */
export function writeAuditLogRange(prev: URLSearchParams, prefix: string, range: DateRange): URLSearchParams {
  const params = new URLSearchParams(prev);
  const set = (key: string, value: string) => (value ? params.set(prefix + key, value) : params.delete(prefix + key));
  set("dp", range.preset);
  set("df", range.from);
  set("dt", range.to);
  params.delete(prefix + "page");
  return params;
}

/** `prev` without any search, filter, range or page under `prefix`; the sort and page size stay. */
export function clearAuditLogFilters(prev: URLSearchParams, prefix: string): URLSearchParams {
  const params = new URLSearchParams(prev);
  for (const key of ["q", ...Object.values(FILTER_KEYS), "dp", "df", "dt", "page"]) params.delete(prefix + key);
  return params;
}
