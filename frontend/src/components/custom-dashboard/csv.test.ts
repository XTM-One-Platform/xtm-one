/**
 * Unit tests for the CSV export - `yarn test:unit`.
 *
 * Every shape becomes rows; no cell can run as a formula in a spreadsheet,
 * and quoting keeps commas, quotes and new lines inside their cell.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { csvCell, csvFileName, toCsv } from "./csv.ts";

const ok = { status: "ok", fetched_at: "2026-10-25T10:00:00Z", open_url: null } as const;

test("each shape becomes rows", () => {
  assert.equal(toCsv({ ...ok, shape: "number", data: { value: 37, change: -5 } }), "value,change\r\n37,-5\r\n");
  assert.equal(
    toCsv({ ...ok, shape: "distribution", data: { items: [{ label: "High", value: 3 }] } }),
    "label,value\r\nHigh,3\r\n",
  );
  assert.equal(
    toCsv({
      ...ok,
      shape: "series",
      data: {
        series: [
          { name: "Open", points: [{ x: "2026-10-01", y: 5 }, { x: "2026-10-08", y: 7 }] },
          { name: "Closed", points: [{ x: "2026-10-08", y: 2 }] },
        ],
      },
    }),
    "x,Open,Closed\r\n2026-10-01,5,\r\n2026-10-08,7,2\r\n",
  );
  assert.equal(
    toCsv({
      ...ok,
      shape: "rows",
      data: { columns: [{ key: "name", label: "Name" }, { key: "sev", label: "" }], rows: [{ name: "VPN, edge", sev: "high" }], total: 1 },
    }),
    'Name,sev\r\n"VPN, edge",high\r\n',
  );
  assert.equal(toCsv({ ...ok, shape: "text", data: { text: 'He said "go"\nnow' } }), 'text\r\n"He said ""go""\nnow"\r\n');
});

test("no cell runs as a formula, and a number stays a number", () => {
  for (const [value, expected] of [
    ["=HYPERLINK(\"http://x\")", "\"'=HYPERLINK(\"\"http://x\"\")\""],
    ["+1+2", "'+1+2"],
    ["-2+3", "'-2+3"],
    ["@SUM(A1)", "'@SUM(A1)"],
    ["\tcmd", "'\tcmd"],
    ["\r=1", "\"'\r=1\""],
    ["-5", "-5"],
    ["-1.5e3", "-1.5e3"],
    [" padded ", '" padded "'],
    [null, ""],
  ] as const) {
    assert.equal(csvCell(value), expected, String(value));
  }
});

test("the file is named after the visualization, in safe characters", () => {
  assert.equal(csvFileName("Findings / week: Q4"), "Findings-week-Q4.csv");
  assert.equal(csvFileName("../../etc"), "..-..-etc.csv");
  assert.equal(csvFileName("   "), "data.csv");
});

test("a typed cell is written as its text", () => {
  const csv = toCsv({
    status: "ok",
    fetched_at: "2026-10-26T00:00:00Z",
    open_url: null,
    shape: "rows",
    data: {
      columns: [{ key: "labels", label: "Labels", kind: "chips" }, { key: "score", label: "Score", kind: "number" }, { key: "st", label: "Status", kind: "chip" }],
      rows: [{ labels: [{ label: "apt" }, { label: "=cmd" }], score: 9.5, st: { label: "Not detected", color: "danger" } }],
      total: 1,
    },
  });
  assert.equal(csv, 'Labels,Score,Status\r\n"apt, =cmd",9.5,Not detected\r\n');
});
