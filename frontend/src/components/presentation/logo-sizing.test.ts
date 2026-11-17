/**
 * Unit tests for the logo sizing arithmetic - `yarn test:unit`.
 *
 * Pinned here: a logo is sized by its trimmed mark, `logo_height` is the
 * height of a wide mark and the floor of every balanced one, a squarer or
 * lighter mark grows up to the boost and no further, and the rows of a wall
 * are cut exactly as the spec says (columns / rows / per_row, else balanced).
 * A grid with `columns` shares its width in equal cells unless it says
 * otherwise, and never wraps N logos under `columns: N`. In a cell of
 * fixed height (`tile_height`) the balance is not capped - the cell
 * is the bound - and `start` under `columns` keeps the row's equal cells.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  LOGO_BALANCE_MAX_BOOST,
  LOGO_HEIGHT_PX,
  balancedRows,
  currentLoad,
  fullRowLength,
  logoGridAlign,
  logoGridEqualCells,
  logoHeightPx,
  logoImageShown,
  logoInkAlphaRow,
  logoInkRange,
  logoMaxBoost,
  logoPending,
  logoRows,
  logoScale,
  logoShape,
  logoWeightHeightPx,
  LOGO_WEIGHT_MIN_FACTOR,
  logoWallAutoMaxPerRow,
  logoWallColumns,
  shortRows,
  trimBox,
} from "./logo-sizing.ts";

test("the four logo heights are the canvas pixels the spec documents", () => {
  assert.deepEqual(LOGO_HEIGHT_PX, { sm: 28, md: 40, lg: 56, xl: 72 });
});

test("the shape is the TRIMMED mark's, so a padded artboard does not shrink the logo", () => {
  // A 400 x 200 file whose mark is the middle half by a quarter: a 4:1 wordmark.
  const shape = logoShape({ w: 400, h: 200, trim: [0.25, 0.375, 0.75, 0.625], ink: 0.9 });
  assert.ok(shape);
  assert.equal(shape.ratio, 4);
  assert.equal(shape.ink, 0.9);
  // Untrimmed, the same file would read as a 2:1 box.
  assert.equal(logoShape(undefined, { width: 400, height: 200 })?.ratio, 2);
  // Nothing known yet: no shape (the box waits for the image).
  assert.equal(logoShape(undefined, undefined), null);
});

test("a bad trim box reads as the whole file", () => {
  assert.deepEqual(trimBox({ w: 10, h: 10, trim: [0.5, 0.5, 0.5, 0.9] }), [0, 0, 1, 1]);
  assert.deepEqual(trimBox(undefined), [0, 0, 1, 1]);
  assert.deepEqual(trimBox({ w: 10, h: 10, trim: [-1, 0, 2, 1] }), [0, 0, 1, 1]);
});

test("optical weight draws the reference wordmark at the target height", () => {
  assert.equal(logoWeightHeightPx({ ratio: 3, ink: 0.5 }, 40), 40);
  // No ink known: read as the reference ink.
  assert.equal(logoWeightHeightPx({ ratio: 3 }, 40), 40);
});

test("optical weight shrinks a long or heavy wordmark, unlike the balance", () => {
  const long = logoWeightHeightPx({ ratio: 6, ink: 0.5 }, 40);
  assert.ok(long < 40, `a 6:1 wordmark is drawn under 40 px, got ${long}`);
  assert.equal(long, Math.round(40 * Math.sqrt(0.5) * 10) / 10);
  // Never under the floor, however long.
  assert.equal(logoWeightHeightPx({ ratio: 40, ink: 1 }, 40), 40 * LOGO_WEIGHT_MIN_FACTOR);
});

test("optical weight gives a wordmark, a square badge, a roundel and a shield one inked area", () => {
  const marks = [
    { ratio: 3.2, ink: 0.45 }, // wordmark
    { ratio: 1, ink: 0.8 }, // square badge, white text inside
    { ratio: 1, ink: 0.6 }, // round badge with a check
    { ratio: 0.85, ink: 0.7 }, // shield
  ];
  // Unbounded tile: no ceiling but the weight's own.
  const areas = marks.map((m) => {
    const h = logoWeightHeightPx(m, 40, Infinity);
    return h * h * m.ratio * m.ink;
  });
  const reference = 40 * 40 * 3 * 0.5;
  for (const area of areas) {
    assert.ok(Math.abs(area / reference - 1) < 0.02, `inked area ${area} vs ${reference}`);
  }
});

test("optical weight respects a hugging cell's boost", () => {
  assert.equal(logoWeightHeightPx({ ratio: 0.5, ink: 0.3 }, 40, LOGO_BALANCE_MAX_BOOST), 40 * LOGO_BALANCE_MAX_BOOST);
});

test("a logo's own scale is bounded and 1 when unset", () => {
  assert.equal(logoScale(undefined), 1);
  assert.equal(logoScale(Number.NaN), 1);
  assert.equal(logoScale(1.2), 1.2);
  assert.equal(logoScale(3), 1.5);
  assert.equal(logoScale(0.1), 0.5);
});

test("without balance every logo is drawn at the target height", () => {
  assert.equal(logoHeightPx({ ratio: 1 }, 40, false), 40);
  assert.equal(logoHeightPx({ ratio: 6 }, 40, false), 40);
});

test("a wide wordmark is drawn at the target height, never below it", () => {
  assert.equal(logoHeightPx({ ratio: 3 }, 40, true), 40);
  assert.equal(logoHeightPx({ ratio: 5, ink: 0.35 }, 40, true), 40);
  assert.equal(logoHeightPx({ ratio: 8, ink: 0.95 }, 40, true), 40);
});

test("a square seal grows to balance a wordmark, and stops at the boost", () => {
  const seal = logoHeightPx({ ratio: 1, ink: 0.5 }, 40, true);
  assert.equal(seal, 40 * LOGO_BALANCE_MAX_BOOST);
  const between = logoHeightPx({ ratio: 2, ink: 0.5 }, 40, true);
  assert.ok(between > 40 && between < seal, `${between}`);
  // A solid badge grows less than a line-art emblem of the same shape.
  const solid = logoHeightPx({ ratio: 1.6, ink: 0.95 }, 40, true);
  const light = logoHeightPx({ ratio: 1.6, ink: 0.2 }, 40, true);
  assert.ok(solid < light, `${solid} >= ${light}`);
  assert.ok(light <= 40 * LOGO_BALANCE_MAX_BOOST);
});

test("in a fixed tile the balance is not capped: a seal grows to its equal ink area, the cell bounds it", () => {
  // A hugging cell keeps the boost's cap: the row's protection against one tall tile.
  assert.equal(logoMaxBoost({}), LOGO_BALANCE_MAX_BOOST);
  assert.equal(logoMaxBoost({ tile_height: undefined }), LOGO_BALANCE_MAX_BOOST);
  assert.equal(logoHeightPx({ ratio: 1, ink: 0.5 }, 40, true, logoMaxBoost({})), 40 * LOGO_BALANCE_MAX_BOOST);
  // A fixed tile: the stylesheet bounds the mark by the cell, so the arithmetic is the equal-area one.
  assert.equal(logoMaxBoost({ tile_height: 96 }), Infinity);
  const seal = logoHeightPx({ ratio: 1, ink: 0.5 }, 40, true, Infinity);
  assert.equal(seal, Math.round(40 * Math.sqrt(3) * 10) / 10);
  assert.ok(seal > 40 * LOGO_BALANCE_MAX_BOOST);
  // Equal ink area with the reference 3:1 wordmark at the target height (ink 0.5), to the 0.1 px rounding.
  const reference = 3 * 40 * 40 * 0.5;
  assert.ok(Math.abs(seal * seal * 0.5 - reference) / reference < 0.01);
  // A line-art seal grows further, a solid one less; a wordmark stays at the target either way.
  assert.ok(logoHeightPx({ ratio: 1, ink: 0.2 }, 40, true, Infinity) > seal);
  assert.ok(logoHeightPx({ ratio: 1, ink: 0.95 }, 40, true, Infinity) < seal);
  assert.equal(logoHeightPx({ ratio: 4.75, ink: 0.4 }, 40, true, Infinity), 40);
  // Without balance the tile changes nothing about the height asked for.
  assert.equal(logoHeightPx({ ratio: 1, ink: 0.5 }, 40, false, Infinity), 40);
});

test("the cells are the row's equal columns for even, and for start under columns", () => {
  assert.equal(logoGridEqualCells({ align: "even" }), true);
  assert.equal(logoGridEqualCells({ columns: 4 }), true, "even by default with columns");
  assert.equal(logoGridEqualCells({ align: "start", columns: 4 }), true);
  // Hugging cells: packed logos without columns, centred ones, and the spread wall.
  assert.equal(logoGridEqualCells({ align: "start" }), false);
  assert.equal(logoGridEqualCells({ align: "center", columns: 4 }), false);
  assert.equal(logoGridEqualCells({ align: "stretch", columns: 4 }), false);
  assert.equal(logoGridEqualCells({}), false, "stretch by default without columns");
});

test("the sizes stay ordered: sm < md < lg < xl for any shape", () => {
  for (const shape of [{ ratio: 1 }, { ratio: 2.5, ink: 0.4 }, { ratio: 6, ink: 0.8 }]) {
    const heights = (["sm", "md", "lg", "xl"] as const).map((size) => logoHeightPx(shape, LOGO_HEIGHT_PX[size], true));
    for (let i = 1; i < heights.length; i += 1) assert.ok(heights[i] > heights[i - 1], `${JSON.stringify(shape)}: ${heights}`);
  }
});

test("rows follow columns, rows or per_row exactly, else balanced rows of at most six", () => {
  assert.deepEqual(logoRows(8, { perRow: 4 }), [4, 4]);
  assert.deepEqual(logoRows(7, { perRow: 3 }), [3, 3, 1]);
  assert.deepEqual(logoRows(5, { rows: 4 }), [2, 1, 1, 1]);
  assert.deepEqual(logoRows(8, { rows: 3 }), [3, 3, 2]);
  assert.deepEqual(logoRows(6, {}), [6]);
  assert.deepEqual(logoRows(7, {}), [4, 3]);
  assert.deepEqual(logoRows(13, {}), [5, 4, 4]);
  assert.deepEqual(logoRows(8, { autoMaxPerRow: 4 }), [4, 4]);
  assert.deepEqual(logoRows(0, {}), []);
  assert.deepEqual(balancedRows(9, 2), [5, 4]);
});

test("a wall is 2 x 2 for four groups and caps the logos per row by the card width", () => {
  assert.deepEqual([1, 2, 3, 4, 5, 6].map(logoWallColumns), [1, 2, 3, 2, 3, 3]);
  assert.deepEqual(([1, 2, 3] as const).map(logoWallAutoMaxPerRow), [6, 4, 3]);
});

test("a file with no stored box is drawn at the size it loads at, even when it carries a revision", () => {
  assert.equal(logoShape({ rev: "abc123def456" }, { width: 300, height: 100 })?.ratio, 3);
  assert.equal(logoShape({ rev: "abc123def456" }, undefined), null);
});

test("a row shorter than the configured row is short, whatever the other rows hold", () => {
  // columns: 4 with two logos is one row of two - centred, not pinned to the edges.
  assert.deepEqual(shortRows([2], 4), [true]);
  assert.deepEqual(shortRows([4, 4, 2], 4), [false, false, true]);
  // With no count configured, a row is short against the longest one.
  assert.deepEqual(shortRows([3, 2], undefined), [false, true]);
  assert.deepEqual(shortRows([2], undefined), [false]);
  // A single logo is always centred.
  assert.deepEqual(shortRows([1], undefined), [true]);
  assert.deepEqual(shortRows([4, 1], 4), [false, true]);
});

test("a grid with columns is even unless it says otherwise; one without spreads its rows", () => {
  assert.equal(logoGridAlign({ columns: 4 }), "even");
  assert.equal(logoGridAlign({}), "stretch");
  // A stored or chosen value always wins - the materialized `stretch` of an
  // older deck included.
  for (const align of ["start", "center", "even", "stretch"] as const) {
    assert.equal(logoGridAlign({ columns: 4, align }), align);
    assert.equal(logoGridAlign({ align }), align);
  }
});

test("N logos under columns N are one row whatever the alignment, and the cells are sized for a full row", () => {
  for (const columns of [2, 3, 4, 5, 6]) {
    assert.deepEqual(logoRows(columns, { perRow: columns }), [columns]);
  }
  // Seven logos under columns 4: the short row keeps a full row's cell width.
  assert.equal(fullRowLength(logoRows(7, { perRow: 4 }), 4), 4);
  assert.equal(fullRowLength([3, 2], undefined), 3);
  assert.equal(fullRowLength([2], 4), 4);
});

test("a logo's ready signal belongs to the source it was measured for", () => {
  const first = { src: "blob:a", natural: { width: 300, height: 100 }, decoded: true };
  assert.equal(logoPending(false, "blob:a", currentLoad(first, "blob:a"), true), false);
  // Replaced in place: a new source is shown, and the old load says nothing about it.
  assert.equal(currentLoad(first, "blob:b"), undefined);
  assert.equal(logoPending(false, "blob:b", currentLoad(first, "blob:b"), true), true);
  // Bytes on their way hold it; a failed fetch (no source) or a failed load releases it.
  assert.equal(logoPending(true, null, undefined, false), true);
  assert.equal(logoPending(false, null, undefined, false), false);
  assert.equal(logoPending(false, "blob:c", { src: "blob:c", natural: null, decoded: true }, true), false);
  // An inked wall also waits for the decode.
  assert.equal(logoPending(false, "blob:a", { ...first, decoded: false }, true), true);
  assert.equal(logoPending(false, "blob:a", { ...first, decoded: false }, false), false);
});

test("a logo whose load failed is not drawn, so a removed file leaves an empty tile", () => {
  assert.equal(logoImageShown(null, undefined), false);
  assert.equal(logoImageShown("blob:a", undefined), true);
  assert.equal(logoImageShown("blob:a", { src: "blob:a", natural: { width: 4, height: 2 }, decoded: true }), true);
  assert.equal(logoImageShown("blob:a", { src: "blob:a", natural: null, decoded: true, failed: true }), false);
});

/** RGBA bytes of `count` pixels of one colour. */
function pixels(count: number, rgba: [number, number, number, number]): number[] {
  return Array.from({ length: count }, () => rgba).flat();
}

