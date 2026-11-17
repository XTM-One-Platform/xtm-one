/**
 * Unit tests for the rating dialog's criteria - `yarn test:unit`.
 *
 * Every criterion type the dialog draws (categorical / numeric / scale), what
 * each input becomes as a score, and what the dialog refuses to send. The
 * server applies the same rules again (`backend/tests/test_feedback_criteria.py`).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { FeedbackCriterion } from "@/types";
import {
  criterionFormDraft,
  criterionFormProblem,
  criterionPayload,
  criterionTimeline,
  criterionUpdatePayload,
  draftFromScores,
  parseScoreDraft,
  scaleSteps,
  scorePercent,
} from "./feedback-criteria.ts";

function criterion(overrides: Partial<FeedbackCriterion>): FeedbackCriterion {
  return {
    id: "c",
    key: "c",
    name: "Criterion",
    description: null,
    type: "scale",
    weight: 1,
    options: null,
    min_value: 1,
    max_value: 5,
    sort_order: 0,
    enabled: true,
    created_at: "2026-09-27T00:00:00Z",
    updated_at: "2026-09-27T00:00:00Z",
    ...overrides,
  };
}

const tone = criterion({
  id: "tone",
  type: "categorical",
  min_value: null,
  max_value: null,
  options: [
    { value: "cold", label: "Cold", score: 0 },
    { value: "warm", label: "Warm", score: 1 },
  ],
});
const accuracy = criterion({ id: "accuracy", type: "scale", min_value: 1, max_value: 5 });
const latency = criterion({ id: "latency", type: "numeric", min_value: 0, max_value: 10 });

test("a scale offers each whole step between its bounds", () => {
  assert.deepEqual(scaleSteps(accuracy), [1, 2, 3, 4, 5]);
  assert.deepEqual(scaleSteps({ min_value: 0, max_value: 0 }), [0]);
  assert.deepEqual(scaleSteps({ min_value: null, max_value: 5 }), []);
  assert.deepEqual(scaleSteps({ min_value: 1.5, max_value: 5 }), []);
  assert.deepEqual(scaleSteps({ min_value: 5, max_value: 1 }), []);
});

test("every criterion type becomes its score", () => {
  const parsed = parseScoreDraft([tone, accuracy, latency], { tone: "warm", accuracy: "4", latency: " 2.5 " });

  assert.deepEqual(parsed.scores, [
    { criterion_id: "tone", value: "warm" },
    { criterion_id: "accuracy", value: 4 },
    { criterion_id: "latency", value: 2.5 },
  ]);
  assert.deepEqual(parsed.problems, {});
});

test("an empty input is no score", () => {
  const parsed = parseScoreDraft([tone, accuracy, latency], { tone: "", latency: "   " });

  assert.deepEqual(parsed, { scores: [], problems: {} });
});

test("a typed number the server would refuse is a problem, not a score", () => {
  assert.deepEqual(parseScoreDraft([latency], { latency: "fast" }), {
    scores: [],
    problems: { latency: "not_a_number" },
  });
  assert.deepEqual(parseScoreDraft([latency], { latency: "11" }), {
    scores: [],
    problems: { latency: "out_of_range" },
  });
  assert.deepEqual(parseScoreDraft([latency], { latency: "-0.1" }).problems, { latency: "out_of_range" });
  assert.deepEqual(parseScoreDraft([latency], { latency: "10" }).scores, [{ criterion_id: "latency", value: 10 }]);
});

test("a stored option or step that no longer exists is dropped", () => {
  const parsed = parseScoreDraft([tone, accuracy], { tone: "hot", accuracy: "7" });

  assert.deepEqual(parsed, { scores: [], problems: {} });
  assert.deepEqual(parseScoreDraft([accuracy], { accuracy: "2.5" }).scores, []);
});

test("only the criteria on offer are read", () => {
  const parsed = parseScoreDraft([accuracy], { accuracy: "3", removed: "5" });

  assert.deepEqual(parsed.scores, [{ criterion_id: "accuracy", value: 3 }]);
});

test("a stored rating's scores fill the draft back", () => {
  const draft = draftFromScores([
    { criterion_id: "tone", value: "cold" },
    { criterion_id: "accuracy", value: 5 },
  ]);

  assert.deepEqual(draft, { tone: "cold", accuracy: "5" });
  assert.deepEqual(parseScoreDraft([tone, accuracy], draft).scores, [
    { criterion_id: "tone", value: "cold" },
    { criterion_id: "accuracy", value: 5 },
  ]);
  assert.deepEqual(draftFromScores(undefined), {});
});

test("a score reads as a whole percentage", () => {
  assert.equal(scorePercent(0.625), 63);
  assert.equal(scorePercent(0), 0);
  assert.equal(scorePercent(1.2), 100);
  assert.equal(scorePercent(null), null);
  assert.equal(scorePercent(undefined), null);
  assert.equal(scorePercent(Number.NaN), null);
});

test("the timeline lists every day up to today, with or without scores", () => {
  const slots = criterionTimeline(
    [{ date: "2026-09-26", count: 2, mean_score: 0.5 }],
    3,
    new Date("2026-09-27T23:30:00Z"),
  );

  assert.deepEqual(
    slots.map((slot) => [slot.date, slot.day?.count ?? 0]),
    [
      ["2026-09-25", 0],
      ["2026-09-26", 2],
      ["2026-09-27", 0],
    ],
  );
  // Across a month boundary.
  assert.deepEqual(
    criterionTimeline([], 2, new Date("2026-10-01T00:00:00Z")).map((slot) => slot.date),
    ["2026-09-30", "2026-10-01"],
  );
});

// ---------------------------------------------------------------------------
// The Settings form
// ---------------------------------------------------------------------------

test("a new criterion starts as a 1-5 scale", () => {
  const form = criterionFormDraft();

  assert.equal(form.type, "scale");
  assert.equal(criterionFormProblem({ ...form, name: "Accuracy" }), null);
  assert.deepEqual(criterionPayload({ ...form, name: " Accuracy " }), {
    name: "Accuracy",
    description: null,
    type: "scale",
    weight: 1,
    options: null,
    min_value: 1,
    max_value: 5,
    enabled: true,
  });
});

test("the form refuses what the server would", () => {
  const form = { ...criterionFormDraft(), name: "Accuracy" };

  assert.equal(criterionFormProblem({ ...form, name: "  " }), "name");
  assert.equal(criterionFormProblem({ ...form, weight: "-1" }), "weight");
  assert.equal(criterionFormProblem({ ...form, weight: "" }), "weight");
  assert.equal(criterionFormProblem({ ...form, minValue: "5", maxValue: "5" }), "bounds");
  assert.equal(criterionFormProblem({ ...form, minValue: "1.5" }), "bounds");
  assert.equal(criterionFormProblem({ ...form, type: "numeric", minValue: "1.5" }), null);
  assert.equal(criterionFormProblem({ ...form, type: "numeric", maxValue: "" }), "bounds");

  const categorical = { ...form, type: "categorical" as const };
  // The two starting options have no value yet.
  assert.equal(criterionFormProblem(categorical), "options");
  const filled = {
    ...categorical,
    options: [
      { value: "cold", label: "Cold", score: "0" },
      { value: "warm", label: "", score: "1" },
    ],
  };
  assert.equal(criterionFormProblem(filled), null);
  assert.equal(criterionFormProblem({ ...filled, options: filled.options.slice(0, 1) }), "options");
  assert.equal(
    criterionFormProblem({ ...filled, options: [filled.options[0], { value: "hot", label: "", score: "1.5" }] }),
    "options",
  );
});

test("a categorical criterion sends its options and no bounds", () => {
  const payload = criterionPayload({
    ...criterionFormDraft(),
    name: "Tone",
    type: "categorical",
    weight: "2.5",
    options: [
      { value: " cold ", label: "Cold", score: "0" },
      { value: "warm", label: " ", score: "1" },
    ],
  });

  assert.deepEqual(payload.options, [
    { value: "cold", label: "Cold", score: 0 },
    { value: "warm", label: null, score: 1 },
  ]);
  assert.equal(payload.weight, 2.5);
  assert.equal(payload.min_value, null);
  assert.equal(payload.max_value, null);
});

test("editing a criterion reads it back and never sends its type", () => {
  const stored = criterion({ id: "latency", type: "numeric", min_value: 0, max_value: 10, weight: 0.5 });
  const form = criterionFormDraft(stored);

  assert.deepEqual(
    [form.type, form.minValue, form.maxValue, form.weight],
    ["numeric", "0", "10", "0.5"],
  );
  const update = criterionUpdatePayload({ ...form, maxValue: "20" });
  assert.equal("type" in update, false);
  assert.equal(update.max_value, 20);
});
