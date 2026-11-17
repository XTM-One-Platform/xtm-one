/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The tiled heroes' composition - the pure half, unit-tested in
 * `mosaic.test.ts`. `HeroBackdrop` draws what this returns.
 *
 * A grid of square tiles on the slide canvas, filled where a density field
 * says so and never inside the zones a cover's content and chrome occupy. A
 * tile is 1x1 most of the time, sometimes 2x1, 2x2 or 3x3 where the field is
 * dense - the pixel-block language of a mosaic, never a pattern that repeats.
 *
 * Four PATTERNS, one per tiled hero (`MOSAIC_HEROES` in `hero.ts`):
 *   - `scatter` (`mosaic`) clusters grown from the corners the text leaves
 *     free, with a sparse band along the top edge;
 *   - `cascade` tiles stepping down from the top-right corner - dense at the
 *     corner, thinning along a diagonal, a run along the top edge and down the
 *     right one - the cover of the THREAD marketing kit;
 *   - `horizon` a ragged skyline of tiles rising from the bottom edge;
 *   - `pillar` a ragged column of tiles down the right edge.
 *
 * Two COMPOSITIONS, after the cover layouts: `start` (a left-aligned title:
 * the headline column keeps the left, the logo its corner) and `center` (the
 * closing slide and a centred title: the content keeps the middle, so the
 * tiles take the corners or both edges, and the logo and the slide number
 * keep theirs).
 *
 * Deterministic: the same seed (the slide id) and the same canvas give the
 * same tiles on screen, in the PDF and in the PPTX picture, render after
 * render. A tile carries a colour SLOT (1-6) and a `tint` flag, never a
 * colour: the stylesheet maps the slots onto the slide's palette through the
 * hero tone, so the mosaic follows the template and the slide's scheme.
 */

/** Edge of one `scatter` tile, in canvas pixels. */
export const MOSAIC_TILE = 60;
/** How many colour slots a tile may take (`--deck-hero-mosaic-1..6`). */
export const MOSAIC_SLOTS = 6;

export type MosaicPattern = "scatter" | "cascade" | "horizon" | "pillar";
export type MosaicComposition = "start" | "center";

/** Edge of one tile per pattern: the shapes that run along an edge are finer. */
const PATTERN_TILE: Readonly<Record<MosaicPattern, number>> = {
  scatter: MOSAIC_TILE,
  cascade: 48,
  horizon: 48,
  pillar: 48,
};

interface MosaicTile {
  /** Grid column and row of the tile's top-left cell. */
  col: number;
  row: number;
  /** Span in cells (1, 2 or 3). */
  w: number;
  h: number;
  /** Colour slot, 1..MOSAIC_SLOTS. */
  slot: number;
  /** A lighter step of the slot's colour. */
  tint: boolean;
}

export interface MosaicGrid {
  cols: number;
  rows: number;
  /** Edge of one cell, in canvas pixels. */
  tile: number;
  tiles: MosaicTile[];
}