/** The ink alpha the filter gives a colour of the logo (before the source alpha). */
function inkAlpha(row: number[], [r, g, b]: [number, number, number]): number {
  const value = row[0] * (r / 255) + row[1] * (g / 255) + row[2] * (b / 255) + row[4];
  return Math.min(1, Math.max(0, value));
}

test("ink inks a yellow or orange wordmark on transparent whole, not as a pale grey", () => {
  for (const colour of [
    [250, 170, 40],
    [255, 214, 0],
  ] as [number, number, number][]) {
    // The mark, its anti-aliased edge, and the transparent ground around it.
    const bytes = [...pixels(300, [...colour, 255]), ...pixels(40, [...colour, 90]), ...pixels(600, [0, 0, 0, 0])];
    const range = logoInkRange(bytes);
    assert.ok(range);
    const row = logoInkAlphaRow(range);
    assert.ok(inkAlpha(row, colour) >= 0.9, `${colour} inked at ${inkAlpha(row, colour)}`);
  }
});

test("ink keeps the white letters of a badge and the white ground of a roundel clear", () => {
  const badge = [...pixels(400, [200, 30, 40, 255]), ...pixels(80, [255, 255, 255, 255])];
  const row = logoInkAlphaRow(logoInkRange(badge));
  assert.ok(inkAlpha(row, [200, 30, 40]) >= 0.99);
  assert.equal(inkAlpha(row, [255, 255, 255]), 0);
  // A light grey anti-aliasing step stays clear, a mid tone is ink.
  assert.equal(inkAlpha(row, [245, 245, 245]), 0);
  assert.ok(inkAlpha(row, [180, 180, 180]) >= 0.99);
});

