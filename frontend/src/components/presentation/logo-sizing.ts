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
 * How big a logo is drawn, how a wall of them is cut into rows, and when a
 * logo is ready to be printed.
 *
 * Pure logic - no DOM, no React - so it is unit-tested under Node's runner
 * (`logo-sizing.test.ts`) and shared by the `logo_grid` and `logo_wall`
 * renderers (`blocks/logos.tsx`).
 *
 * A logo is sized by its MARK, not by its file: the stored geometry of the file
 * (`asset_meta`, `backend/app/services/image_geometry.py`) gives the box of its
 * non-transparent pixels, so the height a deck asks for is the height of the
 * ink, whatever padding the file was exported with. `logo_height` is the
 * height a WIDE logo is drawn at; with optical balance a squarer mark grows -
 * a seal next to a wordmark of the same height reads a fraction of its weight
 * - by the square root of how much squarer it is (equal areas), weighted by
 * its ink (a solid badge grows less than a line-art emblem), and never past
 * `LOGO_BALANCE_MAX_BOOST` times the height: square seals are capped by that
 * height, wide wordmarks by their row (the stylesheet shrinks a row too narrow
 * for its logos, all of them in proportion). In a cell of fixed height
 * (`tile_height`) the ceiling is the cell itself: the balance is not
 * capped here and the stylesheet bounds the mark by the cell's inner box, so a
 * seal grows to the height its equal ink area asks for, up to the full inner
 * height, while a wordmark takes the cell's width.
 */
import type { DeckAssetMeta, DeckLogoAlign, DeckLogoHeight } from "@/types";

/** Canvas px per `logo_height` step - mirrors `LOGO_HEIGHT_PX` in `presentation_spec.py`. */
export const LOGO_HEIGHT_PX: Record<DeckLogoHeight, number> = { sm: 28, md: 40, lg: 56, xl: 72 };
/** The aspect ratio (width / height) drawn at exactly `logo_height`: a wordmark. */
export const LOGO_BALANCE_REFERENCE_RATIO = 3;
/** Mean opacity of a reference mark inside its trimmed box. */
const LOGO_BALANCE_REFERENCE_INK = 0.5;
/** A squarer mark grows up to this multiple of `logo_height`, never more. */
export const LOGO_BALANCE_MAX_BOOST = 1.5;
/** Bounds of the ink weighting (a solid square is not shrunk to nothing). */
const LOGO_INK_FACTOR_MIN = 0.8;
const LOGO_INK_FACTOR_MAX = 1.25;
/** Logos per row a `logo_grid` puts on a row when the spec says nothing. */
const LOGO_GRID_AUTO_MAX_PER_ROW = 6;

/** A logo's trimmed shape: width / height of its mark, and its ink when known. */
interface LogoShape {
  ratio: number;
  ink?: number;
}

/** The trim box as fractions, `[x0, y0, x1, y1]`; the whole file when unknown. */
type TrimBox = [number, number, number, number];

const FULL_TRIM: TrimBox = [0, 0, 1, 1];

const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value));

/** The trim box of the stored geometry, validated; the whole file otherwise. */
export function trimBox(meta: DeckAssetMeta | undefined): TrimBox {
  const trim = meta?.trim;
  if (!trim || trim.length !== 4 || trim.some((v) => typeof v !== "number" || !Number.isFinite(v))) return FULL_TRIM;
  const [x0, y0, x1, y1] = trim.map((v) => clamp(v, 0, 1));
  return x1 - x0 > 0.001 && y1 - y0 > 0.001 ? [x0, y0, x1, y1] : FULL_TRIM;
}

/**
 * The mark's shape: from the stored geometry (trimmed), else from the size the
 * image loaded at (`natural`, untrimmed), else `null` - the image has not
 * loaded and nothing is known yet.
 */
export function logoShape(
  meta: DeckAssetMeta | undefined,
  natural?: { width: number; height: number } | null,
): LogoShape | null {
  // A `rev` without a box (the file could not be read) is no geometry at all.
  const width = meta?.w ?? 0;
  const height = meta?.h ?? 0;
  if (meta?.trim && width > 0 && height > 0) {
    const [x0, y0, x1, y1] = trimBox(meta);
    const ratio = ((x1 - x0) * width) / ((y1 - y0) * height);
    if (Number.isFinite(ratio) && ratio > 0) {
      return typeof meta.ink === "number" && meta.ink > 0 ? { ratio, ink: meta.ink } : { ratio };
    }
  }
  if (natural && natural.width > 0 && natural.height > 0) return { ratio: natural.width / natural.height };
  return null;
}

