/**
 * Unit tests for the coverage and stats of the CTEM command center - `yarn test:unit`.
 *
 * A cell says whether a connected module feeds the stage, a primary source not
 * connected is a blind spot with its connect path, and every stat is read from
 * the posture and its recorded history, `null` when it cannot be, never 0.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { coverageMatrix, exposureStats, type CoveragePostureInput } from "./ctem-coverage-stats.ts";

const posture = (over: Partial<CoveragePostureInput> = {}): CoveragePostureInput => ({
  posture_score: 62,
  stages: [
    { key: "scoping", label: "Scoping", score: 70, covered: true, primary_products: ["opencti", "openaev"], secondary_products: [] },
    { key: "discovery", label: "Discovery", score: 55, covered: true, primary_products: ["opencti"], secondary_products: [] },
    { key: "validation", label: "Validation", score: null, covered: false, primary_products: ["opencti", "openbas"], secondary_products: [] },
    { key: "remediation", label: "Remediation", score: 40, covered: true, primary_products: ["opencti"], secondary_products: ["openaev"] },
  ],
  products: { opencti: { present: true }, openaev: { present: false }, opencrq: { present: false } },
  ...over,
});

test("a cell is live, missing, optional or none from the stage's sources and the connected modules", () => {
  const m = coverageMatrix(posture());
  assert.ok(m);
  const cells = Object.fromEntries(m.rows.map((r) => [r.key, r.cells]));
  assert.deepEqual(cells.scoping, { opencti: "live", openaev: "missing", opencrq: "none" });
  assert.deepEqual(cells.discovery, { opencti: "live", openaev: "none", opencrq: "none" });
  // openbas is OpenAEV's former identifier.
  assert.deepEqual(cells.validation, { opencti: "live", openaev: "missing", opencrq: "none" });
  assert.deepEqual(cells.remediation, { opencti: "live", openaev: "optional", opencrq: "none" });
});

test("the connect path lists the primary sources not connected, the counts read the posture", () => {
  const m = coverageMatrix(posture());
  assert.ok(m);
  assert.deepEqual(
    m.rows.map((r) => [r.key, r.missing]),
    [
      ["scoping", ["openaev"]],
      ["discovery", []],
      ["validation", ["openaev"]],
      ["remediation", []],
    ],
  );
  assert.equal(m.covered, 3);
  assert.deepEqual(m.connected, ["opencti"]);
  assert.deepEqual(m.feedsAnyStage, { opencti: true, openaev: true, opencrq: false });
});

test("with every module connected nothing is missing", () => {
  const m = coverageMatrix(
    posture({ products: { opencti: { present: true }, openbas: { present: true }, opencrq: { present: true } } }),
  );
  assert.ok(m);
  assert.ok(m.rows.every((r) => r.missing.length === 0));
  assert.deepEqual(m.connected, ["opencti", "openaev", "opencrq"]);
});

test("no posture read yet gives no matrix and no stats", () => {
  assert.equal(coverageMatrix(undefined), null);
  assert.equal(exposureStats(null, []), null);
});

test("the change is the score now against the first recorded day that has one", () => {
  const s = exposureStats(posture(), [
    { date: "2026-08-10", posture_score: null, stage_metrics: {} },
    { date: "2026-08-11", posture_score: 50, stage_metrics: { scoping: { score: 60 }, discovery: { score: 58 } } },
    { date: "2026-11-06", posture_score: 62, stage_metrics: { scoping: { score: 70 }, discovery: { score: 55 } } },
  ]);
  assert.ok(s);
  assert.equal(s.postureStart, 50);
  assert.equal(s.postureChange, 12);
  assert.deepEqual(s.postureSeries, [null, 50, 62]);
  assert.equal(s.daysRecorded, 3);
  assert.equal(s.since, "2026-08-10");
  const byKey = Object.fromEntries(s.stages.map((t) => [t.key, t]));
  assert.equal(byKey.scoping.change, 10);
  assert.equal(byKey.discovery.change, -3);
  // No score now, or no recorded score: the change is unknown, not 0.
  assert.equal(byKey.validation.change, null);
  assert.equal(byKey.remediation.startScore, null);
  assert.equal(byKey.remediation.change, null);
  assert.equal(s.improved, 1);
  assert.equal(s.declined, 1);
});

test("a single recorded day gives unknown changes, never 0", () => {
  const s = exposureStats(posture(), [
    { date: "2026-11-06", posture_score: 62, stage_metrics: { scoping: { score: 70 }, discovery: { score: 55 } } },
  ]);
  assert.ok(s);
  assert.equal(s.daysRecorded, 1);
  assert.equal(s.since, "2026-11-06");
  assert.equal(s.postureStart, 62);
  assert.equal(s.postureChange, null);
  assert.ok(s.stages.every((t) => t.change === null));
  assert.equal(s.improved, 0);
  assert.equal(s.declined, 0);
});

test("no history gives unknown changes and no start date", () => {
  const s = exposureStats(posture(), undefined);
  assert.ok(s);
  assert.equal(s.postureChange, null);
  assert.equal(s.since, null);
  assert.equal(s.daysRecorded, 0);
  assert.ok(s.stages.every((t) => t.change === null && t.series.length === 0));
});