test("ink reads the logo's own colours: a pale badge with white letters is still inked", () => {
  const pale = [...pixels(400, [250, 200, 120, 255]), ...pixels(80, [255, 255, 255, 255])];
  const row = logoInkAlphaRow(logoInkRange(pale));
  assert.ok(inkAlpha(row, [250, 200, 120]) >= 0.9);
  assert.equal(inkAlpha(row, [255, 255, 255]), 0);
});

test("ink of a one-colour or unreadable logo", () => {
  // A white mark on transparent is its alpha mask, as mono.
  assert.deepEqual(logoInkAlphaRow(logoInkRange(pixels(50, [255, 255, 255, 255]))), [0, 0, 0, 0, 1]);
  // Nothing opaque: no range; unreadable pixels fall back to absolute luminance.
  assert.equal(logoInkRange(pixels(10, [255, 0, 0, 0])), undefined);
  const fallback = logoInkAlphaRow(undefined);
  assert.equal(inkAlpha(fallback, [0, 2, 12]), 1);
  assert.equal(inkAlpha(fallback, [255, 255, 255]), 0);
});

test("a logo that loaded without an intrinsic size is still drawn: only a failed load hides it", () => {
  // A viewBox-only SVG loads fine and reports a natural size of 0 in some browsers.
  assert.equal(logoImageShown("blob:a", { src: "blob:a", natural: null, decoded: true }), true);
  assert.equal(logoImageShown("blob:a", { src: "blob:a", natural: null, decoded: true, failed: false }), true);
});