/**
 * The height (canvas px) a logo is drawn at before its cell caps its width:
 * `targetPx` for a wide mark or without balance, up to `maxBoost` times it
 * for a squarer, lighter one - `LOGO_BALANCE_MAX_BOOST` in a cell that hugs
 * its logo (the row's protection against one tall tile), `Infinity` in a
 * cell of fixed height, which the stylesheet bounds the mark by instead.
 */
export function logoHeightPx(
  shape: LogoShape,
  targetPx: number,
  balance: boolean,
  maxBoost: number = LOGO_BALANCE_MAX_BOOST,
): number {
  if (!balance) return targetPx;
  const area = Math.sqrt(LOGO_BALANCE_REFERENCE_RATIO / shape.ratio);
  const ink =
    shape.ink !== undefined && shape.ink > 0
      ? clamp(Math.sqrt(LOGO_BALANCE_REFERENCE_INK / shape.ink), LOGO_INK_FACTOR_MIN, LOGO_INK_FACTOR_MAX)
      : 1;
  return Math.round(targetPx * clamp(area * ink, 1, maxBoost) * 10) / 10;
}

/** Bounds of the optical weight factor: a long wordmark is not shrunk past this, a seal not raised past that. */
export const LOGO_WEIGHT_MIN_FACTOR = 0.6;
const LOGO_WEIGHT_MAX_FACTOR = 1.8;
/** Ink coverage the weight reads is held in this range (a hairline mark is not blown up). */
const LOGO_WEIGHT_INK_MIN = 0.2;
const LOGO_WEIGHT_INK_MAX = 1;
/** Bounds of a logo's own `scale`, mirroring `LogoItem.scale` in `presentation_spec.py`. */
const LOGO_SCALE_MIN = 0.5;
const LOGO_SCALE_MAX = 1.5;

/**
 * The height (canvas px) that gives a logo the visual weight of the reference
 * wordmark (`LOGO_BALANCE_REFERENCE_RATIO` wide, `LOGO_BALANCE_REFERENCE_INK`
 * inked) drawn at `targetPx` (`optical_weight`). The weight is the inked
 * area: height^2 x ratio x ink. Unlike the balance it also SHRINKS a mark
 * wider or heavier than the reference, so a row of wordmarks, roundels and
 * shields reads at one apparent size. The factor stays within
 * `LOGO_WEIGHT_MIN_FACTOR` and `LOGO_WEIGHT_MAX_FACTOR` (or `maxBoost` when
 * lower; a fixed tile passes `Infinity` and the stylesheet bounds the mark).
 */
export function logoWeightHeightPx(shape: LogoShape, targetPx: number, maxBoost: number = LOGO_WEIGHT_MAX_FACTOR): number {
  const ink =
    shape.ink !== undefined && shape.ink > 0
      ? clamp(shape.ink, LOGO_WEIGHT_INK_MIN, LOGO_WEIGHT_INK_MAX)
      : LOGO_BALANCE_REFERENCE_INK;
  const factor = Math.sqrt((LOGO_BALANCE_REFERENCE_RATIO * LOGO_BALANCE_REFERENCE_INK) / (shape.ratio * ink));
  const ceiling = Math.min(LOGO_WEIGHT_MAX_FACTOR, maxBoost === Infinity ? LOGO_WEIGHT_MAX_FACTOR : Math.max(maxBoost, 1));
  return Math.round(targetPx * clamp(factor, LOGO_WEIGHT_MIN_FACTOR, ceiling) * 10) / 10;
}

/** A logo's luminance range, its darkest and lightest opaque colours (BT.709, 0 to 1). */
export interface LogoInkRange {
  lo: number;
  hi: number;
}

