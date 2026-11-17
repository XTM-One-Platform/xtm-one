/**
 * A run metric's value is formatted by the unit the package declares -
 * `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { formatRunMetricValue } from "./run-metric-format.ts";

test("a missing value reads as a dash, whatever the unit", () => {
  assert.equal(formatRunMetricValue("performance.latency.ttft", null, "milliseconds"), "—");
  assert.equal(formatRunMetricValue("judge_seconds", null), "—");
});

test("each declared unit has its own form", () => {
  assert.equal(formatRunMetricValue("performance.latency.ttft", 925, "milliseconds"), "0.93s");
  assert.equal(formatRunMetricValue("performance.reliability.completion_rate", 1, "ratio"), "100.0%");
  assert.equal(formatRunMetricValue("performance.cost.llm", 0.04214, "currency"), "$0.0421");
  assert.equal(formatRunMetricValue("performance.tokens.total", 4486, "tokens"), (4486).toLocaleString());
  assert.equal(formatRunMetricValue("performance.counts.tool_calls", 3, "count"), "3");
});

test("the unit decides, not the key: a metric under any key is formatted by its unit", () => {
  assert.equal(formatRunMetricValue("performance.example.duration", 1500, "milliseconds"), "1.50s");
  assert.equal(formatRunMetricValue("performance.example.cost", 0.5, "currency"), "$0.5000");
});

test("a mean of tokens is rounded-sm, a mean of counts keeps one decimal", () => {
  const oneDecimal = (n: number) => n.toLocaleString(undefined, { maximumFractionDigits: 1 });
  assert.equal(formatRunMetricValue("performance.tokens.total", 4486.5, "tokens"), (4487).toLocaleString());
  assert.equal(formatRunMetricValue("performance.counts.tool_calls", 2.5, "count"), oneDecimal(2.5));
  // No pointless trailing zero, and a large count keeps its separators.
  assert.equal(formatRunMetricValue("performance.counts.tool_calls", 2.95, "count"), "3");
  assert.equal(formatRunMetricValue("performance.counts.model_calls", 1234.5, "count"), oneDecimal(1234.5));
});

test("the platform's own keys declare no unit and are read by name", () => {
  assert.equal(formatRunMetricValue("judge_seconds", 1.234, null), "1.23s");
  assert.equal(formatRunMetricValue("run_duration_seconds", 29.174), "29.17s");
  assert.equal(formatRunMetricValue("cost_usd", 0.01, null), "$0.0100");
});

test("a quality score has no unit and keeps the plain form", () => {
  assert.equal(formatRunMetricValue("prose.rouge_l", 87.456, null), "87.46");
  assert.equal(formatRunMetricValue("prose.exact_match", 100, null), (100).toLocaleString());
});

test("a declared unit wins over a name that suggests another", () => {
  assert.equal(formatRunMetricValue("odd_seconds", 1500, "milliseconds"), "1.50s");
});
