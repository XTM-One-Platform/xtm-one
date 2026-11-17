/**
 * The chat chart block as the chat reads it - `yarn test:unit`.
 *
 * The block is the one `services/chat_charts.py` stores in an answer: a fenced
 * `xtm-chart` block the chat draws, or shows as code when it is not one (the
 * streamed `[[CHART:<id>]]` marker is `answerMarkers.ts`'s).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CHART_FENCE_LANGUAGE,
  chartRows,
  formatChartValue,
  parseChartBlock,
  pieRows,
  type ChatChartBlock,
} from "./chat-chart.ts";

const BLOCK: ChatChartBlock = {
  type: "chart",
  kind: "line",
  categories: ["2026-09-28", "2026-10-05"],
  series: [
    { name: "model-one", values: [1.5, 2.25] },
    { name: "model-two", values: [0, 4] },
  ],
  unit: "USD",
  format: "currency",
  title: "Cost",
};

test("the fence language is the backend's", () => {
  assert.equal(CHART_FENCE_LANGUAGE, "xtm-chart");
});

test("a valid block parses, anything else stays code", () => {
  assert.deepEqual(parseChartBlock(JSON.stringify(BLOCK)), BLOCK);
  assert.equal(parseChartBlock("not json"), null);
  assert.equal(parseChartBlock(JSON.stringify({ ...BLOCK, kind: "gauge" })), null);
  assert.equal(parseChartBlock(JSON.stringify({ ...BLOCK, series: [{ name: "x", values: [1] }] })), null);
  assert.equal(parseChartBlock(JSON.stringify({ ...BLOCK, series: [{ name: "x", values: [1, null] }] })), null);
});

test("rows are one object per category keyed by series", () => {
  assert.deepEqual(chartRows(BLOCK), [
    { category: "2026-09-28", "model-one": 1.5, "model-two": 0 },
    { category: "2026-10-05", "model-one": 2.25, "model-two": 4 },
  ]);
  assert.deepEqual(pieRows({ ...BLOCK, kind: "pie", series: [BLOCK.series[0]] }), [
    { name: "2026-09-28", value: 1.5 },
    { name: "2026-10-05", value: 2.25 },
  ]);
});

test("values are written in the block's format", () => {
  assert.equal(formatChartValue(1.5, BLOCK, "en"), "$1.50");
  assert.equal(formatChartValue(12, { ...BLOCK, unit: "CHF" }, "en"), "CHF 12");
  assert.equal(formatChartValue(86, { ...BLOCK, format: "percent" }, "en"), "86%");
  assert.equal(formatChartValue(1204, { ...BLOCK, format: "number", unit: "traces" }, "en"), "1,204 traces");
  assert.equal(formatChartValue(48, { ...BLOCK, format: "number", unit: "h" }, "en"), "48h");
});
