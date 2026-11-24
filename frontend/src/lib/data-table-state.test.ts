import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  activeFilterCount,
  clearFilterParams,
  initialTableState,
  matchesFilters,
  readTableState,
  storedPageSize,
  writeTableState,
  type DataTableDefaults,
} from "./data-table-state.ts";

const defaults: DataTableDefaults = { sort: { key: "name", direction: "asc" }, pageSize: 25 };

describe("data table state in the URL", () => {
  it("clears the page's filter keys and the table's search, filters and page, keeps the rest", () => {
    const params = new URLSearchParams("view=waiting&fleet=f1&agent=a&status=blocked&wi_q=x&wi_f_kind=a&wi_page=2&q=old&page=3&wi_sort=due:asc&wi_size=50&vis=mine&item=i1");
    const next = clearFilterParams(params, ["view", "fleet", "agent", "status"], "wi_");
    assert.equal(next.toString(), "wi_sort=due%3Aasc&wi_size=50&vis=mine&item=i1");
    assert.equal(params.get("view"), "waiting", "the input is never changed");
  });

  it("writes nothing for an untouched table and keeps the other keys", () => {
    const params = new URLSearchParams("tab=usage");
    const next = writeTableState(params, "", initialTableState(defaults), defaults);
    assert.equal(next.toString(), "tab=usage");
  });

  it("round-trips search, sort, page, size and filters", () => {
    const state = {
      search: "mailbox",
      sort: { key: "cost", direction: "desc" as const },
      page: 2,
      pageSize: 50,
      filters: { status: ["active", "paused"], owner: ["me"] },
    };
    const written = writeTableState(new URLSearchParams(), "", state, defaults);
    assert.equal(written.get("q"), "mailbox");
    assert.equal(written.get("sort"), "cost:desc");
    assert.equal(written.get("page"), "3");
    assert.equal(written.get("size"), "50");
    assert.deepEqual(written.getAll("f_status"), ["active", "paused"]);
    assert.deepEqual(readTableState(written, "", defaults), state);
  });

  it("keeps two tables of one page apart by their prefix", () => {
    const one = writeTableState(new URLSearchParams(), "a_", { ...initialTableState(defaults), search: "x" }, defaults);
    const both = writeTableState(one, "b_", { ...initialTableState(defaults), search: "y" }, defaults);
    assert.equal(readTableState(both, "a_", defaults).search, "x");
    assert.equal(readTableState(both, "b_", defaults).search, "y");
  });

  it("removes a cleared filter and a cleared sort from the URL", () => {
    const params = new URLSearchParams("f_status=active&sort=cost:desc");
    const cleared = writeTableState(params, "", { ...initialTableState(defaults), sort: null }, defaults);
    assert.equal(cleared.getAll("f_status").length, 0);
    assert.equal(cleared.get("sort"), "none");
    assert.equal(readTableState(cleared, "", defaults).sort, null);
  });

  it("falls back to the defaults on a malformed value", () => {
    const state = readTableState(new URLSearchParams("sort=cost:sideways&page=-4&size=abc"), "", defaults);
    assert.deepEqual(state.sort, defaults.sort);
    assert.equal(state.page, 0);
    assert.equal(state.pageSize, 25);
  });
});

describe("stored page size", () => {
  it("falls back when the table keeps no preference", () => {
    assert.equal(storedPageSize(undefined, 25), 25);
    assert.equal(storedPageSize("no-such-table", 10), 10);
  });
});

describe("column filters", () => {
  const row: Record<string, string[]> = { status: ["active"], tags: ["cti", "soc"] };
  const values = (key: string) => row[key] ?? [];

  it("ORs the values of one filter and ANDs the filters", () => {
    assert.equal(matchesFilters({ status: ["active", "paused"] }, values), true);
    assert.equal(matchesFilters({ status: ["active"], tags: ["soc"] }, values), true);
    assert.equal(matchesFilters({ status: ["active"], tags: ["ot"] }, values), false);
  });

  it("ignores a filter with no value and counts only the active ones", () => {
    assert.equal(matchesFilters({ status: [] }, values), true);
    assert.equal(activeFilterCount({ status: [], tags: ["cti", "soc"], owner: ["me"] }), 2);
  });
});
