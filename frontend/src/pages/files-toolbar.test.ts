/**
 * The Files toolbar keeps one row at 1080 - `yarn test`.
 *
 * UX r2 B1 on #405 (Standards s.7, one filter row per list): the toolbar holds the
 * search, one Filters pill, the Visibility filter, the Group switch and the count.
 * Context, Origin and State live inside the Filters pill; a SegmentedFilter drawn
 * on the toolbar row itself brings the second row back at 1080.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const source = readFileSync(join(process.cwd(), "src", "pages", "Files.tsx"), "utf8");

test("Context, Origin and State sit inside the Filters pill", () => {
  const pill = source.match(/<FiltersPill\b[\s\S]*?<\/FiltersPill>/);
  assert.ok(pill, "the Files toolbar draws a FiltersPill");
  const inside = pill[0].match(/<SegmentedFilter\b/g) ?? [];
  assert.equal(inside.length, 3, "Context, Origin and State are the three filters of the pill");
  for (const name of ["PARAM_CONTEXT", "PARAM_ORIGIN", "PARAM_STATUS"]) {
    assert.ok(pill[0].includes(name), `${name} is set from inside the pill`);
  }
});

test("no SegmentedFilter is drawn on the toolbar row outside the pill", () => {
  const outside = source.replace(/<FiltersPill\b[\s\S]*?<\/FiltersPill>/g, "");
  assert.equal(/<SegmentedFilter\b/.test(outside), false);
});

test("the pill counts the active secondary filters", () => {
  assert.match(source, /<FiltersPill\b[^>]*count=\{secondaryFilterCount\}/);
  assert.match(source, /\[context, origin, status\]\.filter\(\(v\) => v !== "all"\)\.length/);
});
