// The loop on a case (CTEM-03-UI, #892): the SLA countdown, the reopened and
// escalated marks, and the open P1 cases Act now puts first.

import { describe, expect, it } from "vitest";
import { actNowP1Cases } from "./act-now";
import { caseLoopMarks, slaCountdown } from "./case-loop";

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
    expect(caseLoopMarks({ state: "needs_attention", validations: failed, sla_breached_at: null })).toEqual({ reopened: true, escalated: false });
  });
  it("marks an open case past its SLA as escalated", () => {
    expect(caseLoopMarks({ state: "in_progress", validations: passed, sla_breached_at: "x" })).toEqual({ reopened: false, escalated: true });
  });
  it("marks nothing on a resolved case", () => {
    expect(caseLoopMarks({ state: "resolved", validations: failed, sla_breached_at: "x" })).toEqual({ reopened: false, escalated: false });
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
