// The loop on a case (CTEM-03-UI, #892): the SLA countdown, the reopened and
// escalated marks, and the open P1 cases Act now puts first.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { actNowP1Cases } from "./act-now";
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
    expect(caseLoopMarks({ state: "needs_attention", validations: failed, sla_breached_at: null })).toMatchObject({ reopened: true, escalated: false, closedOnRetest: false });
  });
  it("marks an open case past its SLA as escalated", () => {
    expect(caseLoopMarks({ state: "in_progress", validations: passed, sla_breached_at: "x" })).toMatchObject({ reopened: false, escalated: true, closedOnRetest: false });
  });
  it("marks nothing on a resolved case whose last validation is not a passing re-test", () => {
    expect(caseLoopMarks({ state: "resolved", validations: failed, sla_breached_at: "x" })).toEqual({
      reopened: false,
      escalated: false,
      closedOnRetest: false,
      closedAt: null,
      closedProvider: null,
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

describe("actNowP1Cases", () => {
  it("keeps the open P1 cases, the nearest SLA first, two at most", () => {
    const cases = [
      { id: "a", state: "needs_attention", severity: "P1", sla_due_at: "2026-11-22T10:00:00Z" },
      { id: "b", state: "in_progress", severity: "P1", sla_due_at: "2026-11-21T20:00:00Z" },
      { id: "c", state: "needs_attention", severity: "P2", sla_due_at: "2026-11-21T13:00:00Z" },
      { id: "d", state: "resolved", severity: "P1", sla_due_at: "2026-11-21T13:00:00Z" },
      { id: "e", state: "needs_attention", severity: "P1", sla_due_at: "2026-11-23T10:00:00Z" },
    ];
    expect(actNowP1Cases(cases).map((c) => c.id)).toEqual(["b", "a"]);
  });
});
