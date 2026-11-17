/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Page arithmetic of the Trace Explorer lists (Traces, Trace Sessions).
 *
 * `GET /traces` and `GET /traces/sessions` refuse an `offset` past
 * `TRACE_LIST_MAX_OFFSET`: the database reads and discards every row
 * it skips. A page past that bound is read from the other end of the order
 * instead. `reverse=true` mirrors the ORDER BY exactly - direction,
 * NULL placement and tie-breaker - so the page that starts `page * limit` rows
 * from the first row is the one that ends `total - (page + 1) * limit` rows
 * before the last, and its rows are put back in display order. A page deeper
 * than the bound from both ends is never requested.
 */

/** Deepest `offset` the trace lists accept (`TRACE_LIST_MAX_OFFSET` in `backend/app/services/tracing/read_store.py`). */
export const TRACE_LIST_MAX_OFFSET = 100_000;

/** How one page of a trace list is read. */
export type TraceListPagePlan =
  /** By the keyset cursor the newest-first walk recorded (the first page needs none). */
  | { kind: "cursor" }
  /** By offset from the first row, within the bound. */
  | { kind: "offset"; offset: number; limit: number }
  /** In the mirrored order, `offset` rows from the last row; the rows are reversed back for display. */
  | { kind: "from-end"; offset: number; limit: number }
  /** Past the bound from the first row, and no count yet to plan the far end from. */
  | { kind: "count-first" }
  /** Past the bound from both ends: never requested. */
  | { kind: "out-of-reach" }
  /** Starts after the last row: nothing to read. */
  | { kind: "past-end" };

interface TraceListPageInput {
  /** Zero-based page index. */
  page: number;
  /** Rows per page, at least 1. */
  limit: number;
  /** Rows matching the filters, or `null` while none was counted for them. */
  total: number | null;
  /** Whether the newest-first keyset walk holds this page's cursor. */
  hasCursor: boolean;
  /** The API's offset ceiling. */
  maxOffset?: number;
}

export function planTraceListPage({
  page,
  limit,
  total,
  hasCursor,
  maxOffset = TRACE_LIST_MAX_OFFSET,
}: TraceListPageInput): TraceListPagePlan {
  if (hasCursor) return { kind: "cursor" };
  const offset = page * limit;
  if (offset <= maxOffset) return { kind: "offset", offset, limit };
  if (total === null) return { kind: "count-first" };
  if (offset >= total) return { kind: "past-end" };
  // A short last page has no row after it; a full page has `total - (page + 1) * limit`.
  const rows = Math.min(limit, total - offset);
  const fromEnd = total - offset - rows;
  if (fromEnd > maxOffset) return { kind: "out-of-reach" };
  return { kind: "from-end", offset: fromEnd, limit: rows };
}

/** Whether the plan reads rows from the API. */
export function readsRows(plan: TraceListPagePlan): boolean {
  return plan.kind === "cursor" || plan.kind === "offset" || plan.kind === "from-end";
}

/**
 * Whether the page is counted from the first row, as the keyset walk counts:
 * read by cursor or by offset. Only such a page may plan the pages after it
 * with the count it carries - a far-end read is itself planned from the count,
 * so adopting its own would re-plan it whenever the store moved, one request
 * after another on a busy install - or hand the walk a cursor.
 */
export function readsFromFirstRow(plan: TraceListPagePlan): boolean {
  return plan.kind === "cursor" || plan.kind === "offset";
}

/**
 * Whether the page shown gives the keyset walk the cursor of the page after
 * it. A far-end page is counted from the last row, with a count that may have
 * moved since, so its last row need not end the page the walk numbers the
 * same. And the last page has no page after it: a cursor past the oldest row
 * would read an empty page once the list grows.
 */
export function recordsNextCursor(plan: TraceListPagePlan, page: number, limit: number, total: number): boolean {
  return readsFromFirstRow(plan) && (page + 1) * limit < total;
}

/** The `offset`, `limit` and `reverse` a plan sends. */
export function pageRequest(
  plan: TraceListPagePlan,
  page: number,
  limit: number,
): { offset?: number; limit: number; reverse?: true } {
  switch (plan.kind) {
    case "cursor":
      return { limit };
    case "offset":
      return { offset: plan.offset, limit };
    case "from-end":
      return { offset: plan.offset, limit: plan.limit, reverse: true };
    default:
      // Never sent: the page's own offset keeps its query apart from every page that is.
      return { offset: page * limit, limit };
  }
}

/** The rows of a response in display order. */
export function pageRows<T>(plan: TraceListPagePlan, items: readonly T[] | undefined): readonly T[] {
  if (!items || !readsRows(plan)) return [];
  return plan.kind === "from-end" ? [...items].reverse() : items;
}

/**
 * The total the pagination bar shows. A far-end page shows the count it was
 * planned from, so its page number matches its rows; a page read from the
 * first row shows the count that came with it.
 */
export function displayedTotal(
  plan: TraceListPagePlan,
  planningTotal: number | null,
  responseTotal: number | undefined,
): number {
  if (readsFromFirstRow(plan) && responseTotal !== undefined) return responseTotal;
  return planningTotal ?? responseTotal ?? 0;
}
