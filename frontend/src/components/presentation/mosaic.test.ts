/**
 * Unit tests for the `mosaic` hero's composition - `yarn test:unit`.
 *
 * What is pinned is what a cover relies on: the same slide draws the same
 * tiles everywhere (screen, PDF, PPTX picture), no tile overlaps another, and
 * no tile lands where the cover's text or chrome sits, on a 16:9 slide, a 4:3
 * slide and a portrait page alike.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  MOSAIC_SLOTS,
  MOSAIC_TILE,
  mosaicSeed,
  mosaicTiles,
  type MosaicComposition,
  type MosaicGrid,
  type MosaicPattern,
} from "./mosaic.ts";

const SHAPES: MosaicPattern[] = ["cascade", "horizon", "pillar"];

const CANVASES = [
  { width: 1920, height: 1080 },
  { width: 1440, height: 1080 },
  { width: 1920, height: 2715 },
];

function cells(grid: MosaicGrid): Array<[number, number]> {
  return grid.tiles.flatMap((tile) => {
    const out: Array<[number, number]> = [];
    for (let r = tile.row; r < tile.row + tile.h; r += 1) {
      for (let c = tile.col; c < tile.col + tile.w; c += 1) out.push([c, r]);
    }
    return out;
  });
}

test("the same seed and canvas give the same tiles, another seed others", () => {
  const a = mosaicTiles({ seed: "slide-1", composition: "start", width: 1920, height: 1080 });
  const b = mosaicTiles({ seed: "slide-1", composition: "start", width: 1920, height: 1080 });
  const c = mosaicTiles({ seed: "slide-2", composition: "start", width: 1920, height: 1080 });
  assert.deepEqual(a, b);
  assert.notDeepEqual(a.tiles, c.tiles);
  assert.equal(mosaicSeed("x"), mosaicSeed("x"));
  assert.notEqual(mosaicSeed("x"), mosaicSeed("y"));
});

test("the grid follows the canvas in whole tiles", () => {
  for (const canvas of CANVASES) {
    const grid = mosaicTiles({ seed: "s", composition: "center", ...canvas });
    assert.equal(grid.cols, Math.round(canvas.width / MOSAIC_TILE));
    assert.equal(grid.rows, Math.round(canvas.height / MOSAIC_TILE));
  }
});

test("tiles never overlap, stay on the grid and carry a known slot", () => {
  for (const composition of ["start", "center"] as MosaicComposition[]) {
    for (const canvas of CANVASES) {
      for (const seed of ["a", "b", "c", "d"]) {
        const grid = mosaicTiles({ seed, composition, ...canvas });
        const seen = new Set<string>();
        for (const [c, r] of cells(grid)) {
          const key = `${c},${r}`;
          assert.ok(!seen.has(key), `${composition} ${seed}: cell ${key} covered twice`);
          seen.add(key);
          assert.ok(c >= 0 && c < grid.cols && r >= 0 && r < grid.rows, `${key} off the grid`);
        }
        for (const tile of grid.tiles) {
          assert.ok(tile.slot >= 1 && tile.slot <= MOSAIC_SLOTS);
          assert.ok([1, 2, 3].includes(tile.w) && [1, 2, 3].includes(tile.h));
        }
        assert.ok(grid.tiles.length >= 12, `${composition} ${seed}: a mosaic of ${grid.tiles.length} tiles`);
      }
    }
  }
});

test("a left-aligned cover keeps its headline column and its logo corner clear", () => {
  for (const canvas of CANVASES) {
    for (const seed of ["a", "b", "c", "d", "e"]) {
      const grid = mosaicTiles({ seed, composition: "start", ...canvas });
      for (const [c, r] of cells(grid)) {
        const inHeadline = c < Math.ceil(grid.cols * 0.7) && r >= Math.floor(grid.rows * 0.12) && r < Math.ceil(grid.rows * 0.88);
        const inLogo = c < Math.ceil(grid.cols * 0.3) && r >= grid.rows - 3;
        assert.ok(!inHeadline && !inLogo, `start ${seed}: tile on ${c},${r}`);
      }
    }
  }
});

test("a centred cover keeps its content, its logo and its slide number clear", () => {
  for (const canvas of CANVASES) {
    for (const seed of ["a", "b", "c", "d", "e"]) {
      const grid = mosaicTiles({ seed, composition: "center", ...canvas });
      const { cols, rows } = grid;
      for (const [c, r] of cells(grid)) {
        const inContent =
          c >= Math.floor(cols * 0.17) && c < Math.ceil(cols * 0.83) && r >= Math.floor(rows * 0.17) && r < Math.ceil(rows * 0.83);
        const inLogo = c >= Math.floor(cols * 0.34) && c < Math.ceil(cols * 0.66) && r >= rows - 3;
        const inNumber = c >= cols - 5 && r >= rows - 2;
        assert.ok(!inContent && !inLogo && !inNumber, `center ${seed}: tile on ${c},${r}`);
      }
    }
  }
});

test("a cluster is tiles, not a blob: no tile shares its slot with the one on its left", () => {
  const grid = mosaicTiles({ seed: "dense", composition: "start", width: 1920, height: 1080 });
  const slotAt = new Map<string, number>();
  for (const tile of grid.tiles) slotAt.set(`${tile.col},${tile.row}`, tile.slot);
  let neighbours = 0;
  let same = 0;
  for (const tile of grid.tiles) {
    if (tile.w !== 1) continue;
    const left = slotAt.get(`${tile.col - 1},${tile.row}`);
    if (left === undefined) continue;
    neighbours += 1;
    if (left === tile.slot) same += 1;
  }
  assert.ok(neighbours > 0);
  assert.ok(same / neighbours < 0.1, `${same} of ${neighbours} left neighbours share a colour`);
});

test("the scatter is the default pattern, and every shape is deterministic and disjoint", () => {
  const base = { seed: "s", composition: "start" as const, width: 1920, height: 1080 };
  assert.deepEqual(mosaicTiles(base), mosaicTiles({ ...base, pattern: "scatter" }));
  for (const pattern of SHAPES) {
    for (const composition of ["start", "center"] as MosaicComposition[]) {
      for (const canvas of CANVASES) {
        const grid = mosaicTiles({ seed: "x", pattern, composition, ...canvas });
        assert.deepEqual(grid, mosaicTiles({ seed: "x", pattern, composition, ...canvas }));
        assert.equal(grid.tile, 48);
        assert.equal(grid.cols, Math.round(canvas.width / 48));
        const seen = new Set<string>();
        for (const [c, r] of cells(grid)) {
          assert.ok(!seen.has(`${c},${r}`), `${pattern} ${composition}: ${c},${r} twice`);
          seen.add(`${c},${r}`);
          assert.ok(c >= 0 && c < grid.cols && r >= 0 && r < grid.rows);
        }
        assert.ok(grid.tiles.length >= 12, `${pattern} ${composition}: ${grid.tiles.length} tiles`);
      }
      // Another seed, another arrangement.
      assert.notDeepEqual(
        mosaicTiles({ seed: "x", pattern, composition, width: 1920, height: 1080 }).tiles,
        mosaicTiles({ seed: "y", pattern, composition, width: 1920, height: 1080 }).tiles,
      );
    }
  }
});

test("a cascade steps down from the top-right corner, clear of the hosted logo and the low headline", () => {
  for (const canvas of CANVASES) {
    for (const seed of ["a", "b", "c", "d", "e"]) {
      const grid = mosaicTiles({ seed, pattern: "cascade", composition: "start", ...canvas });
      const { cols, rows } = grid;
      let right = 0;
      for (const [c, r] of cells(grid)) {
        assert.ok(r < Math.floor(rows * 0.34), `cascade ${seed}: tile low on the slide at ${c},${r}`);
        assert.ok(!(c < Math.ceil(cols * 0.3) && r < Math.ceil(rows * 0.24)), `cascade ${seed}: tile on the logo at ${c},${r}`);
        if (c >= cols / 2) right += 1;
      }
      assert.ok(right / cells(grid).length > 0.85, `cascade ${seed}: the tiles lean right`);
      // The corner itself is always filled: the cascade starts there.
      assert.ok(cells(grid).some(([c, r]) => c >= cols - 2 && r <= 1), `cascade ${seed}: an empty corner`);
    }
  }
});

test("a horizon rises from the bottom edge and a pillar runs down the right one", () => {
  for (const seed of ["a", "b", "c", "d", "e"]) {
    const horizon = mosaicTiles({ seed, pattern: "horizon", composition: "start", width: 1920, height: 1080 });
    for (const [c, r] of cells(horizon)) {
      assert.ok(horizon.rows - r - 0.5 < horizon.rows * 0.22 + 1.5, `horizon ${seed}: tile high at ${c},${r}`);
      assert.ok(!(c < Math.ceil(horizon.cols * 0.3) && r < Math.ceil(horizon.rows * 0.24)), `horizon ${seed}: on the logo`);
    }
    const pillar = mosaicTiles({ seed, pattern: "pillar", composition: "start", width: 1920, height: 1080 });
    for (const [c, r] of cells(pillar)) {
      assert.ok(c >= Math.ceil(pillar.cols * 0.68), `pillar ${seed}: tile in the headline column at ${c},${r}`);
    }
    for (const pattern of SHAPES) {
      const centred = mosaicTiles({ seed, pattern, composition: "center", width: 1920, height: 1080 });
      const { cols, rows } = centred;
      for (const [c, r] of cells(centred)) {
        const inContent =
          c >= Math.floor(cols * 0.18) && c < Math.ceil(cols * 0.82) && r >= Math.floor(rows * 0.2) && r < Math.ceil(rows * 0.76);
        const inNumber = c >= cols - 5 && r >= rows - 2;
        assert.ok(!inContent && !inNumber, `${pattern} center ${seed}: tile on ${c},${r}`);
      }
    }
  }
});
