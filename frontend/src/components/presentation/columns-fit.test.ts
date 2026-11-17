/**
 * Unit tests for when a `columns` block keeps its columns side by side
 * - `yarn test:unit`.
 *
 * The DOM half (`fitColumns`, `useColumnsFit`) is exercised on a live deck by
 * `tests_e2e/presentation-logo-layout.spec.ts`; what is pinned here is the
 * rule both halves rely on: four columns stay four while the widest column's
 * content fits one equal track, only four ever wrap, and they wrap 2 x 2.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  COLUMN_FIT_TOLERANCE_PX,
  WRAPPED_PER_ROW,
  WRAPPING_COLUMN_COUNT,
  columnRowCount,
  columnTrack,
  columnsPerRow,
} from "./columns-fit-math.ts";

test("columns lined up as rows take one row per block of their longest column", () => {
  assert.equal(columnRowCount([[1, 2], [1, 2]]), 2);
  assert.equal(columnRowCount([[1], [1, 2, 3]]), 3);
  assert.equal(columnRowCount([]), 1);
});

test("a track is the block's width less its gaps, shared equally", () => {
  assert.equal(columnTrack(1728, 4, 48), 396);
  assert.equal(columnTrack(1728, 2, 48), 840);
  assert.equal(columnTrack(1000, 1, 48), 1000);
});

test("four columns whose widest content fits a track stay four", () => {
  // A 16:9 body, four columns of one small logo grid each: a few hundred px.
  assert.equal(columnsPerRow(4, 180, 1728, 48), 4);
  assert.equal(columnsPerRow(4, 396, 1728, 48), 4, "exactly a track");
  assert.equal(columnsPerRow(4, 396 + COLUMN_FIT_TOLERANCE_PX, 1728, 48), 4, "a pixel of rounding");
});

test("four columns whose widest content is wider than a track wrap onto rows of two", () => {
  assert.equal(WRAPPING_COLUMN_COUNT, 4);
  assert.equal(columnsPerRow(4, 520, 1728, 48), WRAPPED_PER_ROW);
  assert.equal(WRAPPED_PER_ROW, 2);
});

test("two and three columns never wrap, and a block that measures nothing keeps its columns", () => {
  assert.equal(columnsPerRow(3, 5000, 1728, 48), 3);
  assert.equal(columnsPerRow(2, 5000, 1728, 48), 2);
  // Reveal keeps far-away slides at display: none: everything measures zero.
  assert.equal(columnsPerRow(4, 0, 0, 48), 4);
});
