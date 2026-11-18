// The run review's pure part (#12282): a run's figures over its reports follow the
// platform's rule (an entry left out of the headlines is left out, an unreadable answer is
// counted), the previous run of an agent and model is the comparator, the causes add up,
// and the quick comparisons name the runs a reviewer reaches for.

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import type { ComparedCell, ComparedRun, EvaluationComparison } from "@/types";
import { EXPECTED, NONE, buildModel } from "./evaluation-bundle-review.ts";
import { cellIndex } from "./evaluation-compare.ts";
import {
  causeSummary,
  cellValue,
  comparatorOf,
  configRuns,
  entryCauses,
  precision,
  presets,
  previousRun,
  recall,
  reviewEntries,
  reviewResultIds,
  runFigures,
  runsOfComparison,
  sharedCounted,
} from "./evaluation-run-review.ts";

function run(id: string, subject: string, model: string, at: string, over: Partial<ComparedRun> = {}): ComparedRun {
  return {
    id,
    evaluation_id: "ev",
    subject_id: subject,
    subject_name: subject,
    model_id: model,
    is_default_model: false,
    created_at: at,
    trigger_reason: "manual",
    metric_suite_version: "1",
    execution_mode: null,
    quality_score: null,
    completed_entries: 2,
    total_entries: 2,
    ...over,
  };
}

function cell(runId: string, entry: string, e: [number, number, number], r: [number, number, number], over: Partial<ComparedCell> = {}): ComparedCell {
  const prf = ([tp, fp, fn]: [number, number, number]): [number, number] => [2 * tp, 2 * tp + fp + fn];
  return {
    run_id: runId,
    entry_key: entry,
    result_id: `${runId}-${entry}`,
    counted: true,
    anomaly: null,
    metrics: {
      "stix.entity_prf": prf(e),
      "stix.relationship_prf": prf(r),
      "stix.bundle_f1": prf([e[0] + r[0], e[1] + r[1], e[2] + r[2]]),
    },
    counts: { "stix.entity_prf": e, "stix.relationship_prf": r, "stix.bundle_f1": [e[0] + r[0], e[1] + r[1], e[2] + r[2]] },
    cost: 0.1,
    seconds: 30,
    judge_score: 80,
    ...over,
  };
}

const R1 = run("r1", "agent", "m1", "2026-01-01T10:00:00");
const R2 = run("r2", "agent", "m1", "2026-01-02T10:00:00", { setup_key: "s" });
const R3 = run("r3", "agent", "m1", "2026-01-03T10:00:00", { setup_key: "s" });
const OTHER = run("o3", "agent", "m2", "2026-01-03T10:00:30");

const COMPARISON: EvaluationComparison = {
  family: "STIX_BUNDLE",
  metrics: [],
  evaluations: [
    {
      id: "ev",
      name: "STIX extraction",
      family: "STIX_BUNDLE",
      execution_mode: null,
      timeout_seconds: null,
      judge_on: true,
      evaluator_model: null,
      judge_weights: null,
      judge_instructions_digest: null,
      subjects: [],
      datasets: [],
      runs: [R3],
      run_choices: [R3, OTHER, R2, R1],
    },
  ],
  entries: [
    { key: "e2", label: "Second report", members: [{ evaluation_id: "ev", entry_id: "2", position: 1, has_file: false }] },
    { key: "e1", label: "First report", members: [{ evaluation_id: "ev", entry_id: "1", position: 0, has_file: false }] },
  ],
  cells: [
    cell("r3", "e1", [3, 1, 0], [1, 0, 1]),
    // Unreadable: counted, everything missing.
    cell("r3", "e2", [0, 0, 4], [0, 0, 2], { unreadable: true, judge_score: null }),
    cell("r2", "e1", [2, 0, 1], [1, 0, 1]),
    // A platform error: left out of the figures.
    cell("r2", "e2", [0, 0, 0], [0, 0, 0], { counted: false, metrics: {}, counts: {}, judge_score: null }),
    cell("o3", "e1", [4, 0, 0], [2, 0, 0]),
  ],
};
const cells = cellIndex(COMPARISON);
const runs = runsOfComparison(COMPARISON);

describe("runs and comparators", () => {
  it("lists every run once, oldest first", () => {
    assert.deepEqual(runs.map((r) => r.id), ["r1", "r2", "r3", "o3"]);
  });

  it("compares a run with the previous run of its agent and model, or with its reference", () => {
    assert.deepEqual(configRuns(runs, R3).map((r) => r.id), ["r1", "r2", "r3"]);
    assert.equal(previousRun(runs, R3)?.id, "r2");
    assert.equal(previousRun(runs, R1), null);
    assert.equal(comparatorOf(runs, R3, EXPECTED)?.id, "r2");
    assert.equal(comparatorOf(runs, R3, NONE)?.id, "r2");
    assert.equal(comparatorOf(runs, R3, "o3")?.id, "o3");
  });
});

