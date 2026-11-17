/**
 * The engine's formats, checked against the modules' own
 * (`dev-docs/dashboard-rendering-reference.md`) - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { axisDateOptions, countUpValue, formatAxisDate, formatDuration, formatNamedDate, formatNamedNumber, formatSi, siParts } from "./formats.ts";

test("a figure is scaled to its SI symbol, trailing zeros dropped", () => {
  assert.deepEqual(siParts(1500), { number: 1.5, symbol: "K" });
  assert.deepEqual(siParts(42), { number: 42, symbol: "" });
  assert.deepEqual(siParts(1234567, 2), { number: 1.23, symbol: "M" });
  assert.deepEqual(siParts(2e9), { number: 2, symbol: "G" });
  // A negative figure is not scaled unless it is signed.
  assert.deepEqual(siParts(-1500), { number: -1500, symbol: "" });
});

test("the named number formats", () => {
  assert.equal(formatSi(1500, { space: true }), "1.5 K");
  assert.equal(formatSi(42, { space: true }), "42");
  assert.equal(formatNamedNumber(1500, "si", "en"), "1.5K");
  assert.equal(formatNamedNumber(1500, "si", "fr"), "1,5K");
  assert.equal(formatNamedNumber(1500, "siSpaced"), "1.5 K");
  assert.equal(formatNamedNumber(70926, "compact"), "70.9K");
  assert.equal(formatNamedNumber(-1500000, "compact"), "-1.5M");
  assert.equal(formatNamedNumber(999, "compact"), "999");
  assert.equal(formatNamedNumber(1500, "locale", "en"), "1,500");
  for (const empty of [null, undefined, Number.NaN]) assert.equal(formatNamedNumber(empty, "si"), "-");
});

test("the named date formats and a time axis's ticks", () => {
  const at = "2026-10-01T10:05:09Z";
  assert.equal(formatNamedDate(at, "date", "en-GB", "UTC"), "1 Oct 2026");
  assert.equal(formatNamedDate(at, "longDate", "en-GB", "UTC"), "1 October 2026");
  assert.equal(formatNamedDate(at, "longDateTime", "en-GB", "UTC"), "1 October 2026 at 10:05:09");
  assert.equal(formatNamedDate(at, "dateTime", "en-GB", "UTC"), "1 Oct 2026, 10:05:09");
  assert.equal(formatNamedDate("not a date", "date"), "-");
  assert.equal(formatNamedDate(null, "date"), "-");
  assert.equal(formatAxisDate("2026-01-31T23:00:00Z", "month", "en-GB"), "January 2026");
  assert.equal(formatAxisDate("2026-01-31T23:00:00Z", "quarter", "en-GB"), "January 2026");
  assert.equal(formatAxisDate("2026-01-31T23:00:00Z", "year", "en-GB"), "2026");
  assert.equal(formatAxisDate("2026-01-31T23:00:00Z", "week", "en-GB"), "31 Jan 2026");
  assert.equal(formatAxisDate("2026-01-31T23:00:00Z", undefined, "en-GB"), "31 Jan 2026");
  assert.equal(axisDateOptions("day").timeZone, "UTC");
});

test("a duration is written in its largest two units", () => {
  assert.equal(formatDuration(45_400), "45s");
  assert.equal(formatDuration(180_000), "3min");
  assert.equal(formatDuration(200_000), "3min 20s");
  assert.equal(formatDuration(7_200_000), "2h");
  assert.equal(formatDuration(8_100_000), "2h 15min");
  assert.equal(formatDuration(-5), "0s");
});

test("a figure counts up in 1.2 s, easing out", () => {
  assert.equal(countUpValue(0, 100, 0), 0);
  assert.equal(countUpValue(0, 100, 600), 87.5);
  assert.equal(countUpValue(0, 100, 1200), 100);
  assert.equal(countUpValue(40, 10, 5000), 10);
  assert.equal(countUpValue(0, 100, 10, 0), 100);
});
