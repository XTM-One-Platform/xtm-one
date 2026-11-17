/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/** The extractor catalog, mirrored from `backend/app/services/evaluation/extraction/spec.py`.
 *
 * Both halves are checked against one table, `tests_e2e/fixtures/extractor-catalog.json`,
 * which `backend/tests/test_evaluation_extraction.py` generates from the
 * Python source and `tests_e2e/evaluation-extractor.spec.ts` asserts this module
 * against. A drift in either direction fails a test rather than a run.
 */

import type { ExtractorOperation, ExtractorSpec } from "@/types";

export interface OperationMeta {
  op: ExtractorOperation["op"];
  /** The value types it takes and gives, as codes: shown through `EXTRACTOR_VALUE_TYPE_LABELS`. */
  accepts: ExtractorValueType;
  produces: ExtractorValueType;
  needsPath?: boolean;
}

/** Eight first-order operations: one value in, one value out. */
export const EXTRACTOR_OPERATIONS: OperationMeta[] = [
  { op: "find_json", accepts: "text", produces: "text" },
  { op: "parse_json", accepts: "text", produces: "object or array" },
  { op: "select_path", accepts: "object", produces: "any", needsPath: true },
  { op: "object_keys", accepts: "object", produces: "array" },
  { op: "object_values", accepts: "object", produces: "array" },
  { op: "collect_keys", accepts: "array", produces: "array", needsPath: true },
  { op: "flatten", accepts: "array", produces: "array" },
  { op: "dedupe_sort", accepts: "array", produces: "label set" },
];

/** Each operation's name, read by `t_i18n`: catalog keys `validate-i18n` checks.
 * @i18n-keys */
export const EXTRACTOR_OPERATION_LABELS = {
  find_json: "Find JSON in text",
  parse_json: "Parse JSON",
  select_path: "Select path",
  object_keys: "Collect keys",
  object_values: "Collect values",
  collect_keys: "Collect keys at path, per item",
  flatten: "Flatten one level",
  dedupe_sort: "Deduplicate and sort",
} as const satisfies Record<ExtractorOperation["op"], string>;

/** The value types an operation takes and gives, and an extractor's output kinds, read by
 * `t_i18n`. @i18n-keys */
export const EXTRACTOR_VALUE_TYPE_LABELS = {
  text: "text",
  object: "object",
  array: "array",
  any: "any",
  "object or array": "object or array",
  "label set": "label set",
} as const;

export type ExtractorValueType = keyof typeof EXTRACTOR_VALUE_TYPE_LABELS;

/** An output kind as a value type: `label_set` reads "label set". */
export const EXTRACTOR_OUTPUT_KIND_TYPES = {
  object: "object",
  array: "array",
  label_set: "label set",
} as const satisfies Record<ExtractorSpec["output_kind"], ExtractorValueType>;

export const EXTRACTOR_OUTPUT_KINDS: ExtractorSpec["output_kind"][] = [
  "object",
  "array",
  "label_set",
];

export const EXTRACTOR_SOURCE_KINDS: ExtractorSpec["source"]["kind"][] = [
  "agent_output",
  "tool_output",
  "tool_input",
];

/** Read by `t_i18n`. @i18n-keys */
export const EXTRACTOR_SOURCE_LABELS = {
  agent_output: "The agent's answer",
  tool_output: "A tool call's output",
  tool_input: "A tool call's input",
} as const satisfies Record<ExtractorSpec["source"]["kind"], string>;

export const EMPTY_EXTRACTOR: ExtractorSpec = {
  source: { kind: "agent_output" },
  operations: [{ op: "parse_json" }],
  output_kind: "object",
};

/**
 * The extractor an agent's switch turns on, producing an output kind its family scores:
 * a CLASSIFICATION evaluation scores a label set, never the default object.
 * `outputKinds` is the family's list as `GET /evaluations/families` serves it; with none
 * known, the default stands.
 */
export function emptyExtractorFor(outputKinds?: readonly ExtractorSpec["output_kind"][]): ExtractorSpec {
  if (!outputKinds?.length || outputKinds.includes(EMPTY_EXTRACTOR.output_kind)) return EMPTY_EXTRACTOR;
  const first = EXTRACTOR_OUTPUT_KINDS.find((kind) => outputKinds.includes(kind));
  return first ? { ...EMPTY_EXTRACTOR, output_kind: first } : EMPTY_EXTRACTOR;
}

export function operationMeta(op: ExtractorOperation["op"]): OperationMeta | undefined {
  return EXTRACTOR_OPERATIONS.find((entry) => entry.op === op);
}