describe("runFigures", () => {
  it("counts an unreadable answer as everything missing", () => {
    const f = runFigures(cells.get("r3"));
    assert.equal(f.counted, 2);
    assert.equal(f.unreadable, 1);
    assert.deepEqual(f.counts["stix.entity_prf"], { tp: 3, fp: 1, fn: 4 });
    // 2·3 / (2·3 + 1 + 4)
    assert.equal(f.value["stix.entity_prf"]?.toFixed(2), "54.55");
    assert.equal(f.judge, 80);
    assert.ok(Math.abs((f.cost ?? 0) - 0.2) < 1e-9);
    assert.equal(f.seconds, 60);
  });

  it("leaves out an entry the headlines leave out", () => {
    const f = runFigures(cells.get("r2"));
    assert.equal(f.entries, 2);
    assert.equal(f.counted, 1);
    assert.equal(f.leftOut, 1);
    assert.deepEqual(f.counts["stix.entity_prf"], { tp: 2, fp: 0, fn: 1 });
  });

  it("measures a change on the entries both runs count", () => {
    const shared = sharedCounted(cells.get("r3"), cells.get("r2"));
    assert.deepEqual([...shared], ["e1"]);
    assert.equal(runFigures(cells.get("r3"), shared).counted, 1);
  });

  it("reads one cell, null when left out", () => {
    assert.equal(cellValue(cells.get("r2")?.get("e1"), "stix.entity_prf")?.toFixed(1), "80.0");
    assert.equal(cellValue(cells.get("r2")?.get("e2"), "stix.entity_prf"), null);
  });

  it("gives precision and recall on 0-100", () => {
    assert.equal(precision({ tp: 3, fp: 1, fn: 0 }), 75);
    assert.equal(recall({ tp: 1, fp: 0, fn: 3 }), 25);
    assert.equal(precision({ tp: 0, fp: 0, fn: 2 }), 100);
  });
});

describe("reviewEntries", () => {
  it("keeps the reports every run answered, in dataset order", () => {
    assert.deepEqual(reviewEntries(COMPARISON, cells, ["r3"]).map((e) => e.key), ["e1", "e2"]);
    assert.deepEqual(reviewEntries(COMPARISON, cells, ["r3", "o3"]).map((e) => e.key), ["e1"]);
  });
});

describe("causes", () => {
  // APT29 named Cozy Bear (an alias near-miss), Phishing missing, a spurious domain twice.
  const model = buildModel({
    objects: [
      { t: "intrusion-set", n: "APT29", c: "intrusion-set--1", a: ["Cozy Bear"] },
      { t: "attack-pattern", n: "Phishing", c: "attack-pattern--1" },
      { t: "intrusion-set", n: "Cozy Bear", c: "intrusion-set--2" },
      { t: "domain-name", n: "evil.com", c: "domain-name--1" },
    ],
    groups: [0, 1, 0, 2, 2],
    sources: { [EXPECTED]: [0, 1], b: [2, 3, 3] },
    relationships: [],
  });

  it("counts one answer's errors by cause", () => {
    assert.deepEqual(entryCauses(model, "b"), { alias: 1, missing: 1, spurious: 1, duplicates: 1 });
  });

  it("adds them up, near-miss kinds first, two errors per pair", () => {
    const s = causeSummary([entryCauses(model, "b"), { alias: 1, "attack-id": 3, missing: 1 }]);
    assert.deepEqual(s.rows.map((r) => [r.key, r.count, r.unit]), [
      ["attack-id", 3, "pairs"],
      ["alias", 2, "pairs"],
      ["missing", 2, "objects"],
      ["spurious", 1, "objects"],
      ["duplicates", 1, "extra"],
    ]);
    assert.equal(s.nearPairs, 5);
    assert.equal(s.errors, 10 + 2 + 1 + 1);
  });
});

describe("presets", () => {
  it("names the comparisons around the run's agent and model", () => {
    const figure = (id: string) => runFigures(cells.get(id)).value["stix.entity_prf"];
    const list = presets(runs, R3, figure);
    assert.deepEqual(list.map((p) => [p.kind, p.ref, p.under]), [
      ["first-vs-expected", EXPECTED, "r1"],
      ["latest-vs-expected", EXPECTED, "r3"],
      ["first-to-latest", "r1", "r3"],
      ["previous-to-latest", "r2", "r3"],
      ["rerun", "r2", "r3"],
      ["best-other", "r3", "o3"],
      ["latest-alone", NONE, "r3"],
    ]);
  });
});

describe("reviewResultIds", () => {
  it("loads the pair, the comparator, then the history newest first, at most the cap", () => {
    const resultOf = (id: string) => `${id}-e1`;
    assert.deepEqual(reviewResultIds(resultOf, { under: "r3", ref: EXPECTED }, "r2", ["r1", "r2", "r3"]), ["r3-e1", "r2-e1", "r1-e1"]);
    assert.deepEqual(reviewResultIds(resultOf, { under: "r3", ref: "o3" }, null, ["r1", "r2", "r3"], 2), ["r3-e1", "o3-e1"]);
  });
});
