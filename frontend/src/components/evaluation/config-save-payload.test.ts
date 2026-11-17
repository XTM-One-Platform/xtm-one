/**
 * What the Configuration tab's save sends - `yarn test:unit`. The update
 * clears a nullable setting sent as null, so the page sends null only to clear,
 * and leaves out what it does not edit.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { configSavePayload, type ConfigSaveState } from "./config-save-payload.ts";

const filled: ConfigSaveState = {
  name: "Eval",
  description: "About it",
  family: "STRUCTURED_JSON",
  evalInstructions: "Ignore casing",
  executionMode: "full_loop",
  runMode: "parallel",
  evaluatorModel: "judge-model",
  performanceTolerance: 5,
  penaltyFactor: 0.5,
  qualitySelection: ["json.field_accuracy"],
  performanceSelection: [],
  timeoutSeconds: 3600,
  judgeMaxTokens: 8192,
  fieldScoringOverrides: { field_modes: { "/severity": "exact" } },
  agentExtractors: {},
  judgeEnabled: true,
  reuseDefault: true,
  applyPromptGuards: false,
  requireToolApproval: false,
  scheduleEnabled: true,
  schedule: "0 0 * * 1",
  onAgentChange: false,
  automaticRuns: true,
  useAgentDefaultModel: true,
  modelIds: ["model-a"],
  disabledModelIds: [],
};

const cleared: ConfigSaveState = {
  ...filled,
  description: "",
  family: "",
  evaluatorModel: "",
  qualitySelection: null,
  performanceSelection: null,
  fieldScoringOverrides: null,
  scheduleEnabled: false,
};

const nullKeys = (payload: object) =>
  Object.entries(payload)
    .filter(([, value]) => value === null)
    .map(([key]) => key)
    .sort();

test("null is sent only for the settings the save clears", () => {
  assert.deepEqual(nullKeys(configSavePayload(filled)), []);
  assert.deepEqual(nullKeys(configSavePayload(cleared)), [
    "description",
    "evaluator_model",
    "field_scoring_overrides",
    "overall_performance_metrics",
    "overall_quality_metrics",
  ]);
});

test("an empty family is left out, never cleared: an evaluation with no family cannot run", () => {
  assert.equal(configSavePayload(cleared).family, undefined);
});

test("the settings the page does not edit are never sent", () => {
  const payload = configSavePayload(filled);
  for (const key of ["tags", "custom_params", "sub_score_weights", "evaluator_prompt", "quality_weight", "quality_source"]) {
    assert.equal(key in payload, false, key);
  }
});

test("only the triggers the page edits are sent, for the update to merge", () => {
  assert.deepEqual(configSavePayload(filled).trigger_config, { schedule: "0 0 * * 1", on_agent_change: false });
  assert.deepEqual(configSavePayload(cleared).trigger_config, { schedule: null, on_agent_change: false });
});
