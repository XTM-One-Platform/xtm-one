/** `yarn test:unit`: sorting, searching and paging a table's rows. */

import assert from "node:assert/strict";
import { test } from "node:test";

import { matchesSearch, pageOf, sortRows } from "./table-rows.ts";

const ROWS = [
  { name: "Agent 10", cost: 3 },
  { name: "agent 2", cost: null },
  { name: "Agent 1", cost: 7 },
];

test("numbers sort by value, text naturally and case-insensitively", () => {
  assert.deepEqual(sortRows(ROWS, (row) => row.cost, "desc").map((row) => row.name), ["Agent 1", "Agent 10", "agent 2"]);
  assert.deepEqual(sortRows(ROWS, (row) => row.name, "asc").map((row) => row.name), ["Agent 1", "agent 2", "Agent 10"]);
});

test("empty values stay last whatever the direction", () => {
  const last = (rows: typeof ROWS) => rows[rows.length - 1]?.name;
  assert.equal(last(sortRows(ROWS, (row) => row.cost, "asc")), "agent 2");
  assert.equal(last(sortRows(ROWS, (row) => row.cost, "desc")), "agent 2");
});

test("ties keep the incoming order", () => {
  const rows = [{ k: "b", v: 1 }, { k: "a", v: 1 }];
  assert.deepEqual(sortRows(rows, (row) => row.v, "desc").map((row) => row.k), ["b", "a"]);
});

test("every word of the search must appear in one of the fields", () => {
  assert.equal(matchesSearch(["Support Bot", "support@example.com"], "bot support"), true);
  assert.equal(matchesSearch(["Support Bot", null], "sales"), false);
  assert.equal(matchesSearch(["anything"], "   "), true);
});

test("a page past the rows comes back as the last one", () => {
  const rows = Array.from({ length: 23 }, (_, index) => index);
  assert.deepEqual(pageOf(rows, 0, 10).rows, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
  const last = pageOf(rows, 9, 10);
  assert.equal(last.page, 2);
  assert.equal(last.lastPage, 2);
  assert.deepEqual(last.rows, [20, 21, 22]);
  assert.deepEqual(pageOf([], 3, 10), { rows: [], page: 0, lastPage: 0 });
});
