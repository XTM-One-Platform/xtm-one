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

/** Editing a custom tool's declared data cases as JSON. The API validates
 *  each case (`services/custom_tool_tests.py`); this only checks the shape. */

import type { CustomToolTestCase } from "@/types";

export const TEST_CASES_EXAMPLE = `[
  {
    "name": "dry run on example.com",
    "inputs": { "domain": "example.com" },
    "dry_run": true,
    "expect": { "contains": ["example.com"] }
  },
  {
    "name": "refuses an empty domain",
    "inputs": { "domain": "" },
    "expect_error": true
  }
]`;

export function testCodeExample(python: boolean): string {
  return python
    ? `def test_dry_run_changes_nothing():
    out = run_tool({"domain": "example.com"}, dry_run=True)
    assert "would update" in out`
    : `test("dry run changes nothing", async () => {
  const out = await runTool({ domain: "example.com" }, { dryRun: true });
  assert.match(out, /would update/);
});`;
}

export type ParsedTestCases = { ok: true; cases: CustomToolTestCase[] } | { ok: false; error: string };

/** The cases written in *text* (blank: none), or the reason they cannot be read. */
export function parseTestCases(text: string): ParsedTestCases {
  if (!text.trim()) return { ok: true, cases: [] };
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return { ok: false, error: "Invalid JSON. Please check your input." };
  }
  if (!Array.isArray(value)) return { ok: false, error: "Data cases must be a JSON list." };
  if (!value.every((c) => c && typeof c === "object" && !Array.isArray(c) && typeof c.name === "string" && c.name.trim())) {
    return { ok: false, error: "Every data case needs a name." };
  }
  return { ok: true, cases: value as CustomToolTestCase[] };
}
