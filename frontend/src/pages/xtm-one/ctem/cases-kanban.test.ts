// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { caseViewPath, kanbanColumnKeys, kanbanColumns, kanbanDropRefusal, kanbanDropUpdate, readCaseView } from "./cases-kanban";
import { caseGroupLabel, caseGroupRank } from "./cases-grouping";

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

describe("CTEM cases Kanban (#1503)", () => {
  it("reads the view from the path, never a query (#10203)", () => {
    expect(readCaseView("/ctem/cases")).toBe("list");
    expect(readCaseView("/ctem/cases/kanban")).toBe("kanban");
    expect(readCaseView("/ctem/cases/kanban/")).toBe("kanban");
    expect(caseViewPath("kanban")).toBe("/ctem/cases/kanban");
    expect(caseViewPath("list")).toBe("/ctem/cases");
  });

  it("lays every status out in lifecycle order, empty columns included", () => {
    const columns = kanbanColumns("state", [kase({ id: "a", state: "validating" }), kase({ id: "b", state: "new" })]);
    expect([...columns.keys()]).toEqual([
      "new",
      "analyzing",
      "waiting_decision",
      "remediating",
      "validating",
      "mitigated_watching",
      "resolved",
      "accepted_risk",
    ]);
    expect(columns.get("validating")?.map((c) => c.id)).toEqual(["a"]);
    expect(columns.get("analyzing")).toEqual([]);
  });

  it("lays the CTEM phases out in order, a last column only for cases with no phase", () => {
    expect(kanbanColumnKeys("phase", [kase({ phase: "validation" })])).toEqual(["scoping", "discovery", "prioritization", "validation", "remediation"]);
    const columns = kanbanColumns("phase", [kase({ id: "a", phase: "validation" }), kase({ id: "b", phase: null })]);
    const keys = [...columns.keys()];
    expect(keys[keys.length - 1]).toBe("");
    expect(columns.get("")?.map((c) => c.id)).toEqual(["b"]);
    expect(caseGroupLabel("phase", "remediation")).toBe("Mobilization");
    expect(caseGroupLabel("phase", "")).toBe("Phase not known yet");
    expect(caseGroupRank("phase")?.("")).toBe(5);
  });

  it("refuses the drops a person cannot make from the board, with the reason", () => {
    const c = kase({ state: "analyzing" });
    expect(kanbanDropRefusal(c, "resolved", "me")).toMatch(/evidence/);
    expect(kanbanDropRefusal(c, "accepted_risk", "me")).toMatch(/reason and an expiry/);
    expect(kanbanDropRefusal(c, "waiting_decision", "me")).toMatch(/waits on/);
    expect(kanbanDropRefusal(c, "mitigated_watching", "me")).toMatch(/validation/);
    expect(kanbanDropRefusal(c, "remediating", null)).toMatch(/owner/);
    expect(kanbanDropRefusal(c, "validating", null)).toBeNull();
    expect(kanbanDropRefusal(c, "analyzing", null)).toBeNull();
  });

  it("takes the person dropping as owner of an unowned case moved to remediating", () => {
    expect(kanbanDropUpdate(kase({}), "remediating", "me")).toEqual({ state: "remediating", owner_user_id: "me" });
    expect(kanbanDropUpdate(kase({ owner_user_id: "u1" }), "remediating", "me")).toEqual({ state: "remediating" });
    expect(kanbanDropUpdate(kase({}), "validating", "me")).toEqual({ state: "validating" });
  });
});
