/**
 * Unit tests for the OpenAPI tool name / description overrides.
 *
 * Run with `yarn test:unit` (Node's built-in runner, types stripped natively).
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { overridesFromOperations, overridesPayload, sameOverrides, withOverride } from "./openapi-overrides.ts";

const op = (key: string, fields: Record<string, string | undefined>) =>
  ({ key, method: "GET", path: "/", summary: "", tags: [], deprecated: false, tool_type: "read",
     sends_credential: true, supported: true, warnings: [], selected: true, ...fields }) as never;

test("editing keeps the raw text and drops an emptied override", () => {
  let overrides = withOverride({}, "GET /a", { name: "geo " });
  assert.deepEqual(overrides, { "GET /a": { name: "geo " } });
  overrides = withOverride(overrides, "GET /a", { description: "Search places" });
  assert.deepEqual(overrides["GET /a"], { name: "geo ", description: "Search places" });
  assert.deepEqual(withOverride(overrides, "GET /a", { name: "", description: " " }), {});
});

test("the payload is trimmed and leaves empty fields out", () => {
  assert.deepEqual(overridesPayload({ "GET /a": { name: " geo_search ", description: "" }, "GET /b": { name: " " } }), {
    "GET /a": { name: "geo_search" },
  });
});

test("a stored spec's overrides are where the tool differs from the generated one", () => {
  const operations = [
    op("GET /a", { tool_name: "find", default_tool_name: "api_get_a", tool_description: "d", default_tool_description: "d" }),
    op("GET /b", { tool_name: "api_get_b", default_tool_name: "api_get_b", tool_description: "mine", default_tool_description: "GET /b" }),
    op("GET /c", { tool_name: "api_get_c", default_tool_name: "api_get_c" }),
  ];
  assert.deepEqual(overridesFromOperations(operations), {
    "GET /a": { name: "find" },
    "GET /b": { description: "mine" },
  });
});

test("equality compares what would be sent", () => {
  assert.equal(sameOverrides({ "GET /a": { name: "x " } }, { "GET /a": { name: "x" } }), true);
  assert.equal(sameOverrides({}, { "GET /a": { name: " " } }), true);
  assert.equal(sameOverrides({ "GET /a": { name: "x" } }, { "GET /a": { name: "y" } }), false);
});
