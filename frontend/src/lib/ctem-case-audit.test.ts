// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import type { CtemCaseEvent, CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import { auditRows, filterRows, groupByPhase, phaseBand, runPhase } from "./ctem-case-audit";

const event = (at: string, kind: string, extra: Partial<CtemCaseEvent> = {}): CtemCaseEvent =>
  ({ at, actor: "system", kind, issue: null, from: null, to: null, reason: null, evidence: null, ...extra }) as CtemCaseEvent;
const run = (id: string, at: string): CtemCaseRun => ({
  id,
  agent_id: "a",
  agent_name: "Triage",
  status: "completed",
  created_at: at,
  completed_at: null,
  outcome: "ok",
});

const history: CtemCaseEvent[] = [
  event("2026-11-23T11:58:00Z", "gate", { phase: "remediation", autonomy: "waiting", actor_type: "agent", actor: "ag-1" }),
  event("2026-11-23T11:08:00Z", "note", { phase: "prioritization", autonomy: "autonomous", actor_type: "agent", actor: "ag-1" }),
  event("2026-11-22T12:49:00Z", "created", { phase: "discovery", autonomy: "system", actor_type: "system" }),
];

describe("auditRows (#1490)", () => {
  it("keeps the served phase and autonomy class, newest first, runs placed in the phase they started in", () => {
    const rows = auditRows(history, [run("r1", "2026-11-23T11:30:00Z")]);
    expect(rows.map((r) => [r.key, r.phase, r.autonomy])).toEqual([
      ["event-0", "remediation", "waiting"],
      ["run-r1", "prioritization", "autonomous"],
      ["event-1", "prioritization", "autonomous"],
      ["event-2", "discovery", "system"],
    ]);
  });

  it("gives an event served without a phase (an older API) Discovery and its actor's class", () => {
    const [agent, system] = auditRows([event("2026-11-21T10:00:00Z", "note", { actor: "ag-1" }), event("2026-11-20T10:00:00Z", "state")], undefined);
    expect([agent.phase, agent.autonomy, agent.actorType]).toEqual(["discovery", "autonomous", "agent"]);
    expect([system.phase, system.autonomy, system.actorType]).toEqual(["discovery", "system", "system"]);
  });
});

describe("runPhase", () => {
  it("reads the phase of the latest event at or before the run, Discovery before any", () => {
    expect(runPhase("2026-11-23T11:59:00Z", history)).toBe("remediation");
    expect(runPhase("2026-11-20T00:00:00Z", history)).toBe("discovery");
  });
});

describe("groupByPhase and filterRows", () => {
  it("groups the rows by phase, the latest phase on top, with the count of each class", () => {
    const groups = groupByPhase(auditRows(history, [run("r1", "2026-11-23T11:30:00Z")]));
    expect(groups.map((g) => g.phase)).toEqual(["remediation", "prioritization", "discovery"]);
    expect(groups[1].counts.autonomous).toBe(2);
    expect(groups[0].counts.waiting).toBe(1);
  });

  it("shows the same rows in both views for a phase picked on the band", () => {
    const rows = auditRows(history, undefined);
    const picked = filterRows(rows, { phase: "prioritization" });
    expect(picked.map((r) => r.key)).toEqual(["event-1"]);
    expect(groupByPhase(picked).flatMap((g) => g.rows)).toEqual(picked);
    expect(filterRows(rows, { phase: null })).toHaveLength(3);
  });
});

describe("phaseBand", () => {
  it("lists the five phases in CTEM order, a phase not served as not reached", () => {
    const band = phaseBand([{ phase: "discovery", entered_at: "2026-11-22T12:49:00Z", exited_at: null, seconds: 60, events: 1, runs: 0, waiting: 0, current: true }]);
    expect(band.map((p) => p.phase)).toEqual(["scoping", "discovery", "prioritization", "validation", "remediation"]);
    expect(band[0].entered_at).toBeNull();
    expect(band[1].current).toBe(true);
  });
});
