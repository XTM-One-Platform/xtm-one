import { describe, it } from "node:test";
import assert from "node:assert/strict";

import type { ComparedCell, ComparedRun } from "@/types";
import {
  boardOf,
  boardValue,
  configKey,
  configRows,
  COST_METRIC,
  DURATION_METRIC,
  formatBoardValue,
  goodness,
  JUDGE_METRIC,
  launchesOf,
  launchKind,
  launchNotes,
  launchStats,
  setupNames,
  shownLaunches,
  sortConfigRows,
  zoomedAxis,
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

describe("boardOf", () => {
  const runs = [
    { ...run("a", "2026-11-16T09:00:00Z"), completed_at: "2026-11-16T09:10:00Z", judge_score: 70 },
    run("b", "2026-11-17T09:00:00Z", "schedule", "m2"),
  ];
  const cells = index([
    { ...cell("a", "k1", [1, 2], 0.5, 10), counts: { f1: [1, 1, 0] }, unreadable: false },
    { ...cell("a", "k2", [0, 2], 0.25, 20), counts: { f1: [0, 0, 2] }, unreadable: true },
    cell("a", "k3", null, 9, 5),
    cell("b", "k1", [2, 2], null, 30),
  ]);
  const board = boardOf(runs, cells, ["f1"]);
  it("adds the judge, the counted cost, the wall-clock duration and the unreadable answers", () => {
    const a = board.runs.find((r) => r.run.id === "a")!;
    assert.equal(boardValue(a, JUDGE_METRIC), 70);
    assert.equal(boardValue(a, COST_METRIC), 0.75);
    assert.equal(boardValue(a, DURATION_METRIC), 600);
    assert.equal(a.answers, 2);
    assert.equal(a.unreadable, 1);
    assert.deepEqual(a.counts.f1, [1, 1, 2]);
    const b = board.runs.find((r) => r.run.id === "b")!;
    assert.equal(boardValue(b, DURATION_METRIC), 30);
    assert.equal(boardValue(b, COST_METRIC), null);
  });
  it("ranks cost and time lower first", () => {
    assert.ok(goodness(1, COST_METRIC) > goodness(2, COST_METRIC));
    assert.ok(goodness(2, "f1") > goodness(1, "f1"));
    assert.equal(goodness(null, "f1"), Number.NEGATIVE_INFINITY);
  });
  it("reads a launch's kind from its trigger, and its best and mean runs", () => {
    assert.deepEqual(board.launches.map(launchKind), ["experiment", "scheduled"]);
    const stats = launchStats(board, "f1");
    assert.equal(stats[0].best, 25);
    assert.equal(stats[1].best, 100);
    assert.equal(stats[1].bestRun?.run.id, "b");
  });
});

describe("shownLaunches", () => {
  const launches = ["manual", "schedule", "manual", "schedule"].map((t, i) => ({ key: `${i}`, time: `${i}`, trigger: t, runs: [] }));
  const stats = [10, 40, 30, 20].map((best, i) => ({ best, bestRun: null, mean: best - i, runs: 1 }));
  it("picks then orders", () => {
    assert.deepEqual(shownLaunches(launches, stats, "f1", "experiment", "chrono"), [0, 2]);
    assert.deepEqual(shownLaunches(launches, stats, "f1", "scheduled", "newest"), [3, 1]);
    assert.deepEqual(shownLaunches(launches, stats, "f1", "all", "best"), [1, 2, 3, 0]);
    assert.deepEqual(shownLaunches(launches, stats, "f1", "custom", "chrono", new Set([3, 0])), [0, 3]);
  });
});

describe("zoomedAxis", () => {
  it("zooms on the range reached", () => {
    const { min, max } = zoomedAxis([60, 80, null]);
    assert.equal(max, 81);
    assert.equal(min, 55);
    assert.deepEqual(zoomedAxis([]), { min: 0, max: 1 });
    assert.deepEqual(zoomedAxis([50, 50]), { min: 0, max: 50 });
  });
});

describe("setupNames and launchNotes", () => {
  const withSetup = (id: string, t: string, key: string, setup: Record<string, unknown>) => ({ ...run(id, t), setup_key: key, setup });
  const runs = [
    withSetup("r1", "2026-11-16T09:00:00Z", "k1", { persona_version: 3, prompt_versions: { p: 1 } }),
    withSetup("r2", "2026-11-17T09:00:00Z", "k2", { persona_version: 4, prompt_versions: { p: 2 } }),
    withSetup("r3", "2026-11-18T09:00:00Z", "k3", { persona_version: 4, prompt_versions: { p: 3 } }),
    withSetup("r4", "2026-11-19T09:00:00Z", "k3", { persona_version: 4, prompt_versions: { p: 3 } }),
  ];
  it("names a setup by its agent and version, numbered when two share them", () => {
    const names = setupNames(runs);
    assert.equal(names.get("k1"), "a1 v3");
    assert.equal(names.get("k2"), "a1 v4 · 1");
    assert.equal(names.get("k3"), "a1 v4 · 2");
  });
  it("says what changed since the agent's previous launch", () => {
    const notes = launchNotes(launchesOf(runs));
    assert.equal(notes[0][0].status, "first");
    assert.deepEqual(notes[1][0].changes, [{ kind: "version", field: "persona", from: 3, to: 4 }, { kind: "prompts" }]);
    assert.deepEqual(notes[2][0].changes, [{ kind: "prompts" }]);
    assert.equal(notes[3][0].status, "same");
  });
});

describe("formatBoardValue", () => {
  it("writes dollars, minutes and one decimal", () => {
    assert.equal(formatBoardValue(1.234, COST_METRIC), "$1.23");
    assert.equal(formatBoardValue(0, COST_METRIC), "—");
    assert.equal(formatBoardValue(600, DURATION_METRIC), "10 min");
    assert.equal(formatBoardValue(42, DURATION_METRIC), "42 s");
    assert.equal(formatBoardValue(72.25, "f1"), "72.3");
    assert.equal(formatBoardValue(null, "f1"), "—");
  });
});

describe("configRows and sortConfigRows", () => {
  const runs = [run("a", "2026-11-16T09:00:00Z", "manual", "m1"), run("b", "2026-11-17T09:00:00Z", "manual", "m1"), run("c", "2026-11-17T09:00:00Z", "manual", "m2")];
  const board = boardOf(runs, index([cell("a", "k", [1, 2]), cell("b", "k", [2, 2]), cell("c", "k", [1, 4])]), ["f1"]);
  it("gives each configuration its best run, its trend and whether it ran in the launches shown", () => {
    const rows = configRows(board.runs, "f1", [0]);
    const m1 = rows.find((r) => r.runs[0].run.model_id === "m1")!;
    assert.equal(m1.best?.run.id, "b");
    assert.equal(m1.trend, 50);
    assert.equal(m1.inShown, true);
    assert.equal(rows.find((r) => r.runs[0].run.model_id === "m2")!.inShown, false);
  });
  it("sorts on any column", () => {
    const rows = configRows(board.runs, "f1", [0, 1]);
    const name = (r: (typeof rows)[number]) => r.runs[0].run.model_id;
    assert.deepEqual(sortConfigRows(rows, { col: "best", dir: -1 }, "f1", name).map(name), ["m1", "m2"]);
    assert.deepEqual(sortConfigRows(rows, { col: "name", dir: -1 }, "f1", name).map(name), ["m2", "m1"]);
    assert.deepEqual(sortConfigRows(rows, { col: 0, dir: 1 }, "f1", name).map(name), ["m2", "m1"]);
  });
});
