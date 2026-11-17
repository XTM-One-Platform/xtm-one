/**
 * Which badges a run's recorded mode shows - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { evaluationExecutionMode, runModeBadges } from "./run-mode.ts";

test("a run that ran in LLM Only is labeled, and only that run", () => {
  assert.equal(runModeBadges({ execution_mode: "llm_only" }).llmOnly, true);
  for (const mode of ["full_loop", "skip_llm", null, undefined]) {
    assert.equal(runModeBadges({ execution_mode: mode }).llmOnly, false, String(mode));
  }
});

test("fallen-back entries are counted when there are any", () => {
  assert.equal(runModeBadges({ execution_mode: "full_loop", fallback_entries: 3 }).fallbackEntries, 3);
  assert.equal(runModeBadges({ execution_mode: "full_loop", fallback_entries: 0 }).fallbackEntries, 0);
  assert.equal(runModeBadges({ execution_mode: "full_loop" }).fallbackEntries, 0);
  assert.equal(runModeBadges({ fallback_entries: -1 }).fallbackEntries, 0);
});

test("a stored mode that is neither value runs, and is edited, as LLM Only", () => {
  assert.equal(evaluationExecutionMode("full_loop"), "full_loop");
  for (const stored of ["llm_only", "full-loop", "", null, undefined]) {
    assert.equal(evaluationExecutionMode(stored), "llm_only", String(stored));
  }
});
