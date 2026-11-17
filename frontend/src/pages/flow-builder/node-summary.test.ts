import assert from "node:assert/strict";
import { test } from "node:test";

import { SUMMARY_MAX_CHARS, summaryValue } from "./node-summary.ts";

test("a JSON Schema reads as the fields it declares, never [object Object]", () => {
  const schema = {
    type: "object",
    properties: { severity: { type: "string" }, confidence: { type: "number" } },
  };
  assert.equal(summaryValue(schema), "severity, confidence");
  assert.equal(summaryValue({ type: "object", properties: {} }), null);
});

test("any other object reads as compact JSON", () => {
  assert.equal(summaryValue({ status: ["open", "planned"] }), '{"status":["open","planned"]}');
  assert.equal(summaryValue({}), null);
});

test("a list reads as its items, each resolved to its option's label", () => {
  const options = [
    { value: "t1", label: "Web search" },
    { value: "t2", label: "OpenCTI" },
  ];
  assert.equal(summaryValue(["t1", "t2", "t9"], options), "Web search, OpenCTI, t9");
  assert.equal(summaryValue([], options), null);
  assert.equal(summaryValue(["", null], options), null);
});

test("a scalar reads as its option's label, else as itself", () => {
  assert.equal(summaryValue("a-1", [{ value: "a-1", label: "Alert Classifier" }]), "Alert Classifier");
  assert.equal(summaryValue("0 9 * * 1-5"), "0 9 * * 1-5");
  assert.equal(summaryValue(42), "42");
  assert.equal(summaryValue(false), "false");
  for (const empty of [undefined, null, ""]) assert.equal(summaryValue(empty), null);
});

test("a long value is cut to one line", () => {
  const value = summaryValue("x".repeat(200));
  assert.equal(value?.length, SUMMARY_MAX_CHARS);
  assert.ok(value?.endsWith("…"));
});