/** A mark whose lightest colour is below this has no light interior to cut out. */
const LOGO_INK_LIGHT = 0.9;
/** A spread under this is one colour (a white mark on transparent). */
const LOGO_INK_FLAT_SPREAD = 0.08;
/** Share of the logo's own range under which a pixel stays clear, and from which it is full ink. */
const LOGO_INK_CLEAR = 0.1;
const LOGO_INK_FULL = 0.3;
/** Range assumed when the pixels could not be read: absolute black to white. */
const LOGO_INK_DEFAULT_RANGE: LogoInkRange = { lo: 0, hi: 1 };

/**
 * The luminance range of a logo's opaque pixels (alpha at least one half), from
 * RGBA bytes: the 1st and 99th percentiles, so a stray anti-aliased pixel does
 * not set it. `undefined` for a fully transparent image.
 */
export function logoInkRange(rgba: ArrayLike<number>): LogoInkRange | undefined {
  const lums: number[] = [];
  for (let i = 0; i + 3 < rgba.length; i += 4) {
    if (rgba[i + 3] < 128) continue;
    lums.push((0.2126 * rgba[i] + 0.7152 * rgba[i + 1] + 0.0722 * rgba[i + 2]) / 255);
  }
  if (lums.length === 0) return undefined;
  lums.sort((a, b) => a - b);
  const at = (q: number) => lums[Math.round(q * (lums.length - 1))];
  return { lo: at(0.01), hi: at(0.99) };
}

/**
 * The alpha row of the `tone: ink` colour matrix of ONE logo, read against its
 * own colours rather than absolute luminance (the filter then multiplies it by
 * the source alpha). A mark with a light interior (white letters on a badge, a
 * white ground) is stretched between its darkest and lightest colours: what is
 * noticeably darker than the lightest colour is full ink, the lightest is
 * clear. A mark with no light interior (an orange or yellow wordmark on
 * transparent) or of one colour is its alpha mask, as `mono`: full ink.
 */
export function logoInkAlphaRow(range: LogoInkRange | undefined): [number, number, number, number, number] {
  const { lo, hi } = range ?? LOGO_INK_DEFAULT_RANGE;
  const spread = hi - lo;
  if (hi < LOGO_INK_LIGHT || spread < LOGO_INK_FLAT_SPREAD) return [0, 0, 0, 0, 1];
  const k = 1 / (spread * (LOGO_INK_FULL - LOGO_INK_CLEAR));
  const round = (value: number) => Math.round(value * 10000) / 10000;
  return [
    round(-0.2126 * k),
    round(-0.7152 * k),
    round(-0.0722 * k),
    0,
    round(hi * k - LOGO_INK_CLEAR / (LOGO_INK_FULL - LOGO_INK_CLEAR)),
  ];
}

/** A logo's own `scale`, bounded; 1 when unset or not a finite number. */
export function logoScale(scale: number | undefined): number {
  return typeof scale === "number" && Number.isFinite(scale) ? clamp(scale, LOGO_SCALE_MIN, LOGO_SCALE_MAX) : 1;
}

/** The balance ceiling of a block: the cell bounds a fixed tile, the boost bounds a hugging cell. */
export function logoMaxBoost(block: { tile_height?: number }): number {
  return block.tile_height ? Infinity : LOGO_BALANCE_MAX_BOOST;
}

/** `n` items split into `rows` rows as evenly as possible, the longer rows first. */
export function balancedRows(n: number, rows: number): number[] {
  const count = Math.max(1, Math.min(n, Math.round(rows)));
  const base = Math.floor(n / count);
  const extra = n % count;
  return Array.from({ length: count }, (_, index) => base + (index < extra ? 1 : 0));
}

/**
 * The length of every row of a wall of `n` logos: `perRow` logos a row when
 * given (the last row takes the rest), `rows` balanced rows when given, else
 * balanced rows of at most `autoMaxPerRow`.
 */
export function logoRows(
  n: number,
  { perRow, rows, autoMaxPerRow = LOGO_GRID_AUTO_MAX_PER_ROW }: { perRow?: number; rows?: number; autoMaxPerRow?: number },
): number[] {
  if (n <= 0) return [];
  if (perRow && perRow > 0) {
    const size = Math.round(perRow);
    return Array.from({ length: Math.ceil(n / size) }, (_, index) => Math.min(size, n - index * size));
  }
  if (rows && rows > 0) return balancedRows(n, rows);
  return balancedRows(n, Math.ceil(n / Math.max(1, autoMaxPerRow)));
}

