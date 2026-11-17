// The Done for you timeline of the CTEM command center v2 (C2 slice V3): the
// points of each lane and their place on the 7 or 30 day axis. Fleets off is
// the primary case: module events and automation runs fill it alone.

import { describe, expect, it } from "vitest";

import { DFY_CLUSTER_AT, dayIndex, doneForYouCells, doneForYouPoints, type DfyRun } from "./done-for-you";
import type { ActivityEvent } from "./activity-feed";

const NOW = new Date(2026, 10, 9, 15, 0, 0).getTime();
const daysAgo = (d: number, hour = 10) => new Date(2026, 10, 9 - d, hour, 0, 0).toISOString();

function run(id: string, over: Partial<DfyRun> = {}): DfyRun {
  return {
    id, agent_name: "Exposure Briefing Analyst", assignment_name: "Exposure briefing", status: "completed",
    outcome: "ok", actions_taken: [], created_at: daysAgo(1), completed_at: daysAgo(1, 11), ...over,
  };
}

const synced: ActivityEvent = { kind: "posture_synced", id: "posture", at: daysAgo(0), covered: 3, total: 5 };
const down: ActivityEvent = { kind: "platform_unhealthy", id: "health:1", at: daysAgo(2), title: "OpenCTI" };

describe("doneForYouPoints", () => {
  it("fills the modules and automations lanes with no fleet", () => {
    const points = doneForYouPoints({ events: [synced, down], runs: [run("r1")], digests: [], fleetRuns: [] });
    expect(points.map((p) => [p.lane, p.tone])).toEqual([
      ["modules", "module"],
      ["automations", "automation"],
      ["modules", "attention"],
    ]);
  });

  it("adds the outcome a run records, linked to the run", () => {
    const points = doneForYouPoints({
      events: [], runs: [run("r1", { actions_taken: [{}, {}] })], digests: [], fleetRuns: [],
    });
    const outcome = points.find((p) => p.lane === "outcomes");
    expect(outcome?.from).toBe("run:r1");
    expect(outcome?.subject).toEqual({ kind: "actions", count: 2, name: "Exposure briefing" });
    expect(outcome?.runId).toBe("r1");
  });

  it("gives a failed run no outcome and the attention colour", () => {
    const points = doneForYouPoints({
      events: [], runs: [run("r1", { outcome: "failed", status: "failed", actions_taken: [{}] })], digests: [], fleetRuns: [],
    });
    expect(points.map((p) => [p.lane, p.tone])).toEqual([["automations", "attention"]]);
  });

  it("lists a fleet run once, in the fleets lane, never as an automation", () => {
    const points = doneForYouPoints({
      events: [], runs: [run("f1")], digests: [],
      fleetRuns: [{ runId: "f1", fleetName: "Vulnerability to verified fix", agentName: "Triage", outcome: "succeeded", at: daysAgo(1) }],
    });
    expect(points.map((p) => p.lane)).toEqual(["fleets"]);
  });

  it("draws a digest as the outcome of its run, and the run when the feed missed it", () => {
    const digest = { id: "d1", agent_name: "Analyst", assignment_name: "Weekly digest", created_at: daysAgo(3) };
    const withRun = doneForYouPoints({ events: [], runs: [run("d1", { created_at: daysAgo(3) })], digests: [digest], fleetRuns: [] });
    expect(withRun.filter((p) => p.lane === "automations")).toHaveLength(1);
    expect(withRun.find((p) => p.lane === "outcomes")?.from).toBe("run:d1");
    const alone = doneForYouPoints({ events: [], runs: [], digests: [digest], fleetRuns: [] });
    expect(alone.map((p) => p.lane).sort()).toEqual(["automations", "outcomes"]);
  });

  it("never invents a time", () => {
    const points = doneForYouPoints({ events: [], runs: [run("r1", { created_at: "never" })], digests: [], fleetRuns: [] });
    expect(points).toEqual([]);
  });
});

describe("dayIndex", () => {
  it("places today last and drops what is outside the period", () => {
    expect(dayIndex(daysAgo(0), 7, NOW)).toBe(6);
    expect(dayIndex(daysAgo(6), 7, NOW)).toBe(0);
    expect(dayIndex(daysAgo(7), 7, NOW)).toBeNull();
    expect(dayIndex(daysAgo(7), 30, NOW)).toBe(22);
    expect(dayIndex(new Date(NOW + 60_000).toISOString(), 7, NOW)).toBeNull();
  });
});

describe("doneForYouCells", () => {
  it("draws modules and automations always, the fleets and outcomes lanes only when they hold a point", () => {
    expect(doneForYouCells([], 7, NOW)).toEqual({ lanes: ["modules", "automations"], cells: [], count: 0 });
    const points = doneForYouPoints({ events: [], runs: [run("r1", { actions_taken: [{}] })], digests: [], fleetRuns: [] });
    expect(doneForYouCells(points, 7, NOW).lanes).toEqual(["modules", "automations", "outcomes"]);
  });

  it("clusters a day of one lane past the threshold", () => {
    const runs = Array.from({ length: DFY_CLUSTER_AT + 1 }, (_, i) => run(`r${i}`, { created_at: daysAgo(1, 8 + i) }));
    const { cells, count } = doneForYouCells(doneForYouPoints({ events: [], runs, digests: [], fleetRuns: [] }), 7, NOW);
    expect(count).toBe(DFY_CLUSTER_AT + 1);
    expect(cells).toHaveLength(1);
    expect(cells[0]).toMatchObject({ lane: "automations", day: 5, clustered: true });
    expect(cells[0].points.map((p) => p.id)).toEqual(runs.map((r) => `run:${r.id}`));
  });

  it("counts only the period", () => {
    const runs = [run("old", { created_at: daysAgo(20) }), run("new", { created_at: daysAgo(2) })];
    const points = doneForYouPoints({ events: [], runs, digests: [], fleetRuns: [] });
    expect(doneForYouCells(points, 7, NOW).count).toBe(1);
    expect(doneForYouCells(points, 30, NOW).count).toBe(2);
  });
});
