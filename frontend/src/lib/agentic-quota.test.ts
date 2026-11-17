/**
 * Unit tests for the agentic count a quota indicator shows -
 * `yarn test:unit`.
 *
 * The web chat's composer and the chat embedded in OpenCTI / OpenAEV must show
 * the same number: the embedded one reads it from
 * `GET /api/v1/platform/chat/quota`, computed by the backend twin of this
 * function. Both sides run the table below, picking the agentic total the way
 * `QuotaIndicator` does.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import type { QuotaUsageEntry } from "../types/index.ts";
import { mergeSandboxIntoAgenticCount } from "./agentic-quota.ts";

interface CountCase {
  name: string;
  usage: QuotaUsageEntry[];
  expected: number;
}

const cases = JSON.parse(
  readFileSync(new URL("../../tests_e2e/fixtures/agentic-quota-count-cases.json", import.meta.url), "utf8"),
) as CountCase[];

test("the case table is not empty", () => {
  assert.ok(cases.length > 0);
});

for (const c of cases) {
  test(`agentic count: ${c.name}`, () => {
    const total = c.usage.find((u) => u.usage_type === "agentic" && u.model === null);
    assert.equal(mergeSandboxIntoAgenticCount(c.usage, total), c.expected);
  });
}
