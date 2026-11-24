// The loop on a case (CTEM-03-UI, #892): the SLA countdown, the reopened and
// escalated marks, and the open P1 cases Act now puts first.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { actNowCasePriority, actNowCases, caseScopes } from "./act-now";
import type { CtemCaseIssue } from "@/hooks/api/useCtemCasesApi";
import { CaseUrgencyChip, RetestChip, caseLoopMarks, hasRetestData, retestRank, slaCountdown } from "./case-loop";

const NOW = Date.parse("2026-11-21T12:00:00Z");

describe("slaCountdown", () => {
  it("counts hours left under two days", () => {
    expect(slaCountdown("2026-11-22T11:00:00Z", NOW)).toEqual({ late: false, value: 23, unit: "hour" });
  });
  it("counts days for a P3", () => {
    expect(slaCountdown("2026-12-21T12:00:00Z", NOW)).toEqual({ late: false, value: 30, unit: "day" });
  });
  it("says late past the due time", () => {
    expect(slaCountdown("2026-11-21T09:00:00Z", NOW)).toEqual({ late: true, value: 3, unit: "hour" });
  });
  it("counts minutes in the last hour", () => {
    expect(slaCountdown("2026-11-21T12:20:00Z", NOW)).toEqual({ late: false, value: 20, unit: "minute" });
  });
  it("is nothing without an SLA", () => {
    expect(slaCountdown(null, NOW)).toBeNull();
    expect(slaCountdown("not a date", NOW)).toBeNull();
  });
});

describe("caseLoopMarks", () => {
  const failed = [{ provider: "openaev", kind: "retest", ref: "r1", result: "failed" as const, at: "x" }];
  const passed = [{ provider: "openaev", kind: "retest", ref: "r1", result: "passed" as const, at: "x" }];
  it("marks an open case whose last validation failed as reopened", () => {
    expect(caseLoopMarks({ state: "new", validations: failed, sla_breached_at: null })).toMatchObject({ reopened: true, escalated: false, closedOnRetest: false });
  });
  it("marks an open case past its SLA as escalated", () => {
    expect(caseLoopMarks({ state: "remediating", validations: passed, sla_breached_at: "x" })).toMatchObject({ reopened: false, escalated: true, closedOnRetest: false });
  });
  it("marks nothing on a resolved case whose last validation is not a passing re-test", () => {
    expect(caseLoopMarks({ state: "resolved", validations: failed, sla_breached_at: "x" })).toEqual({
      reopened: false,
      escalated: false,
      closedOnRetest: false,
      closedAt: null,
      closedProvider: null,
      closedAtSource: false,
    });
    const scan = [{ provider: "openaev", kind: "validation", ref: "s1", result: "passed" as const, at: "x" }];
    expect(caseLoopMarks({ state: "resolved", validations: scan, sla_breached_at: null }).closedOnRetest).toBe(false);
    expect(caseLoopMarks({ state: "resolved", validations: [], sla_breached_at: null }).closedOnRetest).toBe(false);
  });
  it("marks a resolved case closed by a passing re-test, naming the product and the time, never the run reference", () => {
    const marks = caseLoopMarks({ state: "resolved", validations: passed, sla_breached_at: null });
    expect(marks).toMatchObject({ closedOnRetest: true, closedAt: "x", closedProvider: "OpenAEV" });
    expect(JSON.stringify(marks)).not.toContain("r1");
  });
  it("marks a resolved case whose source closed every member as closed at source, not as a re-test (CTEM-25-UI)", () => {
    const rescan = [{ provider: "aikido", kind: "rescan", ref: "42", result: "passed" as const, at: "y" }];
    const marks = caseLoopMarks({ state: "resolved", validations: rescan, sla_breached_at: null });
    expect(marks).toMatchObject({ closedOnRetest: false, closedAtSource: true, closedAt: "y", closedProvider: "Aikido Security" });
    expect(caseLoopMarks({ state: "remediating", validations: rescan, sla_breached_at: null }).closedAtSource).toBe(false);
  });
});

