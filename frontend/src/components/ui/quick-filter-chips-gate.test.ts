/**
 * QuickFilterChips is a row of toggles (multi-select) only (UX r1 B1 on #1515) - `yarn test`.
 *
 * A single-choice scope of a list is drawn with the shared `SegmentedFilter
 * selection="radio"`, as Signals draws its state scope: one pattern, one
 * component. This gate keeps the radio role out of QuickFilterChips and keeps
 * its props free of a scope.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const CHIPS = readFileSync(join(process.cwd(), "src", "components", "ui", "quick-filter-chips.tsx"), "utf8");
const CASES = readFileSync(join(process.cwd(), "src", "pages", "xtm-one", "CtemCases.tsx"), "utf8");

test("QuickFilterChips draws no single-choice scope", () => {
  assert.equal(/role="radio(group)?"/.test(CHIPS), false);
  assert.equal(/\bscopes?\s*[?:]/.test(CHIPS.slice(CHIPS.indexOf("interface QuickFilterChipsProps"))), false);
});

test("the Cases scope is the shared SegmentedFilter radio, as Signals", () => {
  const scope = CASES.indexOf('testId="ctem-cases-scope"');
  assert.ok(scope > 0);
  const open = CASES.lastIndexOf("<SegmentedFilter", scope);
  assert.ok(open > 0 && scope - open < 300);
  assert.match(CASES.slice(open, scope), /selection="radio"/);
});
