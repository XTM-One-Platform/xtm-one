/** `yarn test:unit`: the CSV a table exports. */

import assert from "node:assert/strict";
import { test } from "node:test";

import { csvCell, csvFilename, toCsv } from "./csv.ts";

test("a field holding a comma, a quote or a line break is quoted", () => {
  assert.equal(csvCell("plain"), "plain");
  assert.equal(csvCell("a, b"), '"a, b"');
  assert.equal(csvCell('say "hi"'), '"say ""hi"""');
  assert.equal(csvCell("two\nlines"), '"two\nlines"');
});

test("a text cell a spreadsheet would run as a formula opens as text", () => {
  assert.equal(csvCell("=HYPERLINK(1)"), "'=HYPERLINK(1)");
  assert.equal(csvCell("+1"), "'+1");
  assert.equal(csvCell("-x"), "'-x");
  assert.equal(csvCell("@cmd"), "'@cmd");
  // A number is written as it is, negative or not.
  assert.equal(csvCell(-1.5), "-1.5");
});

test("empty, boolean and non-finite values", () => {
  assert.equal(csvCell(null), "");
  assert.equal(csvCell(undefined), "");
  assert.equal(csvCell(Number.NaN), "");
  assert.equal(csvCell(true), "true");
});

test("rows become CRLF lines under their header", () => {
  const csv = toCsv(
    [
      { name: "Agent, A", cost: 1.25 },
      { name: "B", cost: 0 },
    ],
    [
      { header: "Agent", value: (row) => row.name },
      { header: "Cost (USD)", value: (row) => row.cost },
    ],
  );
  assert.equal(csv, 'Agent,Cost (USD)\r\n"Agent, A",1.25\r\nB,0\r\n');
});

test("the file name is the stem, file-safe, and the day", () => {
  const at = new Date("2026-10-23T08:00:00Z");
  assert.equal(csvFilename("LLM cost by agent", at), "llm-cost-by-agent-2026-10-23.csv");
  assert.equal(csvFilename("  ", at), "export-2026-10-23.csv");
});
