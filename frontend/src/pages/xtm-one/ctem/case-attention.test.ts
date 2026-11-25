// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { caseAttention, DEFAULT_CASE_ATTENTION_WEIGHTS, rankByAttention } from "./case-attention";

const NOW = Date.parse("2026-11-22T12:00:00Z");
const DAY = 24 * 3600 * 1000;

function kase(over: Partial<CtemCaseSummary> = {}): CtemCaseSummary {
  return {
    id: "c1",
    title: "Upgrade Microsoft Windows",
    remediation_kind: "upgrade",
    product_label: null,
    fixed_version: null,
    state: "new",
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
    primary_asset_count: 0,
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
      kase({ priority: "critical", kev_count: 175, asset_count: 914, primary_asset_count: 2, owner_user_id: null, due_at: null, created_at: new Date(NOW - 3 * DAY).toISOString() }),
      NOW,
    );
    expect(a.score).toBe(40 + 15 + 15 + 5 + 3);
    expect(a.reasons.map((r) => r.kind)).toEqual(["priority", "kev", "primary_asset", "no_owner", "age"]);
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

  describe("a reason set to 0 points leaves the score and the why line (UX r2 B1 on #1260)", () => {
    const W = DEFAULT_CASE_ATTENTION_WEIGHTS;
    const kinds = (over: Partial<CtemCaseSummary>, weights: typeof W) => caseAttention(kase(over), NOW, weights).reasons.map((r) => r.kind);

    it("priority", () => {
      expect(kinds({ priority: "critical" }, { ...W, priority: { ...W.priority, critical: 0 } })).not.toContain("priority");
    });
    it("KEV", () => {
      expect(kinds({ kev_count: 2 }, { ...W, kev: 0 })).not.toContain("kev");
      expect(caseAttention(kase({ kev_count: 2 }), NOW, { ...W, kev: 0 }).score).toBe(0);
    });
    it("primary asset", () => {
      expect(kinds({ primary_asset_count: 1 }, { ...W, primaryAsset: 0 })).not.toContain("primary_asset");
    });
    it("overdue, without falling back to due soon", () => {
      const r = kinds({ due_at: new Date(NOW - DAY).toISOString() }, { ...W, overdue: 0 });
      expect(r).not.toContain("overdue");
      expect(r).not.toContain("due_soon");
    });
    it("due soon", () => {
      expect(kinds({ due_at: new Date(NOW + DAY).toISOString() }, { ...W, dueSoon: 0 })).not.toContain("due_soon");
    });
    it("no owner", () => {
      expect(kinds({ owner_user_id: null }, { ...W, noOwner: 0 })).not.toContain("no_owner");
    });
    it("age, per day or cap", () => {
      const old = { created_at: new Date(NOW - 5 * DAY).toISOString() };
      expect(kinds(old, { ...W, agePerDay: 0 })).not.toContain("age");
      expect(kinds(old, { ...W, ageMax: 0 })).not.toContain("age");
      expect(kinds(old, W)).toContain("age");
    });
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
