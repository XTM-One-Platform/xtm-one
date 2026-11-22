// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { caseAttention, rankByAttention } from "./case-attention";

const NOW = Date.parse("2026-11-22T12:00:00Z");
const DAY = 24 * 3600 * 1000;

function kase(over: Partial<CtemCaseSummary> = {}): CtemCaseSummary {
  return {
    id: "c1",
    title: "Upgrade Microsoft Windows",
    remediation_kind: "upgrade",
    product_label: null,
    fixed_version: null,
    state: "needs_attention",
    state_since: null,
    owner_user_id: "u1",
    due_at: new Date(NOW + 30 * DAY).toISOString(),
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
    created_at: new Date(NOW).toISOString(),
    details_unavailable: [],
    ...over,
  };
}

describe("case attention (#1176)", () => {
  it("adds every reason and names each one", () => {
    const a = caseAttention(
      kase({ priority: "critical", kev_count: 175, asset_count: 914, crown_jewel_count: 2, owner_user_id: null, due_at: null, created_at: new Date(NOW - 3 * DAY).toISOString() }),
      NOW,
    );
    expect(a.score).toBe(40 + 15 + 15 + 5 + 3);
    expect(a.reasons.map((r) => r.kind)).toEqual(["priority", "kev", "crown_jewel", "no_owner", "age"]);
    expect(a.reasons).toContainEqual({ kind: "kev", count: 175, assets: 914 });
    expect(a.next).toBe("assign");
  });

  it("counts an overdue or SLA-breached case and a case due soon", () => {
    expect(caseAttention(kase({ due_at: new Date(NOW - DAY).toISOString() }), NOW).reasons).toContainEqual({ kind: "overdue" });
    expect(caseAttention(kase({ sla_breached_at: new Date(NOW).toISOString() }), NOW).reasons).toContainEqual({ kind: "overdue" });
    expect(caseAttention(kase({ due_at: new Date(NOW + 2 * DAY).toISOString() }), NOW).reasons).toContainEqual({ kind: "due_soon", days: 2 });
  });

  it("caps the age points", () => {
    const old = caseAttention(kase({ created_at: new Date(NOW - 400 * DAY).toISOString() }), NOW);
    expect(old.score).toBe(10);
  });

  it("scores a resolved case or an accepted risk 0", () => {
    expect(caseAttention(kase({ state: "resolved", priority: "critical" }), NOW).score).toBe(0);
    expect(caseAttention(kase({ state: "accepted_risk", priority: "critical" }), NOW).score).toBe(0);
  });

  it("asks for a due date once an owner is set", () => {
    expect(caseAttention(kase({ due_at: null }), NOW).next).toBe("set_due");
    expect(caseAttention(kase(), NOW).next).toBe("open_case");
  });

  it("ranks open cases only, highest score first", () => {
    const ranked = rankByAttention(
      [
        kase({ id: "low" }),
        kase({ id: "critical", priority: "critical" }),
        kase({ id: "done", state: "resolved", priority: "critical" }),
        kase({ id: "high", priority: "high" }),
      ],
      NOW,
    );
    expect(ranked.map((r) => r.kase.id)).toEqual(["critical", "high", "low"]);
  });
});
