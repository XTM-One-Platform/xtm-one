/**
 * Unit tests for the visualization option readers and the data shaping the
 * renderers share - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  annotationMarks,
  bubbleRadius,
  cellItems,
  cellNumbers,
  chartRows,
  collapseSlices,
  columnTotals,
  downsample,
  gaugeShare,
  gaugeTicks,
  minMaxIndexes,
  percentTrend,
  punchcard,
  readGaugeOptions,
  readPieOptions,
  readScatterOptions,
  readSingleValueOptions,
  readTableOptions,
  readXYOptions,
  scatterPoints,
  seriesColor,
  seriesRoles,
  tablePage,
  truncateLabel,
  withValueDefaults,
  type ChartRow,
} from "./chart-options.ts";
import { frameOf } from "./dynamic-options.ts";

test("XY options: defaults, enums read against their list, numbers clamped, colours resolved", () => {
  const defaults = readXYOptions(undefined);
  assert.equal(defaults.stackMode, "auto");
  assert.equal(defaults.legendDisplay, "bottom");
  assert.equal(defaults.nullValueDisplay, "gaps");
  assert.equal(defaults.resultLimit, 1000);
  assert.equal(defaults.showBrush, false);
  const read = readXYOptions({
    stackMode: "stacked100",
    legendDisplay: "sideways",
    yAxisMin: -5,
    yAxisMax: Number.POSITIVE_INFINITY,
    lineWidth: 50,
    areaOpacity: -1,
    resultLimit: 3,
    overlayFields: "errors",
    y2Fields: ["latency", 4],
    seriesColors: ["danger", "#00ff00", "red"],
    seriesColorsByField: { ok: "success", bad: "javascript:x" },
    xAxisLabelRotation: 33,
  });
  assert.equal(read.stackMode, "stacked100");
  assert.equal(read.legendDisplay, "bottom");
  assert.equal(read.yAxisMin, -5);
  assert.equal(read.yAxisMax, undefined);
  assert.equal(read.lineWidth, 6);
  assert.equal(read.areaOpacity, 0);
  assert.equal(read.resultLimit, 10);
  assert.deepEqual(read.overlayFields, ["errors"]);
  assert.deepEqual(read.y2Fields, ["latency"]);
  assert.deepEqual(read.seriesColors, ["var(--color-feedback-error-primary)", "#00ff00", undefined]);
  assert.deepEqual(read.seriesColorsByField, { ok: "var(--color-feedback-success-primary)" });
  assert.equal(read.xAxisLabelRotation, 0);
});

test("pie, scatter, single value, gauge and table options read their defaults", () => {
  assert.deepEqual(
    { ...readPieOptions({ collapseThreshold: 0.9 }), seriesColors: undefined },
    {
      collapseThreshold: 0.5,
      collapseLabel: undefined,
      showDonutHole: true,
      labelDisplay: "off",
      legendDisplay: "right",
      legendTruncation: "ellipsisEnd",
      seriesColors: undefined,
      seriesColorsByField: {},
      areaOpacity: undefined,
    },
  );
  assert.deepEqual([readPieOptions({ labelDisplay: "percentage", areaOpacity: 0.5 }).labelDisplay, readPieOptions({ areaOpacity: 0.5 }).areaOpacity], ["percentage", 0.5]);
  const scatter = readScatterOptions({ bubbleSizeMin: 30, bubbleSizeMax: 10 });
  assert.equal(scatter.bubbleSizeMin, 30);
  assert.equal(scatter.bubbleSizeMax, 30);
  const single = readSingleValueOptions({ numberPrecision: 9.7, trendDisplay: "percent" });
  assert.equal(single.numberPrecision, 6);
  assert.equal(single.trendDisplay, "percent");
  assert.equal(single.majorValue, undefined);
  assert.deepEqual(withValueDefaults("number", { trendValue: 3 }), {
    majorValue: "> primary | seriesByName('value') | lastPoint()",
    trendValue: 3,
    value: "> primary | seriesByName('value') | lastPoint()",
  });
  assert.equal(withValueDefaults("series", {}).majorValue, "> sparklineValues | lastPoint()");
  assert.equal(single.maxValue, 100);
  const gauge = readGaugeOptions({
    gaugeRanges: [{ from: 50, to: 100, value: "danger" }, { from: 0, to: 50, value: "success" }, { from: 9, to: 1 }],
  });
  assert.deepEqual(gauge.gaugeRanges, [
    { from: 0, to: 50, color: "var(--color-feedback-success-primary)" },
    { from: 50, to: 100, color: "var(--color-feedback-error-primary)" },
  ]);
  assert.equal(gauge.minValue, 0);
  assert.equal(gauge.maxValue, 100);
  assert.deepEqual([readGaugeOptions({ minValue: 10, maxValue: 5 }).minValue, readGaugeOptions({ minValue: 10, maxValue: 5 }).maxValue], [0, 100]);
  const table = readTableOptions({ count: 500, columnFormat: { n: { width: 9999, align: "right", cellType: "sparkline" } } });
  assert.equal(table.count, 100);
  assert.deepEqual({ ...table.columnFormat.n, data: undefined }, {
    width: 800,
    align: "right",
    textOverflow: "break-word",
    cellType: "sparkline",
    data: undefined,
    rowColors: undefined,
    rowBackgroundColors: undefined,
  });
});

const SERIES = frameOf("series", {
  series: [
    { name: "a", points: [{ x: "1", y: 1 }, { x: "2", y: 3 }] },
    { name: "b", points: [{ x: "1", y: 3 }] },
  ],
});

test("chart rows draw nulls as gaps, zeros or connected, and stack to 100%", () => {
  assert.deepEqual(chartRows(SERIES, "gaps"), [
    { x: "1", s0: 1, s1: 3 },
    { x: "2", s0: 3, s1: null },
  ]);
  assert.deepEqual(chartRows(SERIES, "zero"), [
    { x: "1", s0: 1, s1: 3 },
    { x: "2", s0: 3, s1: 0 },
  ]);
  assert.deepEqual(chartRows(SERIES, "gaps", new Set([0, 1])), [
    { x: "1", s0: 25, s1: 75 },
    { x: "2", s0: 100, s1: null },
  ]);
  // A series outside the stack (an overlay) keeps its values and is not in the total.
  assert.deepEqual(chartRows(SERIES, "gaps", new Set([0])), [
    { x: "1", s0: 100, s1: 3 },
    { x: "2", s0: 100, s1: null },
  ]);
});

test("downsampling keeps the ends and the peaks, at most the limit", () => {
  const rows: ChartRow[] = Array.from({ length: 200 }, (_v, i) => ({ x: String(i), s0: i === 120 ? 1000 : i % 2 }));
  const kept = downsample(rows, 20);
  assert.equal(kept.length, 20);
  assert.equal(kept[0], rows[0]);
  assert.equal(kept[19], rows[199]);
  assert.ok(kept.includes(rows[120]));
  assert.equal(downsample(rows.slice(0, 5), 20).length, 5);
});

test("roles, colours, labels and min/max", () => {
  assert.deepEqual(seriesRoles(["count", "errors", "latency"], { overlayFields: ["errors"], y2Fields: ["latency"], showOverlayY2Axis: false }), [
    { name: "count", overlay: false, axis: "left" },
    { name: "errors", overlay: true, axis: "left" },
    { name: "latency", overlay: false, axis: "right" },
  ]);
  assert.equal(seriesRoles(["e"], { overlayFields: ["e"], y2Fields: [], showOverlayY2Axis: true })[0].axis, "right");
  const palette = (i: number) => `p${i}`;
  const colors = { seriesColors: [undefined, "#111"], seriesColorsByField: { named: "#222" } };
  assert.deepEqual([seriesColor("named", 1, colors, palette), seriesColor("x", 1, colors, palette), seriesColor("x", 0, colors, palette)], ["#222", "#111", "p0"]);
  assert.equal(seriesColor("constructor", 2, { seriesColors: [], seriesColorsByField: {} }, palette), "p2");
  assert.equal(truncateLabel("abcdefghij", 6, "ellipsisEnd"), "abcde…");
  assert.equal(truncateLabel("abcdefghij", 6, "ellipsisStart"), "…fghij");
  assert.equal(truncateLabel("abcdefghij", 6, "ellipsisMiddle"), "abc…ij");
  assert.equal(truncateLabel("abcdefghij", 6, "ellipsisOff"), "abcdefghij");
  assert.deepEqual([...minMaxIndexes([3, null, 1, 9, 1, 9])], [2, 3]);
});

test("annotations keep the marks on the chart's x", () => {
  const fields = frameOf("rows", { columns: [{ key: "t" }, { key: "what" }], rows: [{ t: "2", what: "deploy" }, { t: "9", what: "off" }] });
  assert.deepEqual(annotationMarks(fields, new Set(["1", "2"])), [{ x: "2", label: "deploy" }]);
  assert.deepEqual(annotationMarks(undefined, new Set(["1"])), []);
});

test("pie slices stay in data order and the small ones collapse", () => {
  const items = [
    { label: "b", value: 50 },
    { label: "a", value: 47 },
    { label: "c", value: 1 },
    { label: "d", value: 2 },
  ];
  assert.deepEqual(collapseSlices(items, 0.05, "Other"), [
    { label: "b", value: 50, index: 0 },
    { label: "a", value: 47, index: 1 },
    { label: "Other", value: 3, index: -1 },
  ]);
  // A single small slice stays itself.
  assert.deepEqual(collapseSlices(items.slice(0, 3), 0.05, "Other").map((s) => s.label), ["b", "a", "c"]);
  assert.deepEqual(collapseSlices(items, 0, "Other").map((s) => s.label), ["b", "a", "c", "d"]);
});

test("scatter points come from rows or series, bubbles carry a size", () => {
  const rows = frameOf("rows", {
    columns: [{ key: "host" }, { key: "cpu" }, { key: "mem" }, { key: "n" }],
    rows: [
      { host: "a", cpu: "10", mem: "20", n: "3" },
      { host: "b", cpu: "x", mem: "1", n: "1" },
      { host: "c", cpu: "30", mem: "40", n: "9" },
    ],
  });
  // "cpu" holds a text: not numeric, so x is mem, y is n.
  assert.deepEqual(scatterPoints("rows", rows, {}, false), [
    { x: 20, y: 3, category: "a" },
    { x: 1, y: 1, category: "b" },
    { x: 40, y: 9, category: "c" },
  ]);
  assert.deepEqual(scatterPoints("rows", rows, { xField: "n", yField: "mem", sizeField: "n" }, true)[2], { x: 9, y: 40, size: 9, category: "c" });
  assert.deepEqual(scatterPoints("series", SERIES, {}, false), [
    { x: 1, y: 1, category: "a" },
    { x: 2, y: 3, category: "a" },
    { x: 1, y: 3, category: "b" },
  ]);
  assert.equal(bubbleRadius(25, 100, 4, 24), 14);
  assert.equal(bubbleRadius(undefined, 100, 4, 24), 4);
});

test("a punchcard adds up repeated pairs in first-seen order", () => {
  const rows = frameOf("rows", {
    columns: [{ key: "day" }, { key: "hour" }, { key: "count" }],
    rows: [
      { day: "Mon", hour: "9", count: "2" },
      { day: "Tue", hour: "9", count: "1" },
      { day: "Mon", hour: "9", count: "3" },
    ],
  });
  // "hour" is numeric: the size is the first numeric field unless named.
  assert.deepEqual(punchcard(rows, { yField: "hour", sizeField: "count" }), {
    xs: ["Mon", "Tue"],
    ys: ["9"],
    cells: [
      { x: "Mon", y: "9", size: 5 },
      { x: "Tue", y: "9", size: 1 },
    ],
    largest: 5,
  });
  assert.deepEqual(punchcard(rows, {}), { xs: [], ys: [], cells: [], largest: 0 });
});

test("trend, gauge share and ticks", () => {
  assert.equal(percentTrend(120, 20), 20);
  assert.equal(percentTrend(5, 5), undefined);
  assert.equal(gaugeShare(150, 0, 100), 1);
  assert.equal(gaugeShare(25, 0, 100), 0.25);
  assert.equal(gaugeShare(1, 5, 5), 0);
  assert.deepEqual(gaugeTicks(0, 100, 25), [0, 25, 50, 75, 100]);
  assert.deepEqual(gaugeTicks(0, 100, 1), [0, 25, 50, 75, 100]);
  assert.deepEqual(gaugeTicks(0, 0), []);
});

test("table paging, totals and list cells", () => {
  const rows = Array.from({ length: 23 }, (_v, i) => i);
  assert.deepEqual(tablePage(rows, 2, 10), { rows: [20, 21, 22], page: 2, pages: 3 });
  assert.deepEqual(tablePage(rows, 9, 10).page, 2);
  assert.deepEqual(tablePage([], 0, 10), { rows: [], page: 0, pages: 1 });
  const fields = frameOf("rows", { columns: [{ key: "n" }, { key: "t" }, { key: "e" }], rows: [{ n: "2", t: "a", e: "" }, { n: "3.5", t: "4" }] });
  assert.deepEqual(columnTotals(fields), [5.5, null, null]);
  assert.deepEqual(cellItems("T1059, T1003; T1566\nT1078"), ["T1059", "T1003", "T1566", "T1078"]);
  assert.deepEqual(cellNumbers("1, 4 2;x;7"), [1, 4, 2, 7]);
  assert.deepEqual(cellItems(null), []);
});
