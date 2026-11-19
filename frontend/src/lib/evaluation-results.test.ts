import { describe, it } from "node:test";
import assert from "node:assert/strict";

import type { BundleReview, ComparedCell, ComparedRun } from "@/types";
import { boardOf } from "./evaluation-leaderboard.ts";
import {
  consistencyRows,
  consistencySetups,
  expectedTotal,
  foundGroups,
  methodologyMarkdown,
  precisionRecall,
  rankConfigs,
  resolveCssVars,
  resultSets,
  runsOfSet,
  slugOf,
  tallyFound,
} from "./evaluation-results.ts";

const run = (id: string, created_at: string, trigger_reason: string, model_id: string, setup_key: string): ComparedRun => ({
  id, evaluation_id: "e", subject_id: "a", subject_name: "Agent", model_id, is_default_model: false, created_at, trigger_reason,
  metric_suite_version: "15.0.0", execution_mode: "full_loop", quality_score: null, completed_entries: 2, total_entries: 2, setup_key,
});

const cell = (run_id: string, entry_key: string, metrics: [number, number] | null, counts?: [number, number, number], unreadable = false): ComparedCell => ({
  run_id, entry_key, result_id: `${run_id}-${entry_key}`, counted: metrics !== null, anomaly: null,
  metrics: metrics ? { f1: metrics } : {}, cost: null, seconds: null, counts: counts ? { f1: counts } : {}, unreadable,
});

function index(cells: ComparedCell[]): Map<string, Map<string, ComparedCell>> {
  const out = new Map<string, Map<string, ComparedCell>>();
  for (const c of cells) {
    if (!out.has(c.run_id)) out.set(c.run_id, new Map());
    out.get(c.run_id)!.set(c.entry_key, c);
  }
  return out;
}

const runs = [
  run("r1", "2026-11-16T09:00:00Z", "manual", "m1", "s1"),
  run("r2", "2026-11-16T09:00:00Z", "manual", "m2", "s1"),
  run("r3", "2026-11-17T09:00:00Z", "schedule", "m1", "s1"),
  run("r4", "2026-11-18T09:00:00Z", "manual", "m1", "s2"),
];
const cells = index([
  cell("r1", "k1", [2, 4], [1, 1, 1]), cell("r1", "k2", [0, 2], [0, 0, 2], true),
  cell("r2", "k1", [4, 4], [2, 0, 0]), cell("r2", "k2", null),
  cell("r3", "k1", [4, 4], [2, 0, 0]), cell("r3", "k2", [2, 2], [1, 0, 0]),
  cell("r4", "k1", [0, 4], [0, 2, 2]), cell("r4", "k2", [2, 2], [1, 0, 0]),
]);
const board = boardOf(runs, cells, ["f1"]);

describe("resultSets and runsOfSet", () => {
  it("offers each experiment launch newest first, each rerun setup, the newest runs and every run", () => {
    const sets = resultSets(board);
    assert.deepEqual(sets.map((s) => s.id), ["l2", "l0", "s:s1", "newest", "all"]);
    assert.deepEqual(runsOfSet(board, sets[1]).map((r) => r.run.id), ["r1", "r2"]);
    assert.deepEqual(runsOfSet(board, sets[2]).map((r) => r.run.id).sort(), ["r1", "r2", "r3"]);
    assert.deepEqual(runsOfSet(board, sets[3]).map((r) => r.run.id).sort(), ["r2", "r4"]);
  });
});

describe("rankConfigs", () => {
  it("pools each configuration, with precision, recall and its unreadable answers", () => {
    const rows = rankConfigs(runsOfSet(board, { id: "l0", kind: "launch", launch: 0 }), cells, "f1", "x");
    assert.deepEqual(rows.map((r) => r.run.model_id), ["m2", "m1"]);
    const m1 = rows[1];
    assert.equal(m1.value, (100 * 2) / 6);
    assert.equal(m1.precision, 50);
    assert.equal(m1.recall, 25);
    assert.equal(m1.unreadable, 1);
    assert.equal(m1.answers, 2);
    assert.equal(rows[0].answers, 1);
  });
  it("reads precision and recall, a side with nothing as perfect", () => {
    assert.deepEqual(precisionRecall([0, 0, 0]), { precision: 100, recall: 100 });
    assert.deepEqual(precisionRecall(undefined), { precision: null, recall: null });
  });
});

describe("consistency", () => {
  it("lists the setups run twice by one configuration, and their spread", () => {
    assert.deepEqual(consistencySetups(board).map((s) => s.setup), ["s1"]);
    const rows = consistencyRows(board, "s1", "f1");
    assert.equal(rows.length, 1);
    assert.deepEqual(rows[0].values, [100 / 3, 100]);
  });
  it("groups each configuration's counted answers per entry", () => {
    const rows = consistencyRows(board, "s1", "f1");
    const { groups, owners, skipped } = foundGroups(rows, cells);
    assert.deepEqual(groups.map((g) => [...g].sort()), [["r1-k1", "r3-k1"], ["r1-k2", "r3-k2"]]);
    assert.deepEqual(owners, [rows[0].id, rows[0].id]);
    assert.deepEqual(skipped, []);
  });
  it("tallies the expected items found by every answer, some, or none", () => {
    const review = {
      entry: { label: "e", pages: [], file_name: null, text_source: null },
      answers: [{ key: "a1" }, { key: "a2" }],
      objects: [
        { t: "malware", n: "X", c: "x" }, { t: "malware", n: "X", c: "x" }, { t: "malware", n: "X", c: "x" },
        { t: "tool", n: "Y", c: "y" }, { t: "tool", n: "Y", c: "y" },
        { t: "tool", n: "Z", c: "z" },
      ],
      groups: [0, 0, 0, 1, 1, 2],
      sources: { expected: [0, 3, 5], a1: [1, 4], a2: [2] },
      relationships: [[0, 1, { expected: ["uses", 0, 3, 0], a1: ["uses", 1, 4, 0] }]],
      unreadable: [],
    } as unknown as BundleReview;
    assert.deepEqual(tallyFound([review], ["row"], "objects").get("row"), { every: 1, some: 1, never: 1 });
    assert.deepEqual(tallyFound([review], ["row"], "both").get("row"), { every: 1, some: 2, never: 1 });
  });
});

describe("methodology and exports", () => {
  it("sums what the entries expect once per entry", () => {
    assert.equal(expectedTotal(cells, "f1"), 2 + 2);
    assert.equal(expectedTotal(cells, "nope"), null);
  });
  it("writes Markdown, slugs and stand-alone SVG colours", () => {
    assert.equal(methodologyMarkdown("Methodology · X", [{ term: "A", text: "a." }, { term: "B", text: "b." }]), "# Methodology · X\n\n**A.** a.\n\n**B.** b.\n");
    assert.equal(slugOf("STIX Extraction (v2)"), "stix-extraction-v2");
    assert.equal(resolveCssVars('fill:var(--a);stroke:var(--b)', (n) => (n === "--a" ? "red" : "")), "fill:red;stroke:currentColor");
  });
});
