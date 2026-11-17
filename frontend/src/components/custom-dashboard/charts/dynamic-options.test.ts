/**
 * Unit tests for the dynamic options subset - `yarn test:unit`.
 *
 * An expression is parsed by a closed grammar (no `eval`), evaluated on the
 * closed data shapes, and anything it cannot read gives `undefined`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { CHART_PALETTE, mixColor, paletteColor, resolveColor } from "./colors.ts";
import {
  evaluate,
  formatNumber,
  frameOf,
  optionColor,
  optionColors,
  optionValue,
  optionValues,
  parseExpression,
  type DosScope,
} from "./dynamic-options.ts";

const SERIES = {
  series: [
    { name: "count", points: [{ x: "2026-01-02", y: 5 }, { x: "2026-01-01", y: 2 }, { x: "2026-01-03", y: 9 }] },
    { name: "other", points: [{ x: "2026-01-01", y: 1 }] },
  ],
};

const scope = (extra: Partial<DosScope> = {}): DosScope => ({ primary: frameOf("series", SERIES), ...extra });

test("parses a pipeline of known functions with typed arguments", () => {
  const parsed = parseExpression("> primary | seriesByName('count') | lastPoint() | rangeValue(ranges)");
  assert.deepEqual(parsed, {
    ok: true,
    expression: {
      source: "primary",
      calls: [
        { name: "seriesByName", args: [{ kind: "string", value: "count" }] },
        { name: "lastPoint", args: [] },
        { name: "rangeValue", args: [{ kind: "ref", name: "ranges" }] },
      ],
    },
  });
});

test("refuses what the grammar does not hold", () => {
  for (const [text, error] of [
    ["primary | lastPoint()", "an expression starts with '>'"],
    ["> primary | eval('1+1')", "unknown function eval"],
    ["> primary | constructor()", "unknown function constructor"],
    ["> primary | seriesByName(1)", "seriesByName() takes (string)"],
    ["> primary | seriesByIndex('a')", "seriesByIndex() takes (number)"],
    ["> primary | lastPoint(", "lastPoint() needs ')'"],
    ["> primary lastPoint()", "steps are separated by '|'"],
    ["> primary | rangeValue(a, b)", "rangeValue() takes (ref)"],
    ["> primary; alert(1)", "unexpected text at 8"],
    [`> primary${" | min()".repeat(11)}`, "an expression has at most 10 steps"],
    [`> primary | prefix('${"a".repeat(600)}')`, "an expression holds at most 500 characters"],
  ] as const) {
    assert.deepEqual(parseExpression(text), { ok: false, error }, text);
  }
});

test("reads every closed shape as fields", () => {
  assert.deepEqual(frameOf("series", SERIES), [
    { name: "x", values: ["2026-01-01", "2026-01-02", "2026-01-03"] },
    { name: "count", values: [2, 5, 9] },
    { name: "other", values: [1, null, null] },
  ]);
  // Categories in the order the data gives them (a radar's axes); dates in time, wherever a series has them.
  assert.deepEqual(frameOf("series", { series: [{ name: "a", points: [{ x: "EDR", y: 1 }, { x: "SIEM", y: 2 }, { x: "Cloud", y: 3 }] }] })[0].values, ["EDR", "SIEM", "Cloud"]);
  const dated = { series: [{ name: "a", points: [{ x: "2026-10-08", y: 1 }] }, { name: "b", points: [{ x: "2026-10-01", y: 2 }, { x: "2026-10-08", y: 3 }] }] };
  assert.deepEqual(frameOf("series", dated)[0].values, ["2026-10-01", "2026-10-08"]);
  assert.deepEqual(frameOf("distribution", { items: [{ label: "a", value: 3 }] }), [
    { name: "label", values: ["a"] },
    { name: "value", values: [3] },
  ]);
  assert.deepEqual(frameOf("number", { value: 7, change: -2 }), [
    { name: "value", values: [7] },
    { name: "change", values: [-2] },
  ]);
  // A number cell (a `number` column) stays a number, a chip is its label, chips their labels joined.
  assert.deepEqual(
    frameOf("rows", {
      columns: [{ key: "n", label: "N" }],
      rows: [{ n: "4" }, { n: 4 }, { n: { label: "High", color: "danger" } }, { n: [{ label: "a" }, { label: "b" }] }, { n: true }],
    }),
    [{ name: "n", values: ["4", 4, "High", "a, b", null] }],
  );
  assert.deepEqual(frameOf("map", {}), []);
});

test("selectors pick series and points", () => {
  assert.deepEqual(evaluate("> primary | seriesByIndex(1) | firstPoint()", scope()), { kind: "point", value: 2 });
  assert.deepEqual(evaluate("> primary | seriesByName('count') | lastPoint()", scope()), { kind: "point", value: 9 });
  assert.deepEqual(evaluate("> primary | seriesByName('count') | delta(-2)", scope()), { kind: "point", value: 4 });
  assert.deepEqual(evaluate("> primary | seriesByName('count') | delta(0)", scope()), { kind: "point", value: 7 });
  assert.deepEqual(evaluate("> primary | seriesByName('count') | min()", scope()), { kind: "point", value: 2 });
  assert.deepEqual(evaluate("> primary | seriesByName('count') | max()", scope()), { kind: "point", value: 9 });
  assert.deepEqual(evaluate("> primary | seriesByName('count') | sum()", scope()), { kind: "point", value: 16 });
  assert.equal(evaluate("> primary | seriesByName('missing') | lastPoint()", scope()), undefined);
  assert.equal(evaluate("> primary | lastPoint()", scope()), undefined);
  assert.equal(evaluate("> primary | seriesByName('count') | delta(-9)", scope()), undefined);
  assert.equal(evaluate("> nowhere | lastPoint()", scope()), undefined);
});

test("a source may name another option, a bounded number of times", () => {
  const options = {
    sparklineValues: "> primary | seriesByIndex(1)",
    majorValue: "> sparklineValues | lastPoint()",
    loopA: "> loopB",
    loopB: "> loopA",
    literal: [1, "2", true, 3],
  };
  assert.equal(optionValue(options.majorValue, scope({ options })), 9);
  assert.deepEqual(optionValues("> sparklineValues", scope({ options })), [2, 5, 9]);
  assert.equal(optionValue("> loopA", scope({ options })), undefined);
  assert.deepEqual(optionValues("> literal", scope({ options })), [1, "2", null, 3]);
  assert.equal(optionValue(42, scope()), 42);
  assert.equal(optionValue({ not: "a value" }, scope()), undefined);
});

test("rangeValue, matchValue and gradient colour a point or every point", () => {
  const context = {
    ranges: [{ to: 3, value: "success" }, { from: 3, to: 8, value: "warning" }, { from: 8, value: "danger" }],
    matches: [{ match: "a", value: "info" }, { match: 5, value: "#ff0000" }],
    gradient: { stops: [0, 10], colors: ["success", "danger"] },
  };
  const s = scope({ context });
  assert.deepEqual(optionValues("> primary | seriesByName('count') | rangeValue(ranges)", s), ["success", "warning", "danger"]);
  assert.equal(optionColor("> primary | seriesByName('count') | lastPoint() | rangeValue(ranges)", s), "var(--color-feedback-error-primary)");
  assert.deepEqual(optionColors("> primary | seriesByName('count') | matchValue(matches)", s), [undefined, "#ff0000", undefined]);
  assert.deepEqual(optionColors("> primary | seriesByName('count') | gradient(gradient)", s), [
    "color-mix(in srgb, var(--color-feedback-error-primary) 20%, var(--color-feedback-success-primary))",
    "color-mix(in srgb, var(--color-feedback-error-primary) 50%, var(--color-feedback-success-primary))",
    "color-mix(in srgb, var(--color-feedback-error-primary) 90%, var(--color-feedback-success-primary))",
  ]);
  // A config the context does not hold colours nothing.
  assert.deepEqual(optionValues("> primary | seriesByName('count') | rangeValue(nothing)", s), [null, null, null]);
});

test("formatByType and prefix format values", () => {
  const context = { fmt: { number: { precision: 1, unit: "%", thousandSeparated: true } } };
  assert.deepEqual(optionValues("> primary | seriesByName('count') | formatByType(fmt) | prefix('~')", scope({ context })), [
    "~2.0%",
    "~5.0%",
    "~9.0%",
  ]);
  assert.deepEqual(optionValues("> primary | seriesByName('other') | prefix('$')", scope()), ["$1", null, null]);
});

test("formatNumber rounds, groups, abbreviates and places the unit", () => {
  assert.equal(formatNumber(1234567.891, { precision: 2 }), "1,234,567.89");
  assert.equal(formatNumber(1234567, { thousandSeparated: false }), "1234567");
  assert.equal(formatNumber(1234567, { abbreviate: true, precision: 1 }), "1.2M");
  assert.equal(formatNumber(1234, { abbreviate: true }), "1.2K");
  assert.equal(formatNumber(1000, { abbreviate: true }), "1K");
  assert.equal(formatNumber(100000, { abbreviate: true }), "100K");
  assert.equal(formatNumber(1500000, { abbreviate: true, precision: 3 }), "1.5M");
  assert.equal(formatNumber(999, { abbreviate: true }), "999");
  assert.equal(formatNumber(-2500, { abbreviate: true, precision: 1, unit: "$", unitPosition: "before" }), "$-2.5K");
  assert.equal(formatNumber(42, { unit: "ms" }), "42 ms");
  assert.equal(formatNumber(73, { unit: "%" }), "73%");
  assert.equal(formatNumber(-0.004, { precision: 2 }), "0.00");
  assert.equal(formatNumber(3, { precision: 99 }), "3.000000");
  assert.equal(formatNumber(Number.NaN), "");
});

test("colours are tokens, strict hex or a mix of both, nothing else", () => {
  assert.equal(resolveColor("danger"), "var(--color-feedback-error-primary)");
  assert.equal(resolveColor("chart3"), CHART_PALETTE[2]);
  assert.equal(resolveColor("#AbC"), "#AbC");
  assert.equal(resolveColor("#abcdef"), "#abcdef");
  for (const hostile of ["red", "#abcd", "url(x)", "var(--x)", "#fff;background:url(x)", "toString", "__proto__", 3]) {
    assert.equal(resolveColor(hostile), undefined, String(hostile));
  }
  assert.equal(resolveColor(mixColor("#000", "var(--color-accent)", 0.25)), "color-mix(in srgb, var(--color-accent) 25%, #000)");
  assert.equal(resolveColor("color-mix(in srgb, url(x) 25%, #000)"), undefined);
  assert.equal(mixColor("#000", "#fff", 0), "#000");
  assert.equal(mixColor("#000", "#fff", 1), "#fff");
  assert.equal(paletteColor(CHART_PALETTE.length + 1), CHART_PALETTE[1]);
  assert.equal(new Set(CHART_PALETTE).size, CHART_PALETTE.length);
});
