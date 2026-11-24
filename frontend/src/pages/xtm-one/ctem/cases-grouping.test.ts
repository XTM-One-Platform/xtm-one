// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { groupRows } from "@/lib/table-rows";
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { caseGroupLabel, caseGroupRank, caseGroupValue, readCaseGroupBy, type CaseGroupBy } from "./cases-grouping";

function kase(over: Partial<CtemCaseSummary>): CtemCaseSummary {
  return {
    id: "c",
    title: "Fix",
    remediation_kind: "upgrade",
    product_label: null,
    fixed_version: null,
    state: "new",
    state_since: null,
    owner_user_id: null,
    due_at: null,
    accepted_reason: null,
    accepted_until: null,
    priority: "low",
    priority_basis: "default policy",
    issue_count: 1,
    open_issue_count: 1,
    restricted_count: 0,
    asset_count: 1,
    crown_jewel_count: 0,
    kev_count: 0,
    max_cvss: null,
    max_epss: null,
    first_seen_at: null,
    created_at: "2026-11-22T12:00:00Z",
    details_unavailable: [],
    ...over,
  };
}

function grouped(key: Exclude<CaseGroupBy, "none">, rows: CtemCaseSummary[]): string[] {
  return groupRows(rows, { value: (c) => caseGroupValue(key, c), rank: caseGroupRank(key) }).map((c) => c.id);
}

describe("CTEM cases grouping (#1176)", () => {
  it("reads an unknown ?c_group= as no grouping", () => {
    expect(readCaseGroupBy(null)).toBe("none");
    expect(readCaseGroupBy("bogus")).toBe("none");
    expect(readCaseGroupBy("fix_family")).toBe("fix_family");
  });

  it("orders priorities critical first and keeps the sort inside a group", () => {
    const rows = [
      kase({ id: "a", priority: "low" }),
      kase({ id: "b", priority: "critical" }),
      kase({ id: "c", priority: "low" }),
      kase({ id: "d", priority: "critical" }),
    ];
    expect(grouped("priority", rows)).toEqual(["b", "d", "a", "c"]);
  });

  it("orders states in their life order", () => {
    const rows = [kase({ id: "a", state: "resolved" }), kase({ id: "b", state: "remediating" }), kase({ id: "c", state: "new" })];
    expect(grouped("state", rows)).toEqual(["c", "b", "a"]);
  });

  it("lists unowned cases last", () => {
    const rows = [kase({ id: "a", owner_user_id: null }), kase({ id: "b", owner_user_id: "u2" }), kase({ id: "c", owner_user_id: "u1" })];
    expect(grouped("owner", rows)).toEqual(["c", "b", "a"]);
  });

  it("groups by fix family and names each family", () => {
    const rows = [kase({ id: "a", remediation_kind: "upgrade" }), kase({ id: "b", remediation_kind: "config" }), kase({ id: "c", remediation_kind: "upgrade" })];
    expect(grouped("fix_family", rows)).toEqual(["b", "a", "c"]);
    expect(caseGroupLabel("fix_family", "config")).toBe("Configuration");
    expect(caseGroupLabel("priority", "critical")).toBe("Critical");
  });
});
