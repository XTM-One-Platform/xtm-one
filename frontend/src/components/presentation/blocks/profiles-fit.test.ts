/**
 * Unit tests for the `profiles` block's measured half.
 *
 * Run with `yarn test:unit` - Node's built-in runner. The rendering is
 * exercised on a live deck by the layout audit; what is pinned here is the
 * fit contract: a line shrinks ONE step before the ellipsis cuts it, and a
 * cut line is marked so the audit and the fit agree on what "truncated" means.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  PROFILE_LINE_TEXT_CLASS,
  PROFILE_NAME_CLASS,
  PROFILE_TITLE_CLASS,
  fitProfileLines,
  profileColumns,
  profileInitials,
} from "./profiles-fit.ts";

test("a balanced grid: never more than four per row, never a lone card under a full row", () => {
  const expected: Record<number, number> = { 1: 1, 2: 2, 3: 3, 4: 4, 5: 3, 6: 3, 7: 4, 8: 4, 9: 3, 10: 4, 11: 4, 12: 4 };
  for (const [count, columns] of Object.entries(expected)) {
    assert.equal(profileColumns(Number(count)), columns, `${count} people`);
    const rows = Math.ceil(Number(count) / columns);
    const last = Number(count) - (rows - 1) * columns;
    assert.ok(columns <= 4);
    if (rows > 1) assert.ok(last >= 2, `${count} people leave ${last} on the last row`);
  }
});

test("initials are the first letters of the first and last words, markup stripped", () => {
  assert.equal(profileInitials("Samuel Hassine"), "SH");
  assert.equal(profileInitials("**Julien** Richard"), "JR");
  assert.equal(profileInitials("Jean-Pierre Dupont"), "JD");
  assert.equal(profileInitials("Plato"), "P");
  assert.equal(profileInitials(""), "");
});

/** A run whose scroll width depends on the fit step the pass applied. */
function fakeRun(widthAtBase: number, widthWhenTight: number, clientWidth: number, className = PROFILE_LINE_TEXT_CLASS) {
  const dataset: Record<string, string> = {};
  return {
    dataset,
    clientWidth,
    matches: (selector: string) => selector === `.${className}`,
    get scrollWidth() {
      return dataset.fit === "tight" ? widthWhenTight : widthAtBase;
    },
  };
}

function fakeRoot(runs: ReturnType<typeof fakeRun>[]) {
  return {
    querySelectorAll(selector: string) {
      assert.equal(selector, `.${PROFILE_LINE_TEXT_CLASS}, .${PROFILE_NAME_CLASS}, .${PROFILE_TITLE_CLASS}`);
      return runs;
    },
  } as unknown as HTMLElement;
}

test("a line that fits keeps its size and carries no attribute", () => {
  const run = fakeRun(300, 260, 400);
  fitProfileLines(fakeRoot([run]));
  assert.deepEqual(run.dataset, {});
});

test("a line wider than its row shrinks one step, and is not marked cut when that is enough", () => {
  const run = fakeRun(420, 380, 400);
  fitProfileLines(fakeRoot([run]));
  assert.deepEqual(run.dataset, { fit: "tight" });
});

test("a line still wider after the step keeps the step and is marked truncated", () => {
  const run = fakeRun(600, 540, 400);
  fitProfileLines(fakeRoot([run]));
  assert.deepEqual(run.dataset, { fit: "tight", truncated: "true" });
});

test("one type step per block: when one name needs the step, every name takes it, and the facts keep their own", () => {
  const long = fakeRun(420, 380, 400, PROFILE_NAME_CLASS);
  const short = fakeRun(200, 180, 400, PROFILE_NAME_CLASS);
  const title = fakeRun(200, 180, 400, PROFILE_TITLE_CLASS);
  const fact = fakeRun(200, 180, 400);
  fitProfileLines(fakeRoot([long, short, title, fact]));
  assert.deepEqual(long.dataset, { fit: "tight" });
  assert.deepEqual(short.dataset, { fit: "tight" });
  assert.deepEqual(title.dataset, {});
  assert.deepEqual(fact.dataset, {});
});

test("the pass starts from the base size every time, so a widened row un-shrinks", () => {
  const run = fakeRun(300, 260, 400);
  run.dataset.fit = "tight";
  run.dataset.truncated = "true";
  fitProfileLines(fakeRoot([run]));
  assert.deepEqual(run.dataset, {});
});
