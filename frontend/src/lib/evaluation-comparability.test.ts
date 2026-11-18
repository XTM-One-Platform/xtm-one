import { test } from "node:test";
import assert from "node:assert/strict";

import {
  comparabilityRows,
  formatSeconds,
  headlineMetrics,
  isFairSample,
  largestGap,
  limitHits,
  onlyInCounts,
  tiedNames,
  unreadableCount,
} from "./evaluation-comparability.ts";
import { cellIndex } from "./evaluation-compare.ts";
import type { EvaluationComparison } from "../types/index.ts";

const t = (key: string, values?: Record<string, string | number>) =>
  key
    .replace(/\{(\w+), plural, one \{([^}]*)\} other \{([^}]*)\}\}/g, (_, k: string, one: string, other: string) =>
      (values?.[k] === 1 ? one : other).replace("#", String(values?.[k])),
    )
    .replace(/\{(\w+)\}/g, (_, k: string) => String(values?.[k] ?? `{${k}}`));

const run = (id: string, evaluationId: string, model = "m1") => ({
  id, evaluation_id: evaluationId, subject_id: "s", subject_name: "S", model_id: model, is_default_model: true,
  created_at: null, metric_suite_version: "1", execution_mode: "full_loop", quality_score: null, completed_entries: 1, total_entries: 1,
});

const evaluation = (id: string, timeout: number, runs: ReturnType<typeof run>[]) => ({
  id, name: id.toUpperCase(), family: "STIX_BUNDLE" as const, execution_mode: "full_loop", timeout_seconds: timeout, judge_on: false,
  evaluator_model: null, judge_weights: null, judge_instructions_digest: null, subjects: [],
  datasets: [{ id: `d-${id}`, name: `Set ${id}`, entry_count: 2, instructions_digest: null, description: id === "b" ? "PDF reports" : null }],
  runs, run_choices: runs,
});

const member = (evaluationId: string) => ({ evaluation_id: evaluationId, entry_id: null, position: 0, has_file: false });

const COMPARISON: EvaluationComparison = {
  family: "STIX_BUNDLE",
  metrics: [
    { key: "stix.envelope_validity", label: "Validity", direction: null },
    { key: "stix.entity_prf", label: "Entity F1", direction: null },
    { key: "stix.relationship_prf", label: "Relationship F1", direction: null },
    { key: "stix.bundle_f1", label: "Bundle F1", direction: null },
  ],
  evaluations: [evaluation("a", 100, [run("ra", "a")]), evaluation("b", 1000, [run("rb", "b")])],
  entries: [
    { key: "k1", label: "one", members: [member("a"), member("b")] },
    { key: "k2", label: "two", members: [member("b")] },
    { key: "k3", label: "three", members: [member("b")] },
  ],
  cells: [
    { run_id: "ra", entry_key: "k1", result_id: "x", counted: true, anomaly: null, metrics: {}, cost: null, seconds: 99, unreadable: true },
    { run_id: "rb", entry_key: "k1", result_id: "y", counted: true, anomaly: null, metrics: {}, cost: null, seconds: 10 },
    { run_id: "rb", entry_key: "k2", result_id: "z", counted: false, anomaly: "x", metrics: {}, cost: null, seconds: 1000, unreadable: true },
  ],
};

test("the headline metrics follow the family's order, every metric otherwise", () => {
  assert.deepEqual(headlineMetrics(COMPARISON).map((m) => m.key), ["stix.bundle_f1", "stix.entity_prf", "stix.relationship_prf"]);
  assert.equal(headlineMetrics({ ...COMPARISON, family: "PROSE" as never }).length, 4);
});

test("entries only one evaluation has are counted per evaluation", () => {
  assert.deepEqual(onlyInCounts(COMPARISON), [0, 2]);
});

test("an answer at 98% of its time limit or more reached it, on the shared entries only", () => {
  assert.equal(limitHits(COMPARISON, cellIndex(COMPARISON), ["k1"]), 1);
  assert.equal(limitHits(COMPARISON, cellIndex(COMPARISON), ["k1", "k2"]), 2);
});

test("an unreadable answer counts unless its result is left out", () => {
  const cells = cellIndex(COMPARISON);
  assert.equal(unreadableCount(cells, "ra", ["k1"]), 1);
  assert.equal(unreadableCount(cells, "rb", ["k1", "k2"]), 0);
});

test("times read in seconds under two minutes, in minutes above", () => {
  assert.equal(formatSeconds(65.4), "65 s");
  assert.equal(formatSeconds(600), "10 min");
  assert.equal(formatSeconds(null), "—");
});

test("the shared entries are a fair sample when every gap stays under four points", () => {
  const gap = largestGap([{ shared: 50, other: 47 }, { shared: 40, other: 42 }, { shared: null, other: 10 }]);
  assert.equal(gap, 3);
  assert.equal(isFairSample(gap as number), true);
  assert.equal(isFairSample(largestGap([{ shared: 50, other: 60 }]) as number), false);
  assert.equal(largestGap([]), null);
});

test("tied names stop at six", () => {
  assert.equal(tiedNames(["a", "b"]), "a, b");
  assert.equal(tiedNames(["1", "2", "3", "4", "5", "6", "7"]), "1, 2, 3, 4, 5, 6…");
});

test("the comparability rows say the family, the entries left out, the models in common and the limits reached", () => {
  const rows = comparabilityRows(COMPARISON, 1, t, (m) => m, cellIndex(COMPARISON));
  const row = (name: string) => rows.find((r) => r.dimension === name)!;
  assert.equal(rows[0].dimension, "Family");
  assert.equal(row("Family").verdict, "same");
  assert.equal(row("Entries").pill, "1 shared");
  assert.deepEqual(row("Entries").values, ["1 · “Set a”", "3 · “Set b”"]);
  assert.deepEqual(row("Entries").details, ["", "PDF reports"]);
  assert.match(row("Entries").note, /0 only in the first, 2 only in the second\.$/);
  assert.equal(row("Models").pill, "1 in common");
  assert.deepEqual(row("Models").values, ["1", "1"]);
  assert.equal(row("Run limits").verdict, "confound");
  assert.equal(row("Run limits").note, "1 answer on the shared entries reached its limit.");
});