/** FNV-1a: a stable 32-bit seed from any string. */
export function mosaicSeed(value: string): number {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

/** mulberry32: tiny, deterministic, plenty for scattering tiles. */
function random(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

interface Zone {
  /** Half-open cell ranges [c0, c1) x [r0, r1) where no tile may land. */
  c0: number;
  c1: number;
  r0: number;
  r1: number;
}

interface Layout {
  /** Fill probability of the cell at (col, row), 0..1. */
  density: (col: number, row: number) => number;
  zones: Zone[];
}

/** A zone in fractions of the grid, rounded outwards to whole cells. */
function zone(cols: number, rows: number, c0: number, c1: number, r0: number, r1: number): Zone {
  return { c0: Math.floor(cols * c0), c1: Math.ceil(cols * c1), r0: Math.floor(rows * r0), r1: Math.ceil(rows * r1) };
}

/** A cluster grown from (x, y): round (`ellipse`) or stepping along a diagonal (`diamond`). */
function cluster(x: number, y: number, rx: number, ry: number, peak: number, shape: "ellipse" | "diamond") {
  return (col: number, row: number) => {
    const dx = Math.abs(col + 0.5 - x) / rx;
    const dy = Math.abs(row + 0.5 - y) / ry;
    const distance = shape === "ellipse" ? Math.sqrt(dx * dx + dy * dy) : dx + dy;
    if (distance >= 1) return 0;
    return peak * Math.pow(1 - distance, shape === "ellipse" ? 1.25 : 0.55);
  };
}

/**
 * A ragged edge: one depth per column (or row), a smoothed random walk scaled
 * to [low, high] of *reach* cells, so neighbours differ by a step and never
 * jump - a skyline, not noise.
 */
function raggedEdge(length: number, reach: number, low: number, high: number, next: () => number): number[] {
  const raw = Array.from({ length }, () => next());
  const smooth = raw.map((_, index) => {
    let sum = 0;
    let count = 0;
    for (let offset = -2; offset <= 2; offset += 1) {
      const at = index + offset;
      if (at >= 0 && at < length) {
        sum += raw[at];
        count += 1;
      }
    }
    return sum / count;
  });
  const min = Math.min(...smooth);
  const span = Math.max(...smooth) - min || 1;
  return smooth.map((value) => reach * (low + (high - low) * ((value - min) / span)));
}

/** Tiles inside a ragged edge: dense at the slide's edge, lighter at the inner end, a few strays past it. */
function edgeDensity(depth: number, extent: number): number {
  if (depth < extent) return 0.94 - 0.36 * (depth / extent);
  return depth < extent + 1.5 ? 0.07 : 0;
}

function layoutFor(
  pattern: MosaicPattern,
  composition: MosaicComposition,
  cols: number,
  rows: number,
  next: () => number,
): Layout {
  const centred = composition === "center";
  // The centred cover's content keeps the middle, its slide number the bottom
  // right corner (the closing slide); a cover that hosts the logo keeps the
  // top of its axis, one that does not the bottom of it.
  const middle = zone(cols, rows, 0.17, 0.83, 0.17, 0.83);
  const number: Zone = { c0: cols - 5, c1: cols, r0: rows - 2, r1: rows };
  const hostedLogo = centred ? zone(cols, rows, 0.34, 0.66, 0, 0.2) : zone(cols, rows, 0, 0.3, 0, 0.24);
  const chromeLogo: Zone = centred
    ? { c0: Math.floor(cols * 0.34), c1: Math.ceil(cols * 0.66), r0: rows - 3, r1: rows }
    : { c0: 0, c1: Math.ceil(cols * 0.3), r0: rows - 3, r1: rows };

  if (pattern === "cascade") {
    const corners = centred
      ? [
          cluster(cols, 0, cols * 0.34, rows * 0.32, 0.95, "diamond"),
          cluster(0, rows, cols * 0.34, rows * 0.32, 0.92, "diamond"),
        ]
      : [cluster(cols, 0, cols * 0.38, rows * 0.28, 0.9, "diamond")];
    return {
      density: (col, row) => {
        let best = Math.max(...corners.map((corner) => corner(col, row)));
        // A run along the top edge and down the right one, thinning away from the corner.
        if (row === 0) best = Math.max(best, 0.62 * Math.max(0, 1 - (cols - col - 0.5) / (cols * 0.45)));
        if (col === cols - 1) best = Math.max(best, 0.55 * Math.max(0, 1 - (row + 0.5) / (rows * 0.32)));
        return Math.min(best, 0.95);
      },
      // The headline sits low on a cascade cover: the content zone is the lower body.
      zones: centred
        ? [middle, hostedLogo, number]
        : [hostedLogo, zone(cols, rows, 0, 0.82, 0.34, 1)],
    };
  }

  if (pattern === "horizon") {
    const heights = raggedEdge(cols, rows * 0.22, 0.35, 1, next);
    return {
      density: (col, row) => edgeDensity(rows - row - 0.5, heights[col]),
      zones: centred
        ? [zone(cols, rows, 0.17, 0.83, 0.12, 0.76), hostedLogo, number]
        : [hostedLogo, zone(cols, rows, 0, 0.72, 0.14, 0.76)],
    };
  }

  if (pattern === "pillar") {
    const right = raggedEdge(rows, cols * (centred ? 0.14 : 0.2), 0.4, 1, next);
    const left = centred ? raggedEdge(rows, cols * 0.14, 0.4, 1, next) : null;
    return {
      density: (col, row) =>
        Math.max(edgeDensity(cols - col - 0.5, right[row]), left ? edgeDensity(col + 0.5, left[row]) : 0),
      zones: centred
        ? [zone(cols, rows, 0.18, 0.82, 0, 1), chromeLogo, number]
        : [zone(cols, rows, 0, 0.68, 0, 1)],
    };
  }

  // `scatter`, the `mosaic` hero.
  const clusters = centred
    ? [
        cluster(0, 0, cols * 0.3, rows * 0.45, 0.9, "ellipse"),
        cluster(cols, 0, cols * 0.3, rows * 0.45, 0.9, "ellipse"),
        cluster(0, rows, cols * 0.32, rows * 0.4, 0.85, "ellipse"),
        cluster(cols, rows, cols * 0.26, rows * 0.36, 0.8, "ellipse"),
      ]
    : [
        cluster(cols, 0, cols * 0.38, rows * 0.5, 0.92, "ellipse"),
        cluster(cols, rows, cols * 0.32, rows * 0.32, 0.85, "ellipse"),
      ];
  const band = centred ? 0.22 : 0.3;
  return {
    density: (col, row) => {
      let best = Math.max(...clusters.map((each) => each(col, row)));
      // A sparse band along the top edge, a little denser towards the right.
      if (row <= 1) best = Math.max(best, band * (row === 0 ? 1 : 0.5) * (0.5 + (col + 0.5) / cols / 2));
      return Math.min(best, 0.95);
    },
    zones: centred
      ? [middle, chromeLogo, number]
      : [
          // The headline column, eyebrow to footer line, and the logo's corner.
          zone(cols, rows, 0, 0.7, 0.12, 0.88),
          chromeLogo,
        ],
  };
}

/** The tiles of one tiled backdrop. */
export function mosaicTiles({
  seed,
  pattern = "scatter",
  composition,
  width,
  height,
}: {
  seed: string;
  pattern?: MosaicPattern;
  composition: MosaicComposition;
  /** Canvas size in pixels (`deckSize()`). */
  width: number;
  height: number;
}): MosaicGrid {
  const tile = PATTERN_TILE[pattern];
  const cols = Math.max(4, Math.round(width / tile));
  const rows = Math.max(4, Math.round(height / tile));
  // The scatter keeps the seed it always had, so a `mosaic` cover drawn
  // before the other patterns existed keeps its tiles.
  const next = random(mosaicSeed(pattern === "scatter" ? `${composition}:${seed}` : `${pattern}:${composition}:${seed}`));
  const { density, zones } = layoutFor(pattern, composition, cols, rows, next);
  const blocked = (col: number, row: number) =>
    zones.some((each) => col >= each.c0 && col < each.c1 && row >= each.r0 && row < each.r1);
  const slots = new Int8Array(cols * rows);
  const free = (col: number, row: number, w: number, h: number) => {
    if (col + w > cols || row + h > rows) return false;
    for (let r = row; r < row + h; r += 1) {
      for (let c = col; c < col + w; c += 1) {
        if (slots[r * cols + c] !== 0 || blocked(c, r)) return false;
      }
    }
    return true;
  };
  const tiles: MosaicTile[] = [];
  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < cols; col += 1) {
      if (slots[row * cols + col] !== 0 || blocked(col, row)) continue;
      const p = density(col, row);
      if (p <= 0 || next() >= p) continue;
      let w = 1;
      let h = 1;
      const roll = next();
      if (p > 0.7 && roll < 0.05 && free(col, row, 3, 3)) {
        w = 3;
        h = 3;
      } else if (p > 0.45 && roll < 0.2 && free(col, row, 2, 2)) {
        w = 2;
        h = 2;
      } else if (roll > 0.92 && free(col, row, 2, 1)) {
        w = 2;
      }
      // Never the colour of the tile on the left or above: a cluster reads as
      // tiles, not as one blob of a colour.
      const left = col > 0 ? slots[row * cols + col - 1] : 0;
      const above = row > 0 ? slots[(row - 1) * cols + col] : 0;
      let slot = 1 + Math.floor(next() * MOSAIC_SLOTS);
      for (let attempt = 0; attempt < 4 && (slot === left || slot === above); attempt += 1) {
        slot = 1 + Math.floor(next() * MOSAIC_SLOTS);
      }
      for (let r = row; r < row + h; r += 1) {
        for (let c = col; c < col + w; c += 1) slots[r * cols + c] = slot;
      }
      tiles.push({ col, row, w, h, slot, tint: next() < 0.28 });
    }
  }
  return { cols, rows, tile, tiles };
}
