import { test } from "node:test";
import assert from "node:assert/strict";

import {
  bootstrap,
  cellIndex,
  comparisonEligibility,
  pairedChange,
  pairedCounts,
  perEntryMean,
  pooled,
  rankSpread,
  runCounts,
  sharedEntries,
} from "./evaluation-compare.ts";
import type { EvaluationComparison } from "../types/index.ts";

test("a pooled figure adds the entries' counts up, as the run's headline does", () => {
  // 1/2 and 9/10 pool to 10/12, not to the mean of 50 and 90.
  assert.equal(pooled([[1, 2], [9, 10]])?.toFixed(2), "83.33");
  assert.equal(pooled([]), null);
  assert.equal(pooled([[0, 0]]), 100);
});

test("a bootstrap interval holds the value and is stable for a seed", () => {
  const counts: Array<[number, number]> = [[2, 10], [8, 10], [5, 10], [9, 10], [1, 10]];
  const first = bootstrap(counts, 7, 500);
  const again = bootstrap(counts, 7, 500);
  assert.ok(first && again);
  assert.deepEqual(first, again);
  assert.ok(first.low <= first.value && first.value <= first.high);
  assert.ok(first.high - first.low > 10, "five uneven entries leave a wide interval");
});

test("a paired change holds only when every resample moves the same way", () => {
  const before: Array<[number, number]> = [[2, 10], [3, 10], [4, 10], [2, 10], [3, 10]];
  const clearlyBetter: Array<[number, number]> = [[8, 10], [9, 10], [8, 10], [9, 10], [8, 10]];
  const mixed: Array<[number, number]> = [[9, 10], [0, 10], [8, 10], [0, 10], [3, 10]];
  const up = pairedChange(before, clearlyBetter, 3, 500);
  const noise = pairedChange(before, mixed, 3, 500);
  assert.ok(up && noise);
  assert.equal(up.holds, true);
  assert.ok(up.change > 50);
  assert.equal(noise.holds, false);
  assert.equal(pairedChange(before, mixed.slice(1), 3), null);
});

test("ranks spread over the runs whose intervals overlap", () => {
  const rows = rankSpread([
    { id: "a", value: 80, low: 70, high: 90 },
    { id: "b", value: 75, low: 60, high: 85 },
    { id: "c", value: 20, low: 10, high: 30 },
  ]);
  assert.deepEqual(rows.map((r) => [r.id, r.best, r.worst]), [["a", 1, 2], ["b", 1, 2], ["c", 3, 3]]);
});

const comparison: EvaluationComparison = {
  family: "CLASSIFICATION",
  metrics: [{ key: "m", label: "M", direction: "higher_is_better" }],
  evaluations: [],
  entries: [
    { key: "k1", label: "one", members: [{ evaluation_id: "A", entry_id: "1", position: 0, has_file: false }, { evaluation_id: "B", entry_id: "3", position: 0, has_file: true }] },
    { key: "k2", label: "two", members: [{ evaluation_id: "A", entry_id: "2", position: 1, has_file: false }] },
  ],
  cells: [
    { run_id: "ra", entry_key: "k1", result_id: "x", counted: true, anomaly: null, metrics: { m: [1, 2] }, cost: 0.4, seconds: 10 },
    { run_id: "ra", entry_key: "k2", result_id: "y", counted: true, anomaly: null, metrics: { m: [2, 2] }, cost: null, seconds: 30 },
    { run_id: "rb", entry_key: "k1", result_id: "z", counted: true, anomaly: null, metrics: { m: [2, 2] }, cost: 0.2, seconds: 5 },
  ],
};

test("runs are read on the entries both evaluations share", () => {
  const cells = cellIndex(comparison);
  const shared = sharedEntries(comparison.entries, ["A", "B"]).map((e) => e.key);
  assert.deepEqual(shared, ["k1"]);
  assert.deepEqual(runCounts(cells, "ra", "m", ["k1", "k2"]), [[1, 2], [2, 2]]);
  assert.deepEqual(pairedCounts(cells, "ra", "rb", "m", ["k1", "k2"]), { keys: ["k1"], before: [[1, 2]], after: [[2, 2]] });
  assert.equal(perEntryMean(cells, "ra", "cost", ["k1", "k2"]), 0.4);
  assert.equal(perEntryMean(cells, "ra", "seconds", ["k1", "k2"]), 20);
});

test("only two to four evaluations of one family can be compared", () => {
  const stix = { family: "STIX_BUNDLE" };
  assert.equal(comparisonEligibility([stix]).canCompare, false);
  assert.deepEqual(comparisonEligibility([stix, stix]), { canCompare: true, reason: null });
  assert.equal(comparisonEligibility([stix, { family: "PROSE" }]).reason, "Only evaluations of the same family can be compared.");
  assert.equal(comparisonEligibility([stix, { family: null }]).canCompare, false);
  assert.equal(comparisonEligibility([stix, stix, stix, stix, stix]).canCompare, false);
});
