import { describe, it } from "node:test";
import assert from "node:assert/strict";

import type { ComparedCell, ComparedRun } from "@/types";
import {
  configKey,
  launchesOf,
  paretoFront,
  perEntry,
  runLabeler,
  poolRuns,
  runValue,
  spreadOf,
  toCsv,
} from "./evaluation-leaderboard.ts";

const run = (id: string, created_at: string, trigger_reason = "manual", model_id = "m1", subject_id = "a1"): ComparedRun => ({
  id, evaluation_id: "e", subject_id, subject_name: subject_id, model_id, is_default_model: false, created_at, trigger_reason,
  metric_suite_version: "15.0.0", execution_mode: "full_loop", quality_score: null, completed_entries: 2, total_entries: 2,
});

const cell = (run_id: string, entry_key: string, counts: [number, number] | null, cost: number | null = null, seconds: number | null = null): ComparedCell => ({
  run_id, entry_key, result_id: `${run_id}-${entry_key}`, counted: counts !== null, anomaly: null,
  metrics: counts ? { f1: counts } : {}, cost, seconds,
});

function index(cells: ComparedCell[]): Map<string, Map<string, ComparedCell>> {
  const out = new Map<string, Map<string, ComparedCell>>();
  for (const c of cells) {
    if (!out.has(c.run_id)) out.set(c.run_id, new Map());
    out.get(c.run_id)!.set(c.entry_key, c);
  }
  return out;
}

describe("launchesOf", () => {
  it("groups runs of one minute and one trigger, oldest launch first", () => {
    const launches = launchesOf([
      run("r3", "2026-11-17T10:05:10Z"),
      run("r1", "2026-11-16T09:00:01Z"),
      run("r2", "2026-11-16T09:00:40Z"),
      run("r4", "2026-11-16T09:00:20Z", "schedule"),
    ]);
    assert.deepEqual(launches.map((l) => l.runs.map((r) => r.id)), [["r1", "r2"], ["r4"], ["r3"]]);
    assert.equal(launches[1].trigger, "schedule");
  });
});

describe("configKey", () => {
  it("is the agent and the model", () => {
    assert.notEqual(configKey(run("x", "t", "manual", "m1", "a1")), configKey(run("y", "t", "manual", "m2", "a1")));
    assert.equal(configKey(run("x", "t", "manual", "m1", "a1")), configKey(run("y", "u", "schedule", "m1", "a1")));
  });
});

describe("runLabeler", () => {
  const r = (model_id: string, subject_id: string | null, subject_name: string | null) => ({ model_id, subject_id, subject_name });
  it("names runs by what differs: the model, the agent, or both", () => {
    const models = [r("openai/m1", "a", "Harvester"), r("m2", "a", "Harvester")];
    assert.deepEqual(models.map(runLabeler(models)), ["m1", "m2"]);
    const agents = [r("m1", "a", "Harvester"), r("m1", "b", "Grounded")];
    assert.deepEqual(agents.map(runLabeler(agents)), ["Harvester", "Grounded"]);
    const both = [r("m1", "a", "Harvester"), r("m2", "b", "Grounded")];
    assert.deepEqual(both.map(runLabeler(both)), ["m1 · Harvester", "m2 · Grounded"]);
  });
  it("never leaves a label empty", () => {
    // Every run on the same model and agent (one agent across evaluations): the model.
    const same = [r("m1", "a", "Harvester"), r("m1", "a", "Harvester")];
    assert.deepEqual(same.map(runLabeler(same)), ["m1", "m1"]);
    // An agent gone (no name) among others on one model: the model.
    const gone = [r("m1", "a", null), r("m1", "b", "Grounded")];
    assert.deepEqual(gone.map(runLabeler(gone)), ["m1", "Grounded"]);
  });
});

describe("runValue and perEntry", () => {
  const cells = index([cell("r", "k1", [1, 2], 0.5, 10), cell("r", "k2", [3, 6], 0, 30), cell("r", "k3", null, null, null)]);
  it("pools a run's own entries, leaving out the ones it did not count", () => {
    assert.equal(runValue(cells, "r", "f1"), 50);
    assert.equal(runValue(cells, "nope", "f1"), null);
  });
  it("averages recorded costs (a 0 cost is unrecorded) and every agent time", () => {
    assert.equal(perEntry(cells, ["r"], "cost"), 0.5);
    assert.equal(perEntry(cells, ["r"], "seconds"), 20);
  });
});

describe("poolRuns", () => {
  it("pools every run's counts and brackets the value", () => {
    const interval = poolRuns([[[1, 2], [1, 2]], [[2, 2], [0, 2]]], 7)!;
    assert.equal(interval.value, 50);
    assert.ok(interval.low <= 50 && interval.high >= 50);
  });
  it("is stable for a seed and null with nothing counted", () => {
    const counts = [[[1, 4], [3, 4]], [[2, 4]]] as const;
    assert.deepEqual(poolRuns(counts as unknown as [number, number][][], 3), poolRuns(counts as unknown as [number, number][][], 3));
    assert.equal(poolRuns([[], []], 1), null);
  });
  it("has no width when every resample is the same", () => {
    assert.deepEqual(poolRuns([[[2, 4], [2, 4]], [[1, 2]]], 5), { value: 50, low: 50, high: 50 });
  });
});

describe("paretoFront", () => {
  it("keeps the points no cheaper point beats", () => {
    const front = paretoFront([
      { id: "a", x: 1, y: 40 },
      { id: "b", x: 2, y: 30 },
      { id: "c", x: 3, y: 60 },
      { id: "d", x: 0.5, y: 40 },
    ]);
    assert.deepEqual(front.map((p) => p.id), ["d", "c"]);
  });
});

describe("spreadOf", () => {
  it("needs two runs and gives the sample standard deviation", () => {
    assert.equal(spreadOf([50]), null);
    assert.deepEqual(spreadOf([40, 60]), { mean: 50, sd: Math.sqrt(200), min: 40, max: 60 });
  });
});

describe("toCsv", () => {
  it("quotes fields that need it", () => {
    assert.equal(toCsv([["a", 1, null], ['say "hi", ok', 2.5, "x"]]), 'a,1,\n"say ""hi"", ok",2.5,x\n');
  });
});
