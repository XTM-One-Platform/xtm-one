/**
 * The engine's palettes and scales, checked against the modules' own
 * (`dev-docs/dashboard-rendering-reference.md`) - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { heatColor, heatRanges, paletteColors, rateColor, threatColor } from "./palettes.ts";

test("the Material palettes: 400 in the dark theme, 600 in the light one", () => {
  const dark = paletteColors("material", "dark");
  assert.equal(dark.length, 38);
  assert.deepEqual(dark.slice(0, 5), ["#ef5350", "#ec407a", "#ab47bc", "#7e57c2", "#5c6bc0"]);
  assert.equal(dark[18], "#78909c");
  // Then the same hues a tint deeper.
  assert.deepEqual(dark.slice(19, 21), ["#f44336", "#e91e63"]);
  assert.deepEqual(paletteColors("material", "light").slice(0, 3), ["#e53935", "#d81b60", "#8e24aa"]);
  assert.deepEqual(paletteColors("material", "light")[19], "#d32f2f");
  const mixed = paletteColors("materialMixed", "dark");
  assert.deepEqual(mixed, [
    "#ef5350", "#ab47bc", "#ec407a", "#7e57c2", "#5c6bc0", "#42a5f5", "#26c6da", "#78909c", "#29b6f6", "#66bb6a",
    "#26a69a", "#9ccc65", "#ffca28", "#ff7043", "#d4e157", "#ffee58", "#8d6e63", "#ffa726", "#bdbdbd",
  ]); // prettier-ignore
  assert.equal(paletteColors("materialMixed", "light")[1], "#8e24aa");
  assert.deepEqual(paletteColors("results", "dark"), ["#7e57c2", "#5c6bc0", "#29b6f6"]);
  assert.deepEqual(paletteColors("results", "light"), ["#5e35b1", "#3949ab", "#039be5"]);
});

test("threat levels: an unranked region is drawn as level 5", () => {
  assert.equal(paletteColors("threat", "light").length, 10);
  assert.equal(threatColor(1), "#ffe082");
  assert.equal(threatColor(9), "#b71c1c");
  for (const unranked of [0, null, undefined, 12, Number.NaN]) assert.equal(threatColor(unranked), "#d95f00");
});

test("heat ranges: zero apart, then ten whole steps from the lowest value", () => {
  const palette = paletteColors("heat", "dark");
  const ranges = heatRanges(0, 90, palette);
  assert.equal(ranges.length, 11);
  assert.deepEqual(ranges[0], { from: 1, to: 10, color: "#023362" });
  assert.deepEqual(ranges[9], { from: 90, to: 100, color: "#9eccff" });
  assert.deepEqual(ranges[10], { from: 0, to: 0, color: "#001e3c" });
  assert.equal(heatColor(0, ranges), "#001e3c");
  assert.equal(heatColor(5, ranges), "#023362");
  // On a boundary, the higher step.
  assert.equal(heatColor(10, ranges), "#02407a");
  // Values spanning fewer than 9 still get steps of 1.
  const narrow = heatRanges(2, 6, palette);
  assert.deepEqual(narrow.slice(0, 2), [{ from: 2, to: 3, color: "#023362" }, { from: 3, to: 4, color: "#02407a" }]);
  assert.equal(heatColor(6, narrow), "#0b75d9");
  assert.equal(heatColor(-1, narrow), undefined);
  assert.equal(paletteColors("heat", "light")[0], "#f3f6f9");
});

test("rate scales", () => {
  // Coverage: a bound belongs to the band below it.
  assert.deepEqual([0, 0.25, 0.26, 0.5, 0.75, 0.76, 1].map((r) => rateColor(r, "coverage", "dark")), [
    "#f44336", "#f44336", "#ff7043", "#ff7043", "#ffb300", "#26a96c", "#26a96c",
  ]); // prettier-ignore
  for (const none of [null, undefined, -1, Number.NaN]) assert.equal(rateColor(none, "coverage", "dark"), "#5c6b7a");
  assert.equal(rateColor(0.6, "average", "light"), "rgb(255, 216, 0)");
  assert.equal(rateColor(-1, "average", "dark"), "rgba(128, 127, 127, 0.37)");
  // A gauge: a bound belongs to the band above it.
  assert.deepEqual([0.24, 0.25, 0.5, 0.74, 0.75].map((r) => rateColor(r, "gauge", "dark")), [
    "var(--color-feedback-error-primary)", "#ff7043", "#ffa726", "#ffa726", "var(--color-feedback-success-primary)",
  ]); // prettier-ignore
  assert.equal(rateColor(0.6, "gauge", "light"), "#ed6c02");
  assert.equal(rateColor(null, "gauge", "light"), "#6e7788");
});
