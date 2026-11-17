/**
 * What the creation screen adds to the create request - `yarn test:unit`: the
 * overall metric selection and each agent's output extraction, as a later edit of the
 * Configuration tab would save them.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { creationExtras } from "./creation-payload.ts";
import type { ExtractorSpec } from "../../types/index.ts";

const labels: ExtractorSpec = {
  source: { kind: "agent_output" },
  operations: [{ op: "find_json" }, { op: "parse_json" }],
  output_kind: "label_set",
};
const fromTool: ExtractorSpec = {
  source: { kind: "tool_output", tool_name: "search_cases" },
  operations: [{ op: "parse_json" }],
  output_kind: "object",
};

test("the selections and every created agent's extractor are sent", () => {
  assert.deepEqual(
    creationExtras({
      agentIds: ["a1", "a2"],
      agentExtractors: { a1: labels, a2: fromTool },
      qualitySelection: ["classification.micro_prf"],
      performanceSelection: [],
    }),
    {
      agent_extractors: { a1: labels, a2: fromTool },
      overall_quality_metrics: ["classification.micro_prf"],
      overall_performance_metrics: [],
    },
  );
});

test("an agent removed from the screen takes its extractor out of the request", () => {
  const extras = creationExtras({
    agentIds: ["a2"],
    agentExtractors: { a1: labels, a2: fromTool },
    qualitySelection: null,
    performanceSelection: null,
  });

  assert.deepEqual(extras.agent_extractors, { a2: fromTool });
});

test("an extractor switched off, or none at all, sends no extraction", () => {
  const cases: Record<string, ExtractorSpec | null>[] = [{ a1: null }, {}];
  for (const agentExtractors of cases) {
    const extras = creationExtras({
      agentIds: ["a1"],
      agentExtractors,
      qualitySelection: null,
      performanceSelection: null,
    });
    assert.equal(extras.agent_extractors, undefined);
  }
});

test("a selection left on its defaults is sent as inherit, an empty one as none", () => {
  const extras = creationExtras({
    agentIds: [],
    agentExtractors: {},
    qualitySelection: null,
    performanceSelection: [],
  });

  // `null` inherits the family's default metrics; `[]` composes the headline from nothing.
  assert.equal(extras.overall_quality_metrics, null);
  assert.deepEqual(extras.overall_performance_metrics, []);
});