/**
 * How the cells of a `logo_grid` row share its width: the block's own
 * `align`, else `even` when it sets `columns` - equal cells with each logo
 * centred, so a grid under a timeline of as many steps lines up with them -
 * and `stretch` when the renderer picks the row length.
 */
export function logoGridAlign(block: { align?: DeckLogoAlign; columns?: number }): DeckLogoAlign {
  return block.align ?? (block.columns ? "even" : "stretch");
}

/**
 * Whether the cells of a `logo_grid` row are the row's equal columns (each
 * logo centred in its own) rather than cells hugging their logos: `even`,
 * and `start` under `columns` - the equal cells packed from the
 * start, so a short row lines up with its siblings' columns. `center` and
 * `stretch`, and `start` without `columns`, keep hugging cells.
 */
export function logoGridEqualCells(block: { align?: DeckLogoAlign; columns?: number }): boolean {
  const align = logoGridAlign(block);
  return align === "even" || (align === "start" && Boolean(block.columns));
}

/**
 * The length of a full row: the configured count (`columns` / `per_row`) when
 * there is one, else the longest row. The cells of an `even` grid are sized
 * for it, a short row's included, so the columns of every row line up.
 */
export function fullRowLength(rows: number[], capacity: number | undefined): number {
  return capacity && capacity > 0 ? Math.round(capacity) : Math.max(1, ...rows);
}

/**
 * Which rows are short - centred rather than spread edge to edge: a row
 * holding fewer logos than a full row, or a single logo. A full row is the
 * configured count (`columns` / `per_row`) when there is one - two logos under
 * `columns: 4` are a short row even when they are the only one - else the
 * longest row.
 */
export function shortRows(rows: number[], capacity: number | undefined): boolean[] {
  const full = fullRowLength(rows, capacity);
  return rows.map((length) => length < full || length === 1);
}

/** What a logo's `<img>` reported, and for which source. */
export interface LogoLoad {
  src: string;
  /** The size it loaded at, `null` for a failed load or an image with no intrinsic size (a viewBox-only SVG). */
  natural: { width: number; height: number } | null;
  decoded: boolean;
  /** The load failed (`onError`): the only state in which the `<img>` is not drawn. */
  failed?: boolean;
  /** The luminance range of its pixels, read at decode for `tone: ink` (`logoInkRange`). */
  inkRange?: LogoInkRange;
}

/** The load that belongs to `src`: one reported for a previous source says nothing about it. */
export function currentLoad(load: LogoLoad | null, src: string | null): LogoLoad | undefined {
  return load && src !== null && load.src === src ? load : undefined;
}

/**
 * Whether a logo still holds the deck's ready signal (`data-deck-pending`):
 * its bytes are on their way, or the source now shown has not loaded - and,
 * for an inked wall, decoded - yet. A failed fetch (no source) or a failed
 * load releases it: a deleted logo must never hold the print, whose wait has
 * no time bound.
 */
export function logoPending(
  loading: boolean,
  src: string | null,
  load: LogoLoad | undefined,
  needsDecode: boolean,
): boolean {
  if (loading) return true;
  if (src === null) return false;
  if (!load) return true;
  if (load.natural === null) return false;
  return needsDecode && !load.decoded;
}

/**
 * Whether the `<img>` of a logo is drawn: once its load failed it is not, so a
 * file that no longer exists leaves an empty tile on the static export page as
 * it does on screen, never a broken-image glyph.
 */
export function logoImageShown(src: string | null, load: LogoLoad | undefined): src is string {
  return src !== null && !load?.failed;
}

/** Cards per row of a `logo_wall` when the spec says nothing: all of them up to three, 2 x 2 for four. */
export function logoWallColumns(groups: number): 1 | 2 | 3 {
  if (groups <= 1) return 1;
  if (groups === 2 || groups === 4) return 2;
  return 3;
}

/** Logos per row inside a card when the spec says nothing: a narrower card holds fewer. */
export function logoWallAutoMaxPerRow(columns: 1 | 2 | 3): number {
  return columns === 1 ? 6 : columns === 2 ? 4 : 3;
}
