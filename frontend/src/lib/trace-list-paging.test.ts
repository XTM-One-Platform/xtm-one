/**
 * Unit tests for the Trace Explorer page arithmetic. Run with `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  TRACE_LIST_MAX_OFFSET,
  displayedTotal,
  pageRequest,
  pageRows,
  planTraceListPage,
  readsFromFirstRow,
  readsRows,
  recordsNextCursor,
  type TraceListPagePlan,
} from "./trace-list-paging.ts";

const LIMIT = 50;

function plan(page: number, total: number | null, hasCursor = false): TraceListPagePlan {
  return planTraceListPage({ page, limit: LIMIT, total, hasCursor });
}

test("a page the keyset walk holds a cursor for is read by that cursor", () => {
  assert.deepEqual(plan(0, null, true), { kind: "cursor" });
  assert.deepEqual(plan(9_000, 10_000_000, true), { kind: "cursor" });
});

test("a page within the bound is read by offset, the bound itself included", () => {
  assert.deepEqual(plan(0, null), { kind: "offset", offset: 0, limit: LIMIT });
  assert.deepEqual(plan(3, 120), { kind: "offset", offset: 150, limit: LIMIT });
  assert.equal(TRACE_LIST_MAX_OFFSET / LIMIT, 2_000);
  assert.deepEqual(plan(2_000, 10_000_000), { kind: "offset", offset: TRACE_LIST_MAX_OFFSET, limit: LIMIT });
});

test("a page past the bound waits for a count before anything is requested", () => {
  assert.deepEqual(plan(2_001, null), { kind: "count-first" });
});

test("the last page of a huge store is read from the end, a short one with its own row count", () => {
  // 1,000,001 rows: the last page holds the single last row.
  assert.deepEqual(plan(20_000, 1_000_001), { kind: "from-end", offset: 0, limit: 1 });
  // 1,000,000 rows: the last page is a full one.
  assert.deepEqual(plan(19_999, 1_000_000), { kind: "from-end", offset: 0, limit: LIMIT });
  // The page before it ends one page before the last row.
  assert.deepEqual(plan(19_998, 1_000_000), { kind: "from-end", offset: 50, limit: LIMIT });
});

test("the far end is bounded too: a page deeper than the bound from both ends is out of reach", () => {
  const total = 1_000_000;
  // Page 17,999 ends exactly TRACE_LIST_MAX_OFFSET rows before the last one.
  assert.deepEqual(plan(17_999, total), { kind: "from-end", offset: TRACE_LIST_MAX_OFFSET, limit: LIMIT });
  assert.deepEqual(plan(17_998, total), { kind: "out-of-reach" });
  assert.deepEqual(plan(2_001, total), { kind: "out-of-reach" });
  assert.deepEqual(plan(10_000, total), { kind: "out-of-reach" });
});

test("a page past the last row is known to be empty and is not requested", () => {
  assert.deepEqual(plan(3_000, 120_000), { kind: "past-end" });
  assert.deepEqual(plan(2_001, 0), { kind: "past-end" });
});

test("every page read from either end holds exactly the rows of the page counted from the first row", () => {
  for (let maxOffset = 0; maxOffset <= 12; maxOffset += 1) {
    for (let limit = 1; limit <= 6; limit += 1) {
      for (let total = 0; total <= 40; total += 1) {
        const rows = Array.from({ length: total }, (_, index) => index);
        const mirrored = [...rows].reverse();
        const pages = Math.max(1, Math.ceil(total / limit));
        for (let page = 0; page < pages + 2; page += 1) {
          const expected = rows.slice(page * limit, (page + 1) * limit);
          const planned = planTraceListPage({ page, limit, total, hasCursor: false, maxOffset });
          const context = JSON.stringify({ maxOffset, limit, total, page, planned });
          switch (planned.kind) {
            case "offset":
              assert.ok(planned.offset <= maxOffset, context);
              assert.deepEqual(rows.slice(planned.offset, planned.offset + planned.limit), expected, context);
              break;
            case "from-end": {
              assert.ok(planned.offset <= maxOffset, context);
              const read = mirrored.slice(planned.offset, planned.offset + planned.limit);
              assert.deepEqual([...pageRows(planned, read)], expected, context);
              break;
            }
            case "past-end":
              assert.deepEqual(expected, [], context);
              break;
            case "out-of-reach":
              // Only when both ends are past the bound.
              assert.ok(page * limit > maxOffset, context);
              assert.ok(total - (page + 1) * limit > maxOffset, context);
              break;
            default:
              assert.fail(`unexpected plan ${context}`);
          }
        }
      }
    }
  }
});

test("the request of each plan: a far-end read says so, a page never read keeps its own offset", () => {
  assert.deepEqual(pageRequest({ kind: "cursor" }, 4, LIMIT), { limit: LIMIT });
  assert.deepEqual(pageRequest({ kind: "offset", offset: 150, limit: LIMIT }, 3, LIMIT), { offset: 150, limit: LIMIT });
  assert.deepEqual(pageRequest({ kind: "from-end", offset: 0, limit: 7 }, 20_000, LIMIT), {
    offset: 0,
    limit: 7,
    reverse: true,
  });
  assert.deepEqual(pageRequest({ kind: "out-of-reach" }, 5_000, LIMIT), { offset: 250_000, limit: LIMIT });
  assert.deepEqual(pageRequest({ kind: "count-first" }, 2_001, LIMIT), { offset: 100_050, limit: LIMIT });
});

test("rows read from the end are put back in display order, and a page never read has none", () => {
  assert.deepEqual(pageRows({ kind: "from-end", offset: 0, limit: 3 }, ["c", "b", "a"]), ["a", "b", "c"]);
  assert.deepEqual(pageRows({ kind: "offset", offset: 0, limit: 3 }, ["a", "b", "c"]), ["a", "b", "c"]);
  assert.deepEqual(pageRows({ kind: "cursor" }, undefined), []);
  // A disabled query can still hold cached rows for its key: they are not this page's.
  assert.deepEqual(pageRows({ kind: "out-of-reach" }, ["a"]), []);
  assert.deepEqual(pageRows({ kind: "count-first" }, ["a"]), []);
});

test("only a page read from the first row may re-plan the pages after it", () => {
  assert.equal(readsFromFirstRow({ kind: "cursor" }), true);
  assert.equal(readsFromFirstRow({ kind: "offset", offset: 0, limit: LIMIT }), true);
  assert.equal(readsFromFirstRow({ kind: "from-end", offset: 0, limit: LIMIT }), false);
  assert.equal(readsRows({ kind: "from-end", offset: 0, limit: LIMIT }), true);
  assert.equal(readsRows({ kind: "out-of-reach" }), false);
});

test("only a page counted from the first row, with a page after it, hands the keyset walk a cursor", () => {
  assert.equal(recordsNextCursor({ kind: "cursor" }, 0, LIMIT, 120), true);
  assert.equal(recordsNextCursor({ kind: "offset", offset: 50, limit: LIMIT }, 1, LIMIT, 120), true);
  // Page 3 of 3 has no page after it: a cursor past the oldest row reads nothing.
  assert.equal(recordsNextCursor({ kind: "offset", offset: 100, limit: LIMIT }, 2, LIMIT, 120), false);
  assert.equal(recordsNextCursor({ kind: "cursor" }, 1, LIMIT, 100), false);
  // A far-end page is counted from the last row: its last row is not the walk's.
  assert.equal(recordsNextCursor({ kind: "from-end", offset: 50, limit: LIMIT }, 19_998, LIMIT, 1_000_000), false);
});

test("a far-end page shows the count it was planned from, so its number matches its rows", () => {
  const fromEnd: TraceListPagePlan = { kind: "from-end", offset: 0, limit: LIMIT };
  // The store grew by 20 rows between the count and the far-end read.
  assert.equal(displayedTotal(fromEnd, 1_000_000, 1_000_020), 1_000_000);
  assert.equal(displayedTotal({ kind: "offset", offset: 0, limit: LIMIT }, 1_000_000, 1_000_020), 1_000_020);
  assert.equal(displayedTotal({ kind: "cursor" }, 900, undefined), 900);
  assert.equal(displayedTotal({ kind: "out-of-reach" }, 1_000_000, undefined), 1_000_000);
  assert.equal(displayedTotal({ kind: "count-first" }, null, undefined), 0);
});