describe("re-test of a member", () => {
  const member = (extra: Partial<CtemCaseIssue>): CtemCaseIssue => ({ source: "openaev", external_id: "f1", open: true, ...extra }) as CtemCaseIssue;
  it("has no column data when no member carries the lifecycle", () => {
    expect(hasRetestData([member({}), member({ source: "opencti" } as Partial<CtemCaseIssue>)])).toBe(false);
    expect(hasRetestData([member({}), member({ retest_status: "open" })])).toBe(true);
    expect(hasRetestData([member({ retest_pending: true })])).toBe(true);
  });
  it("sorts by lifecycle order, members with no lifecycle last", () => {
    const ranks = [
      member({ retest_status: "resolved" }),
      member({ retest_pending: true }),
      member({ retest_status: "open", last_tested_at: "2026-11-20T14:05:00Z" }),
      member({}),
      member({ retest_status: "not_reproduced" }),
      member({ retest_status: "open" }),
    ].map(retestRank);
    expect(ranks).toEqual([4, 0, 1, 99, 3, 2]);
  });
  it("draws one chip per state and nothing without a lifecycle", () => {
    renderWithProviders(<RetestChip issue={member({ retest_status: "open", last_tested_at: "2026-11-20T14:05:00Z" })} />);
    expect(screen.getByText("Reproduced")).toBeInTheDocument();
    const empty = renderWithProviders(<RetestChip issue={member({})} />);
    expect(empty.container.textContent).toBe("");
  });
});

describe("CaseUrgencyChip", () => {
  // One case, one urgency (UX round 1 on #897, B3): never "P1" beside "Low".
  it("shows the severity alone when the case has one", () => {
    renderWithProviders(<CaseUrgencyChip severity="P1" priority="critical" />);
    expect(screen.getByText("P1")).toBeInTheDocument();
    expect(screen.queryByText("Critical")).toBeNull();
    expect(screen.queryByText("Low")).toBeNull();
  });
  it("shows the priority when the case has no severity", () => {
    renderWithProviders(<CaseUrgencyChip severity={null} priority="low" />);
    expect(screen.getByText("Low")).toBeInTheDocument();
  });
});

describe("actNowCases", () => {
  it("ranks the open loop cases by severity, then scope criticality, then the nearest SLA", () => {
    const cases = [
      { id: "a", state: "new", severity: "P1", sla_due_at: "2026-11-22T10:00:00Z" },
      { id: "b", state: "remediating", severity: "P1", sla_due_at: "2026-11-21T20:00:00Z" },
      { id: "c", state: "new", severity: "P2", sla_due_at: "2026-11-21T13:00:00Z" },
      { id: "d", state: "resolved", severity: "P1", sla_due_at: "2026-11-21T13:00:00Z" },
      { id: "e", state: "new", severity: "P1", sla_due_at: "2026-11-23T10:00:00Z" },
    ];
    expect(actNowCases(cases).map((c) => c.id)).toEqual(["b", "a", "e", "c"]);
    // A critical primary asset puts its case first among the P1s, whatever its SLA.
    const scopes = caseScopes([
      { case_id: "e", decision: { scope: { criticality: "critical" }, reconcile: { primary_assets_named: ["Customer data"] } } },
      { case_id: "a", decision: { scope: { criticality: "low" } } },
      { case_id: "a", decision: { scope: { criticality: "high" } } },
    ]);
    expect(scopes.get("a")?.criticality).toBe("high");
    expect(scopes.get("e")?.primaryAssets).toEqual(["Customer data"]);
    expect(actNowCases(cases, scopes).map((c) => c.id)).toEqual(["e", "a", "b", "c"]);
  });

  it("keeps a P2 below what waits on the reader and a P1 above it, in the loop's order", () => {
    expect(actNowCasePriority("P1", 1)).toBeGreaterThan(98);
    expect(actNowCasePriority("P2", 0)).toBeLessThan(97);
    expect(actNowCasePriority("P2", 0)).toBeGreaterThan(actNowCasePriority("P2", 1));
  });
});
