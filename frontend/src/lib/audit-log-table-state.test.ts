import { describe, expect, it } from "vitest";
import {
  clearAuditLogFilters,
  readAuditLogParams,
  writeAuditLogRange,
  writeAuditLogTable,
} from "./audit-log-table-state";

const params = (query: string) => new URLSearchParams(query);

describe("audit log table URL state (#9378 slice 6y)", () => {
  it("reads the keys /logs always had", () => {
    const { table, range } = readAuditLogParams(params("q=agent&user=u1&action=create&entity=agent&dp=7d&page=2"), "", 25);
    expect(table).toEqual({
      search: "agent",
      sort: { key: "time", direction: "desc" },
      page: 2,
      pageSize: 25,
      filters: { user: ["u1"], action: ["create"], entity: ["agent"] },
    });
    expect(range).toEqual({ preset: "7d", from: "", to: "" });
  });

  it("reads several values, a known sort and a page size under a prefix", () => {
    const { table } = readAuditLogParams(params("audit_action=create,delete&audit_sort=user&audit_dir=asc&audit_size=50&action=x"), "audit_", 25);
    expect(table.filters).toEqual({ action: ["create", "delete"] });
    expect(table.sort).toEqual({ key: "user", direction: "asc" });
    expect(table.pageSize).toBe(50);
  });

  it("falls back to newest first on an unknown sort", () => {
    expect(readAuditLogParams(params("sort=summary"), "", 25).table.sort).toEqual({ key: "time", direction: "desc" });
  });

  it("writes no key for the default sort and size, and goes back to page one on a new filter", () => {
    const current = readAuditLogParams(params("page=3"), "", 25).table;
    const next = { ...current, filters: { action: ["create", "delete"] } };
    expect(writeAuditLogTable(params("page=3"), "", current, next, 25).toString()).toBe("action=create%2Cdelete");
  });

  it("keeps the page on a page change and writes a non-default sort", () => {
    const current = readAuditLogParams(params(""), "", 25).table;
    const next = { ...current, page: 4, sort: { key: "action", direction: "asc" as const } };
    const written = writeAuditLogTable(params(""), "", current, next, 25);
    expect(written.get("page")).toBe("4");
    expect(written.get("sort")).toBe("action");
    expect(written.get("dir")).toBe("asc");
  });

  it("writes a range back to page one and clears the filters but not the sort", () => {
    expect(writeAuditLogRange(params("page=2"), "", { preset: "", from: "2026-11-01", to: "" }).toString()).toBe("df=2026-11-01");
    expect(clearAuditLogFilters(params("q=x&user=u&dp=7d&page=2&sort=user&size=50"), "").toString()).toBe("sort=user&size=50");
  });
});
