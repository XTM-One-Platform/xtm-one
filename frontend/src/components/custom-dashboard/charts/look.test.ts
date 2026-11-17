/**
 * A chart's look, checked against the modules' own numbers
 * (`dev-docs/dashboard-rendering-reference.md`) - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { isPale, themedColor } from "./palettes.ts";
import {
  crowdedAxis,
  cutLabel,
  gridColor,
  isAllEmpty,
  lookCategory,
  lookColors,
  lookNumber,
  nthColor,
  readLook,
  sampleFor,
  sampleSeries,
  seriesColorsOf,
  timeTicks,
  valueExtras,
  weeklyDates,
} from "./look.ts";

const compact = (value: unknown) => `c${String(value)}`;

test("an empty look keeps XTM One's own: nothing named, solid fills, category axes", () => {
  const look = readLook({});
  assert.equal(look.palette, undefined);
  assert.equal(look.numberFormat, undefined);
  assert.deepEqual([look.fill, look.tooltipContent, look.xAxisType, look.colorByPoint, look.openLinks, look.labelCut], ["solid", "labelValue", "category", false, false, "chars"]);
  assert.equal(lookColors(look, "dark"), undefined);
  assert.equal(lookNumber(look, 1500, compact), "c1500");
  assert.equal(lookCategory(look, "2026-01-05"), "2026-01-05");
});

test("a look is read defensively: enums against their lists, numbers clamped, colours resolved", () => {
  const look = readLook({
    palette: "rainbow", paletteLead: "url(javascript:x)", separatorWidth: 40, barSize: 2, fillOpacityFrom: 3, radius: 9999,
    numberFormat: "si", tooltipContent: "label", emptySample: "nope", axisLabelColor: "muted", markerSize: "4",
  }); // prettier-ignore
  assert.deepEqual(
    [look.palette, look.paletteLead, look.separatorWidth, look.barSize, look.fillOpacityFrom, look.radius, look.numberFormat, look.tooltipContent, look.emptySample, look.axisLabelColor, look.markerSize],
    [undefined, undefined, 6, 10, 1, 400, "si", "label", undefined, "var(--text-default-secondary)", undefined],
  );
});

test("the modules' palettes: the accent then Material 400 in OpenCTI's order, OpenAEV's results, by theme", () => {
  const cti = readLook({ palette: "materialMixed", paletteLead: "accent" });
  assert.deepEqual(lookColors(cti, "dark")?.slice(0, 4), ["var(--color-accent)", "#ef5350", "#ab47bc", "#ec407a"]);
  assert.deepEqual(lookColors(cti, "light")?.slice(0, 4), ["var(--color-accent)", "#e53935", "#8e24aa", "#d81b60"]);
  assert.deepEqual(lookColors({ palette: "results", paletteNoPale: false }, "dark"), ["#7e57c2", "#5c6bc0", "#29b6f6"]);
  // OpenAEV's horizontal bars: the accent, then the 19 hues at 400, then at 500.
  const aev = lookColors(readLook({ palette: "material", paletteLead: "accent" }), "dark") ?? [];
  assert.deepEqual([aev.length, aev[1], aev[19], aev[20]], [39, "#ef5350", "#78909c", "#f44336"]);
  assert.deepEqual([nthColor(["a", "b"], 3), nthColor([], 0), nthColor(undefined, 0)], ["b", undefined, undefined]);
});

test("a treemap of distinct colours leaves out those too close to white (brightness 0.9)", () => {
  assert.deepEqual([isPale("#ffffff"), isPale("#ffee58"), isPale("#fff59d"), isPale("#bdbdbd"), isPale("var(--x)")], [true, false, true, false, false]);
  const colors = lookColors({ palette: "materialMixed", paletteNoPale: true }, "dark") ?? [];
  assert.equal(colors.length, 19);
});

test("a data colour follows the theme: Material 400 takes its 600 twin and white turns black in the light theme", () => {
  assert.deepEqual([themedColor("#66bb6a", "light"), themedColor("#EF5350", "light"), themedColor("#ffffff", "light"), themedColor("#fff", "light")], ["#43a047", "#e53935", "#000000", "#000000"]);
  assert.deepEqual([themedColor("#75829a", "light"), themedColor("#00f0bc", "light"), themedColor("#BA88FF", "light"), themedColor("success", "light")], ["#6e7788", "#009474", "#BA88FF", "success"]);
  assert.equal(themedColor("#ffffff", "dark"), "#ffffff");
});

test("numbers and dates as the modules write them", () => {
  assert.deepEqual(
    [lookNumber({ numberFormat: "siSpaced" }, 1520, compact), lookNumber({ numberFormat: "si" }, 1520, compact, "en"), lookNumber({ numberFormat: "locale" }, 1532, compact, "en-US")],
    ["1.52 K", "1.52K", "1,532"],
  );
  assert.equal(lookNumber({ numberFormat: "siSpaced" }, "x", compact), "-");
  // OpenCTI's axes by interval, in UTC; OpenAEV's long dates.
  assert.deepEqual(
    [
      lookCategory({ dateFormat: "date" }, "2026-01-05", "en-US"),
      lookCategory({ dateFormat: "monthYear" }, "2026-01-01T00:00:00Z", "en-US"),
      lookCategory({ dateFormat: "year" }, "2026-01-01", "en-US"),
      lookCategory({ dateFormat: "longDate" }, "2026-09-07", "en-US"),
      lookCategory({ dateFormat: "date" }, "Malware", "en-US"),
    ],
    ["Jan 5, 2026", "January 2026", "2026", "September 7, 2026", "Malware"],
  );
});

test("labels cut as each module cuts them: on a character with an ellipsis, or on a word with three dots", () => {
  assert.equal(cutLabel("T1059 Command and Scripting Interpreter", 28, "chars"), "T1059 Command and Scripting…");
  assert.equal(cutLabel("Vulnerability-With-A-Very-Long-Name", 25, "words"), "Vulnerability-With-A-Very...");
  assert.equal(cutLabel("Exploit Public-Facing Application Today", 25, "words"), "Exploit Public-Facing...");
  assert.equal(cutLabel("short", 25, "words"), "short");
  assert.equal(cutLabel("anything", undefined), "anything");
});

test("a time axis keeps at most so many ticks, evenly picked, first and last kept", () => {
  const days = Array.from({ length: 14 }, (_, i) => Date.UTC(2026, 9, i + 1));
  const ticks = timeTicks(days, 5);
  assert.deepEqual([ticks.length, ticks[0], ticks[ticks.length - 1]], [5, days[0], days[13]]);
  assert.deepEqual(timeTicks([3, 1, 2, 2], 5), [1, 2, 3]);
});

test("the grid in the text colour at an opacity, or the theme's border", () => {
  assert.deepEqual([gridColor(0.1, "dark"), gridColor(0.08, "light"), gridColor(undefined, "dark")], ["rgba(255, 255, 255, 0.1)", "rgba(0, 0, 0, 0.08)", "var(--border-elevation-subtle)"]);
});

test("what values carry besides their values: colours for the theme, links, keys, by the frame's order", () => {
  const items = { items: [{ label: "a", value: 1, color: "#66bb6a", link: "https://x.test/1", key: "k1" }, { label: "b", value: 2, color: "url(x)" }] };
  assert.deepEqual(valueExtras("distribution", items, "light"), [
    { color: "#43a047", link: "https://x.test/1", key: "k1", entityType: undefined },
    { color: undefined, link: undefined, key: undefined, entityType: undefined },
  ]);
  const series = { series: [{ name: "s", color: "success", points: [{ x: "2026-02", y: 1, key: "B" }, { x: "2026-01", y: 2, key: "A" }] }, { name: "t", color: "nope", points: [] }] };
  assert.deepEqual(valueExtras("series", series, "dark").map((e) => e.key), ["A", "B"]);
  assert.deepEqual(seriesColorsOf(series, "dark"), ["var(--color-feedback-success-primary)", undefined]);
});

test("a widget with nothing to show (no value, or only zeros) draws its sample, as OpenAEV does", () => {
  assert.deepEqual(
    [isAllEmpty("series", { series: [] }), isAllEmpty("series", { series: [{ points: [{ x: "a", y: 0 }] }] }), isAllEmpty("series", { series: [{ points: [{ x: "a", y: 1 }] }] }), isAllEmpty("distribution", { items: [] }), isAllEmpty("number", {})],
    [true, true, false, true, false],
  );
  assert.deepEqual(sampleSeries("status", []), [
    { name: "", points: [{ x: "SUCCESS", y: 64, color: "success" }, { x: "FAILED", y: 27, color: "#f14337" }, { x: "PENDING", y: 9, color: "#9e9e9e" }] },
  ]);
  const temporal = sampleSeries("temporal", ["Findings", "Injects"], new Date("2026-10-26T12:00:00Z"));
  assert.deepEqual(temporal.map((s) => [s.name, s.points.map((p) => p.y)]), [["Findings", [4, 7, 5, 9, 12, 8, 14, 11]], ["Injects", [2, 5, 3, 7, 9, 6, 10, 7]]]);
  assert.deepEqual(weeklyDates(new Date("2026-10-26T12:00:00Z")).slice(-2), ["2026-10-19", "2026-10-26"]);
  assert.deepEqual(sampleSeries("platforms", []).map((s) => s.name), ["Not Detected", "Not Prevented"]);
  assert.deepEqual(sampleSeries("ttp", [])[0].points.map((p) => p.y), [32, 26, 21, 17, 12]);
  // The radar's series named as OpenAEV's legend writes them.
  assert.deepEqual(sampleSeries("radar", []).map((s) => [s.name, s.points.length]), [["Success", 6], ["Failed", 6]]);
  // A distribution keeps its shape for a sample of one series; a sample of two stays two series, as OpenAEV
  // draws it (Detected and Prevented TTPs side by side, with their legend); a radar is greyed lighter.
  assert.deepEqual(sampleFor("status", "distribution", {}).shape, "distribution");
  assert.deepEqual(sampleFor("status", "distribution", {}).data, { items: [["SUCCESS", 64, "success"], ["FAILED", 27, "#f14337"], ["PENDING", 9, "#9e9e9e"]].map(([label, value, color]) => ({ label, value, color })) });
  const ttp = sampleFor("ttp", "distribution", {});
  assert.deepEqual([ttp.shape, (ttp.data as { series: { name: string }[] }).series.map((s) => s.name)], ["series", ["Detected TTPs", "Prevented TTPs"]]);
  assert.deepEqual(ttp.options, { legendDisplay: "bottom", dataValuesDisplay: "off" });
  // A widget of two series drawing a sample of two keeps its own options.
  assert.deepEqual(sampleFor("temporal", "series", { series: [{ name: "a" }, { name: "b" }] }).options, {});
  assert.deepEqual([sampleFor("radar", "series", {}).variant, sampleFor("status", "distribution", {}).variant], ["subtle", "full"]);
});

test("crowded category labels are all written at -45° once the widest no longer fits its category's room", () => {
  const weeks = ["Jul 19, 2026", "Jul 26, 2026", "Aug 2, 2026", "Aug 9, 2026"];
  // 12 characters at 12 px: 86.4 px wide, against 100 px a category: level.
  assert.deepEqual(crowdedAxis(weeks, 400, 12), { rotate: false, height: 24 });
  // 15 weeks in 560 px (37 px each): tilted, the axis as tall as the widest label's diagonal.
  assert.deepEqual(crowdedAxis([...weeks, ...weeks, ...weeks, "Oct 25, 2026", "Oct 18, 2026", "Oct 11, 2026"], 560, 12), { rotate: true, height: 82 });
});
