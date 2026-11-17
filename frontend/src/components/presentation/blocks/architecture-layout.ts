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
 * Layout engine of the native `architecture` diagram block.
 *
 * The agent authors DATA — nodes on a small grid, labeled edges, zones, a
 * legend (`ArchitectureBlock` in `presentation_spec.py`) — and this module turns
 * it into geometry on the 1920px canvas: node boxes sized from their content,
 * zone panels behind the cells they cover, edges routed so they never run
 * through a node when an alternative exists, anchored at the CENTER of a node
 * side and spread into parallel lanes when several edges share one side (an
 * exchange in both directions runs as two parallel lines, never as a cross),
 * labels on the longest leg of their edge. `Architecture.tsx` draws the result
 * as inline SVG through React.
 *
 * Pure and deterministic on purpose: no DOM, no measurement, the same spec
 * gives the same picture in the viewer, the chat card, the export and the
 * layout check. Text is measured by a per-glyph width table with a safety
 * margin rather than by the browser — a label wraps a little early rather than
 * ever running past its box, and a word wider than its box shrinks its font
 * instead of overflowing.
 *
 * The one thing the renderer tells the engine is the ROOM the drawing has
 * (`layoutArchitecture(block, { room })`, in canvas px): the drawing is never
 * magnified to fill it - a row is as tall as its content, whatever the column
 * count - and when it is taller than the room at 1:1 the fit pass compacts the
 * vertical metrics (`DENSITY_MIN`) before the slide's own shrink-to-fit ever
 * sees the overflow. Without a room the layout is the natural one.
 *
 * The renderer also hands over the stored geometry of the deck's logos
 * (`assets`, the `asset_meta` of the deck response): a zone logo is as
 * wide as its mark's ratio makes it, and the label after it starts where the
 * mark ends, before any byte of the file has arrived. Without it a zone logo
 * takes a wordmark's ratio.
 *
 * An edge may pin the side it leaves and enters (`source_side` /
 * `target_side`), order the edges of one pair of nodes on `lane`s, and end on
 * a zone's border (`zone:<id, label or index>`): such an edge is routed
 * through the `via` candidates, which honour a side even when it faces away,
 * and the edges of a pair running between two facing sides take one offset
 * each, the same on both nodes, so a straight one stays level.
 */
import type {
  DeckArchitectureBlock,
  DeckArchitectureEdge,
  DeckArchitectureLegendItem,
  DeckArchitectureNode,
  DeckArchitectureZone,
  DeckAssetMeta,
  DeckEdgeArrows,
  DeckLegendMarker,
  DeckNodeEmphasis,
  DeckNodeShape,
  DeckPaletteColor,
  DeckTone,
} from "@/types";

import { LOGO_BALANCE_REFERENCE_RATIO, LOGO_HEIGHT_PX, logoHeightPx, logoShape } from "../logo-sizing";
import { markupToPlainText, parseInlineMarkup, type MarkupSpanKind } from "../markup";

// ── Canvas metrics (px on the 1920x1080 canvas) ───────────────────────────────

export const ARCH = {
  PAD: 28,
  COL_W: 280,
  COL_GAP: 96,
  ROW_GAP: 84,
  /** Room for a zone's title above the first row it covers. */
  ZONE_HEAD: 52,
  ZONE_INSET: 22,
  /** Spacing between parallel edges attached to one node side. */
  LANE: 44,
  CORNER: 18,
  CARD_PAD_X: 22,
  CARD_PAD_Y: 20,
  ICON_TILE: 52,
  ICON: 30,
  LOGO_H: 44,
  LABEL_FS: 26,
  LABEL_LH: 32,
  LABEL_MIN_FS: 19,
  SUB_FS: 19,
  SUB_LH: 24,
  CHIP_FS: 17,
  CHIP_H: 30,
  CHIP_PAD: 12,
  CHIP_GAP: 8,
  EDGE_FS: 19,
  EDGE_LABEL_H: 34,
  EDGE_LABEL_PAD: 14,
  /**
   * How far an arrowhead runs back from its tip along the line: the marker of
   * `Architecture.tsx` (viewBox 12, drawn from x 1 to its tip at refX 10, 16
   * units wide in user space) is 9 x 16 / 12 long.
   */
  ARROW_HEAD: 12,
  ZONE_FS: 21,
  ZONE_ICON: 24,
  /** A zone logo is as tall as the title row leaves it, never taller than this. */
  ZONE_LOGO_H: 40,
  LEGEND_FS: 19,
  LEGEND_H: 44,
  LEGEND_GAP: 40,
  LEGEND_MARKER_W: 44,
  CIRCLE_D: 172,
  CORE_D: 204,
  PILL_H: 64,
  PILL_PAD_X: 30,
  MIN_CARD_H: 112,
  MIN_BAND_H: 92,
  EMPTY_ROW_H: 40,
  MAX_LABEL_LINES: 4,
} as const;

// ── Text measurement ──────────────────────────────────────────────────────────

/** Glyph advance in em for the deck's two sans faces, conservative. */
const NARROW = new Set(["i", "l", "j", "t", "f", "r", ".", ",", ":", ";", "'", "!", "|", "I"]);
const WIDE = new Set(["m", "w", "M", "W", "%", "@"]);
const CHAR_EM: Record<string, number> = {
  " ": 0.28,
  "-": 0.36,
  "(": 0.34,
  ")": 0.34,
  "[": 0.34,
  "]": 0.34,
  "/": 0.38,
  "&": 0.72,
  "+": 0.6,
  "=": 0.6,
  "#": 0.62,
  "*": 0.45,
  '"': 0.42,
  "·": 0.3,
  J: 0.5,
};

function charEm(ch: string): number {
  const known = CHAR_EM[ch];
  if (known !== undefined) return known;
  if (NARROW.has(ch)) return ch === "I" ? 0.32 : ch === "r" ? 0.38 : ch === "t" || ch === "f" ? 0.34 : 0.28;
  if (WIDE.has(ch)) return ch === "%" || ch === "@" ? 0.92 : 0.88;
  const code = ch.codePointAt(0) ?? 0;
  if (code >= 0x30 && code <= 0x39) return 0.6;
  if (code >= 0x41 && code <= 0x5a) return 0.68;
  if (code >= 0x61 && code <= 0x7a) return 0.56;
  if (code < 0x80) return 0.5;
  // Accented Latin is as wide as its base letter; anything else (CJK, symbols)
  // is treated as a full em, which only ever wraps early.
  if (code >= 0xc0 && code <= 0x24f) return code >= 0xc0 && code <= 0xde ? 0.68 : 0.56;
  return 1;
}

/** Width of a run of text, in px, at `fontSize`. Slightly over, never under. */
export function estimateTextWidth(text: string, fontSize: number, weight = 400, mono = false): number {
  if (!text) return 0;
  let em = 0;
  for (const ch of text) em += mono ? 0.6 : charEm(ch);
  const weightFactor = weight >= 600 ? 1.06 : weight >= 500 ? 1.02 : 1;
  return em * fontSize * weightFactor * 1.04;
}

interface TextRun {
  text: string;
  bold?: boolean;
  em?: boolean;
  code?: boolean;
  tone?: MarkupSpanKind;
}

interface TextLine {
  runs: TextRun[];
  width: number;
}

export interface TextBlock {
  lines: TextLine[];
  /** The widest line. */
  width: number;
  height: number;
  /** The size actually used — smaller than requested when a word had to fit. */
  fontSize: number;
  lineHeight: number;
}

interface Word {
  text: string;
  style: Omit<TextRun, "text">;
  width: number;
  /** A `\n` in the markup: the line ends after this word. */
  breakAfter: boolean;
}

function styleKey(style: Omit<TextRun, "text">): string {
  return `${style.bold ? 1 : 0}${style.em ? 1 : 0}${style.code ? 1 : 0}${style.tone ?? ""}`;
}

function wordsOf(markup: string, fontSize: number, weight: number): Word[] {
  const words: Word[] = [];
  for (const segment of parseInlineMarkup(markup)) {
    if (segment.kind === "br") {
      if (words.length) words[words.length - 1].breakAfter = true;
      continue;
    }
    const style: Omit<TextRun, "text"> = {
      bold: segment.kind === "bold" || undefined,
      em: segment.kind === "em" || undefined,
      code: segment.kind === "code" || undefined,
      tone: segment.kind === "span" ? segment.tone : undefined,
    };
    for (const text of segment.text.split(/\s+/)) {
      if (!text) continue;
      const bold = weight >= 600 || style.bold ? 600 : weight;
      words.push({
        text,
        style,
        width: estimateTextWidth(text, fontSize, bold, style.code),
        breakAfter: false,
      });
    }
  }
  return words;
}

/**
 * Break one word that is still wider than the box at the final font size into
 * runs that each fit (the SVG equivalent of `overflow-wrap: anywhere`): a
 * valid 40-character token is ~470px at the minimum size against a 236px card
 * column, so leaving it whole would paint across the neighbouring node.
 */
function splitWord(word: Word, maxWidth: number, fontSize: number, weight: number): Word[] {
  const bold = weight >= 600 || word.style.bold ? 600 : weight;
  const measure = (text: string) => estimateTextWidth(text, fontSize, bold, word.style.code);
  const pieces: Word[] = [];
  let chunk = "";
  for (const char of Array.from(word.text)) {
    if (chunk && measure(chunk + char) > maxWidth) {
      pieces.push({ text: chunk, style: word.style, width: measure(chunk), breakAfter: false });
      chunk = "";
    }
    chunk += char;
  }
  pieces.push({ text: chunk, style: word.style, width: measure(chunk), breakAfter: word.breakAfter });
  return pieces;
}

/**
 * Wrap a markup string into lines no wider than `maxWidth`. A single word
 * wider than the box shrinks the font (down to `minFontSize`) instead of
 * overflowing; a word still too wide at the minimum is broken into runs that
 * fit; past `maxLines` the tail is elided.
 */
function layoutText(
  markup: string,
  options: {
    fontSize: number;
    lineHeight: number;
    weight: number;
    maxWidth: number;
    maxLines?: number;
    minFontSize?: number;
  },
): TextBlock {
  const maxLines = options.maxLines ?? ARCH.MAX_LABEL_LINES;
  const minFontSize = options.minFontSize ?? options.fontSize;
  let fontSize = options.fontSize;
  let words = wordsOf(markup, fontSize, options.weight);
  const widest = words.reduce((max, word) => Math.max(max, word.width), 0);
  if (widest > options.maxWidth && fontSize > minFontSize) {
    fontSize = Math.max(minFontSize, Math.floor(fontSize * (options.maxWidth / widest)));
    words = wordsOf(markup, fontSize, options.weight);
  }
  if (words.some((word) => word.width > options.maxWidth)) {
    words = words.flatMap((word) => (word.width > options.maxWidth ? splitWord(word, options.maxWidth, fontSize, options.weight) : [word]));
  }
  const lineHeight = Math.round(options.lineHeight * (fontSize / options.fontSize));
  const space = estimateTextWidth(" ", fontSize, options.weight);
  const lines: Word[][] = [];
  let current: Word[] = [];
  let currentWidth = 0;
  const flush = () => {
    if (current.length) lines.push(current);
    current = [];
    currentWidth = 0;
  };
  for (const word of words) {
    const extra = current.length ? space + word.width : word.width;
    if (current.length && currentWidth + extra > options.maxWidth) flush();
    current.push(word);
    currentWidth += current.length === 1 ? word.width : space + word.width;
    if (word.breakAfter) flush();
  }
  flush();
  if (lines.length > maxLines) {
    const kept = lines.slice(0, maxLines);
    const last = kept[maxLines - 1];
    const ellipsis = estimateTextWidth("…", fontSize);
    const widthOf = (line: Word[]) => line.reduce((sum, word, index) => sum + (index ? space : 0) + word.width, 0);
    // The mark must fit too: drop trailing words until it does, and shorten
    // the one word that has to stay when it alone fills the line.
    while (last.length > 1 && widthOf(last) + space + ellipsis > options.maxWidth) last.pop();
    if (widthOf(last) + space + ellipsis > options.maxWidth) {
      last[0] = splitWord(last[0], Math.max(1, options.maxWidth - space - ellipsis), fontSize, options.weight)[0];
    }
    last.push({ text: "…", style: {}, width: ellipsis, breakAfter: false });
    lines.splice(0, lines.length, ...kept);
  }
  const textLines: TextLine[] = lines.map((lineWords) => {
    const runs: TextRun[] = [];
    let width = 0;
    lineWords.forEach((word, index) => {
      const glue = index === 0 ? "" : " ";
      width += (index === 0 ? 0 : space) + word.width;
      const previous = runs[runs.length - 1];
      if (previous && styleKey(previous) === styleKey(word.style)) {
        previous.text += glue + word.text;
      } else {
        runs.push({ ...word.style, text: glue + word.text });
      }
    });
    return { runs, width };
  });
  return {
    lines: textLines,
    width: textLines.reduce((max, line) => Math.max(max, line.width), 0),
    height: textLines.length * lineHeight,
    fontSize,
    lineHeight,
  };
}

// ── Geometry ──────────────────────────────────────────────────────────────────

export interface Point {
  x: number;
  y: number;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** @public */
export type Side = "top" | "right" | "bottom" | "left";

type NodeKind = "card" | "band" | "pill" | "circle";

interface ChipBox {
  text: TextBlock;
  x: number;
  y: number;
  w: number;
}

export interface LayoutNode extends Rect {
  id: string;
  node: DeckArchitectureNode;
  kind: NodeKind;
  shape: DeckNodeShape;
  emphasis: DeckNodeEmphasis;
  tone: DeckTone | undefined;
  /** A template palette colour, worn in place of `tone` when set. */
  color: DeckPaletteColor | undefined;
  cx: number;
  cy: number;
  /** Corner radius of the shape (half the height for a pill / circle). */
  radius: number;
  /** `frame: false`: the content is drawn alone, no shape behind it, no glow around it. */
  frame: boolean;
  media: "icon" | "logo" | null;
  /** Where the icon tile or logo box sits, relative to the node's origin. */
  mediaBox: Rect | null;
  /**
   * What the media paints, relative to the node's origin: the tile, the bare
   * icon, or the logo as its file's ratio fits it in `mediaBox` (the whole box
   * when the ratio is not known).
   */
  mediaRect: Rect | null;
  /** The box of everything the node paints - media, text, chips - relative to its origin. */
  content: Rect;
  /**
   * Where the node's edges meet it, absolute: its own box, or for a frameless
   * node the box of its content, so an arrow ends on what is drawn rather than
   * on a border nobody sees. The node's box still places it and still keeps
   * the other edges and the labels away.
   */
  anchor: Rect;
  /**
   * Whether an icon is drawn in its tile (a card's above the label, a band's
   * before it) or bare: `icon_tile: false` draws it inline before the
   * label at the icon's own size, so the label stays vertically centred.
   */
  mediaTile: boolean;
  /** How the label and sublabel lines are anchored at `labelBox.x`. */
  textAnchor: "start" | "middle";
  label: TextBlock;
  labelBox: Rect;
  sublabel: TextBlock | null;
  sublabelBox: Rect | null;
  chips: ChipBox[];
  ariaLabel: string;
}

/** One of a zone's chips, absolute: its pill and its text, at the size the row was drawn at. */
interface ZoneChip extends Rect {
  text: TextBlock;
}

export interface LayoutZone extends Rect {
  zone: DeckArchitectureZone;
  /** The label on the title row; `null` for a zone headed by its logo or its chips alone. */
  title: TextBlock | null;
  titleX: number;
  /** The title row's centre line: the icon, the logo, the label and the chips are all centred on it. */
  titleY: number;
  iconX: number;
  /** Where the zone logo is drawn (absolute), its mark meeting the box; `null` without one. */
  logo: Rect | null;
  /** The zone's chips, one row on the title row; empty without any. */
  chips: ZoneChip[];
  /** The box of the chip row, which edges and their labels keep off; `null` without chips. */
  chipsBox: Rect | null;
  /**
   * Canvas px the chip row lacks at its smaller size to clear the logo / label
   * and stay inside the zone's padding; 0 when it fits. The row is drawn whole
   * anyway and the renderer publishes the amount for the layout check.
   */
  chipsOverflow: number;
}

interface EdgeLabel extends Rect {
  text: TextBlock;
}

export interface LayoutEdge {
  key: string;
  edge: DeckArchitectureEdge;
  tone: DeckTone;
  arrows: DeckEdgeArrows;
  dashed: boolean;
  /** SVG path data. */
  d: string;
  points: Point[];
  label: EdgeLabel | null;
}

export interface LegendItemLayout {
  item: DeckArchitectureLegendItem;
  marker: DeckLegendMarker;
  tone: DeckTone;
  x: number;
  y: number;
  text: TextBlock;
  textX: number;
}

export interface ArchitectureLayout {
  width: number;
  height: number;
  /**
   * The height at density 1 - the drawing's own, before any compaction. The
   * renderer sizes the stage from it, so the box the room is read from never
   * depends on the compaction the room asked for.
   */
  naturalHeight: number;
  /** The vertical density the fit pass settled on: 1 = the natural layout. */
  density: number;
  nodes: LayoutNode[];
  zones: LayoutZone[];
  edges: LayoutEdge[];
  legend: LegendItemLayout[];
  /** Node ids -> ids of the nodes an edge ties them to (for the hover state). */
  neighbours: Map<string, Set<string>>;
}

/** The box the drawing has, in canvas px; `width` optional (then the drawing is taken as drawn at 1:1). */
interface ArchitectureRoom {
  width?: number;
  height: number;
}

/** The stored geometry of the deck's logos, by file id as the spec spells it (the deck's `asset_meta`). */
export type ArchitectureAssets = Record<string, DeckAssetMeta>;

const span = (node: DeckArchitectureNode) => Math.max(1, node.col_span ?? 1);
/** The column grid of one layout: `ARCH.COL_W` / `ARCH.COL_GAP` unless a sized node logo asks for more width. */
interface Grid {
  colW: number;
  colGap: number;
}
const DEFAULT_GRID: Grid = { colW: ARCH.COL_W, colGap: ARCH.COL_GAP };
const spanWidth = (cols: number, grid: Grid = DEFAULT_GRID) => cols * grid.colW + (cols - 1) * grid.colGap;
/**
 * Side margin of a sized card logo: the mark spans the card's width, not its
 * text column, but keeps a card padding's worth of air to the border.
 */
export const SIZED_LOGO_INSET = 12;
/** The widest a column grows for a sized wordmark when the block has no room to fit (drawn 1:1). */
const SIZED_COL_W_MAX = 440;
/**
 * The gap between the columns of a widened grid: a sized wordmark takes its
 * width from the gaps first (48 still carries an arrow and its head): a 5:1
 * wordmark on a five-column row then reads at lg on a 16:9 body at a fit of
 * about 0.85.
 */
const SIZED_COL_GAP = 48;
/**
 * The smallest fit scale a widened grid may cost: a sublabel (`ARCH.SUB_FS`)
 * stays at 16 px or more on the slide. A wordmark the capped column cannot
 * draw at its step is drawn smaller and published (`data-logo-step`), so the
 * layout check names it rather than the whole drawing shrinking for it.
 */
export const SIZED_FIT_MIN = 16 / 19;

const plain = (markup: string | undefined): string => markupToPlainText(markup).replace(/\s+/g, " ").trim();

/** Width over height of a logo file drawn whole (a node's logo meets its box uncropped); `null` when unknown. */
const fileRatio = (meta: DeckAssetMeta | undefined): number | null => (meta?.w && meta?.h ? meta.w / meta.h : null);

/** Width over height of a zone logo's mark - trimmed to its ink when the geometry has a box, the file otherwise. */
function markRatio(meta: DeckAssetMeta | undefined): number {
  const natural = meta?.w && meta?.h ? { width: meta.w, height: meta.h } : null;
  return logoShape(meta, natural)?.ratio ?? LOGO_BALANCE_REFERENCE_RATIO;
}

/** The rect a picture of `ratio` paints when it meets `box` (`preserveAspectRatio` meet), at its start or centred. */
function fitPicture(box: Rect, ratio: number | null, align: "start" | "middle"): Rect {
  if (!ratio || !(ratio > 0)) return box;
  const w = Math.min(box.w, box.h * ratio);
  const h = Math.min(box.h, box.w / ratio);
  return { x: align === "start" ? box.x : box.x + (box.w - w) / 2, y: box.y + (box.h - h) / 2, w, h };
}

/** How far a line's glyphs reach above and below its centre line, in em: what a text block paints, not its line boxes. */
const GLYPH_HALF_EM = 0.6;

/** The box a text block paints drawn from `top`, its lines starting at `x` or centred on it - as `SvgText` draws it. */
function textRect(block: TextBlock, x: number, top: number, anchor: "start" | "middle"): Rect {
  const half = block.fontSize * GLYPH_HALF_EM;
  const first = top + block.lineHeight / 2 - half;
  const last = top + (block.lines.length - 0.5) * block.lineHeight + half;
  return { x: anchor === "start" ? x : x - block.width / 2, y: first, w: block.width, h: Math.max(0, last - first) };
}

function unionRects(rects: readonly (Rect | null | undefined)[]): Rect | null {
  const present = rects.filter((rect): rect is Rect => !!rect && rect.w > 0 && rect.h > 0);
  if (!present.length) return null;
  const x = Math.min(...present.map((r) => r.x));
  const y = Math.min(...present.map((r) => r.y));
  const right = Math.max(...present.map((r) => r.x + r.w));
  const bottom = Math.max(...present.map((r) => r.y + r.h));
  return { x, y, w: right - x, h: bottom - y };
}

// ── Vertical density ──────────────────────────────────────────────────────────

/**
 * The tightest the fit pass packs the rows: the lengths below are
 * multiplied by the density, the type is not. Below this the gaps no longer
 * hold an edge label, so a drawing still too tall at the floor is left to the
 * slide's shrink-to-fit, which the layout check then reports.
 */
export const DENSITY_MIN = 0.6;

/** Gap between a card's media and its label, chips and the row above them, at density 1. */
const CARD_GAP = 14;
/** Gap between the drawing and its legend, at density 1. */
const LEGEND_GAP_Y = 24;
/** What a zone's title band gives up at the floor: the title's own line never shrinks. */
const ZONE_HEAD_SLACK = 16;
/** Vertical room a circle keeps around its text, at any density. */
const CIRCLE_PAD_Y = 20;

/** The vertical metrics at density `v`: the lengths the fit pass compacts. */
interface Metrics extends Grid {
  v: number;
  /** What a sized node logo is raised by so it still reads at its step once the block is fitted to its room (1: no room). */
  logoBoost: number;
  padY: number;
  rowGap: number;
  zoneHead: number;
  cardPadY: number;
  minCardH: number;
  minBandH: number;
  cardGap: number;
  circleD: number;
  coreD: number;
  emptyRowH: number;
  legendGap: number;
}

function metricsAt(v: number, grid: Grid = DEFAULT_GRID, logoBoost = 1): Metrics {
  const scale = (n: number) => Math.round(n * v);
  return {
    ...grid,
    v,
    logoBoost,
    padY: scale(ARCH.PAD),
    rowGap: scale(ARCH.ROW_GAP),
    zoneHead: Math.round(ARCH.ZONE_HEAD - (1 - v) * ZONE_HEAD_SLACK),
    cardPadY: scale(ARCH.CARD_PAD_Y),
    minCardH: scale(ARCH.MIN_CARD_H),
    minBandH: scale(ARCH.MIN_BAND_H),
    cardGap: scale(CARD_GAP),
    circleD: scale(ARCH.CIRCLE_D),
    coreD: scale(ARCH.CORE_D),
    emptyRowH: scale(ARCH.EMPTY_ROW_H),
    legendGap: scale(LEGEND_GAP_Y),
  };
}

// ── Nodes ─────────────────────────────────────────────────────────────────────

function measureChips(chips: readonly string[] | undefined, maxWidth: number): { boxes: ChipBox[]; w: number; h: number } {
  const labels = (chips ?? []).filter(Boolean);
  if (!labels.length) return { boxes: [], w: 0, h: 0 };
  const boxes: ChipBox[] = [];
  let x = 0;
  let y = 0;
  let rowWidth = 0;
  let widest = 0;
  for (const label of labels) {
    const text = layoutText(label, {
      fontSize: ARCH.CHIP_FS,
      lineHeight: ARCH.CHIP_H,
      weight: 500,
      maxWidth: maxWidth - 2 * ARCH.CHIP_PAD,
      maxLines: 1,
    });
    const w = Math.min(maxWidth, text.width + 2 * ARCH.CHIP_PAD);
    if (x > 0 && x + w > maxWidth) {
      widest = Math.max(widest, rowWidth);
      x = 0;
      y += ARCH.CHIP_H + ARCH.CHIP_GAP;
    }
    boxes.push({ text, x, y, w });
    rowWidth = x + w;
    x += w + ARCH.CHIP_GAP;
  }
  widest = Math.max(widest, rowWidth);
  // Centre every row.
  const rows = new Map<number, ChipBox[]>();
  for (const box of boxes) {
    const row = rows.get(box.y) ?? [];
    row.push(box);
    rows.set(box.y, row);
  }
  for (const row of rows.values()) {
    const last = row[row.length - 1];
    const width = last.x + last.w;
    const shift = (widest - width) / 2;
    for (const box of row) box.x += shift;
  }
  return { boxes, w: widest, h: y + ARCH.CHIP_H };
}

function nodeKind(node: DeckArchitectureNode): NodeKind {
  const shape = node.shape ?? "card";
  if (shape === "card" && span(node) > 1) return "band";
  return shape;
}

type MeasuredNode = Omit<LayoutNode, "x" | "y" | "cx" | "cy" | "anchor">;
/** A node's shape and where its pieces go, before what they paint is known. */
type ShapedNode = Omit<MeasuredNode, "frame" | "mediaRect" | "content">;

/** Size of a circle's bare icon; every other bare icon is `ARCH.ICON`. */
const CIRCLE_ICON = 34;

/**
 * A node logo sized by `logo_height` (a card's or a band's): the mark is cropped
 * to its ink and drawn at the step's height, raised for a squarer, lighter mark
 * by the optical balance of a `logo_grid` (up to `LOGO_BALANCE_MAX_BOOST`).
 * `null` when the node does not set it: the logo keeps the fixed `ARCH.LOGO_H`
 * box, fitted on the whole file, exactly as before.
 */
export function nodeLogoSize(
  node: DeckArchitectureNode,
  assets: ArchitectureAssets | undefined,
  boost = 1,
): { h: number; ratio: number } | null {
  if (!node.logo_file_id || !node.logo_height) return null;
  const meta = assets?.[node.logo_file_id];
  const ratio = markRatio(meta);
  const shape = logoShape(meta, meta?.w && meta?.h ? { width: meta.w, height: meta.h } : null) ?? { ratio };
  return { h: Math.round(logoHeightPx(shape, LOGO_HEIGHT_PX[node.logo_height], true) * boost * 10) / 10, ratio };
}

/** The height a sized card logo is drawn at in a column `colW` wide: its step, or less when the column caps a wide mark. */
function cardLogoDrawnH(size: { h: number; ratio: number }, colW: number): number {
  return Math.min(size.h, (colW - 2 * SIZED_LOGO_INSET) / size.ratio);
}

/** What a node's media paints inside its `mediaBox` - the rules `NodeMedia` draws by. */
function mediaRectOf(shaped: ShapedNode, m: Metrics, assets: ArchitectureAssets | undefined): Rect | null {
  const box = shaped.mediaBox;
  if (!box || !shaped.media) return null;
  if (shaped.media === "logo") {
    const meta = shaped.node.logo_file_id ? assets?.[shaped.node.logo_file_id] : undefined;
    const sized = shaped.kind === "card" || shaped.kind === "band" ? nodeLogoSize(shaped.node, assets, m.logoBoost) : null;
    if (sized && shaped.kind === "card") {
      // Its own height, centred in the row's shared media box: the balance between marks is kept.
      const h = Math.min(box.h, cardLogoDrawnH(sized, box.w + 2 * SIZED_LOGO_INSET));
      const w = h * sized.ratio;
      return { x: box.x + (box.w - w) / 2, y: box.y + (box.h - h) / 2, w, h };
    }
    if (sized) return fitPicture(box, sized.ratio, shaped.kind === "band" ? "start" : "middle");
    // The fixed box: on a row that mixes logo and icon cards the box is the
    // row's shared media height, the unsized mark keeps its `ARCH.LOGO_H`.
    const h = Math.min(box.h, ARCH.LOGO_H);
    const fixed = { x: box.x, y: box.y + (box.h - h) / 2, w: box.w, h };
    return fitPicture(fixed, fileRatio(meta), shaped.kind === "band" ? "start" : "middle");
  }
  if (shaped.mediaTile) {
    // A square tile as tall as the media box: `ARCH.ICON_TILE`, or the shared
    // media height of a row that mixes logo and icon cards.
    const size = box.h;
    const x = shaped.kind === "band" ? box.x : box.x + (box.w - size) / 2;
    return { x, y: box.y, w: size, h: size };
  }
  const size = shaped.kind === "circle" ? CIRCLE_ICON : ARCH.ICON;
  return { x: box.x + (box.w - size) / 2, y: box.y + (box.h - size) / 2, w: size, h: size };
}

/** A node measured: its shape, plus what it paints - the content a frameless node is drawn as and met at. */
function measureNode(
  node: DeckArchitectureNode,
  m: Metrics,
  assets: ArchitectureAssets | undefined,
  rowLogoH?: number,
  rowMediaH?: number,
  rowContentH?: number,
): MeasuredNode {
  const shaped = measureShape(node, m, assets, rowLogoH, rowMediaH, rowContentH);
  const mediaRect = mediaRectOf(shaped, m, assets);
  // Placed as `NodeView` places the text: a pill's lines are centred on its height.
  const text = (block: TextBlock, box: Rect) =>
    textRect(
      block,
      shaped.textAnchor === "start" ? box.x : box.x + box.w / 2,
      shaped.kind === "pill" ? (shaped.h - block.lineHeight) / 2 : box.y,
      shaped.textAnchor,
    );
  const content = unionRects([
    mediaRect,
    text(shaped.label, shaped.labelBox),
    shaped.sublabel && shaped.sublabelBox ? text(shaped.sublabel, shaped.sublabelBox) : null,
    ...shaped.chips.map((chip) => ({ x: chip.x, y: chip.y, w: chip.w, h: ARCH.CHIP_H })),
  ]) ?? { x: 0, y: 0, w: shaped.w, h: shaped.h };
  return { ...shaped, frame: node.frame !== false, mediaRect, content };
}

/** A circle's text, wrapped to the chord a diameter `d` leaves it. */
function circleText(node: DeckArchitectureNode, d: number): { textW: number; label: TextBlock; sublabel: TextBlock | null } {
  const textW = d * 0.74;
  const label = layoutText(node.label, {
    fontSize: 24,
    lineHeight: 29,
    weight: 600,
    maxWidth: textW,
    maxLines: 2,
    minFontSize: ARCH.LABEL_MIN_FS,
  });
  const sublabel = node.sublabel
    ? layoutText(node.sublabel, { fontSize: 17, lineHeight: 21, weight: 400, maxWidth: textW, maxLines: 2 })
    : null;
  return { textW, label, sublabel };
}

function measureShape(
  node: DeckArchitectureNode,
  m: Metrics,
  assets?: ArchitectureAssets,
  rowLogoH?: number,
  rowMediaH?: number,
  rowContentH?: number,
): ShapedNode {
  const kind = nodeKind(node);
  // A card's or a band's logo at its `logo_height`; unset, the fixed box. The
  // sized cards of one row share the row's tallest media box (`rowLogoH`), so
  // they are one height and their labels sit on one baseline. On a row that
  // mixes logo and icon cards every card's media box is `rowMediaH` (see
  // `mixedRowMediaH`): the icon tile and the logo box are one size.
  const logoSize = kind === "card" || kind === "band" ? nodeLogoSize(node, assets, m.logoBoost) : null;
  const shared = kind === "card" ? (rowMediaH ?? 0) : 0;
  const logoH = !logoSize
    ? Math.max(ARCH.LOGO_H, shared)
    : kind === "card"
      ? Math.max(rowLogoH ?? 0, shared, cardLogoDrawnH(logoSize, m.colW))
      : logoSize.h;
  const shape = node.shape ?? "card";
  const emphasis = node.emphasis ?? "normal";
  const media: LayoutNode["media"] = node.logo_file_id ? "logo" : node.icon ? "icon" : null;
  // The tile is a card's and a band's; a pill and a circle draw the icon bare.
  const mediaTile = media === "icon" && (kind === "card" || kind === "band") && node.icon_tile !== false;
  const cellW = spanWidth(span(node), m);
  const aria = [plain(node.label), plain(node.sublabel)].filter(Boolean).join(" - ");
  const base = { id: node.id, node, kind, shape, emphasis, tone: node.tone, color: node.color, media, mediaTile, ariaLabel: aria };

  if (kind === "circle") {
    // The diameter the density asks for, grown to what its text needs: the
    // text is wrapped to the asked chord first, so it always fits the circle
    // it ends up in; when the circle had to grow, the text is re-wrapped to
    // the grown chord and kept when it still fits (fewer lines, same circle).
    const asked = emphasis === "core" ? m.coreD : m.circleD;
    const mediaH = media === "logo" ? 40 : media === "icon" ? 34 : 0;
    const needed = (text: ReturnType<typeof circleText>) =>
      mediaH + (mediaH ? 10 : 0) + text.label.height + (text.sublabel ? 4 + text.sublabel.height : 0);
    let text = circleText(node, asked);
    const d = Math.max(asked, needed(text) + 2 * CIRCLE_PAD_Y);
    if (d > asked) {
      const wider = circleText(node, d);
      if (needed(wider) + 2 * CIRCLE_PAD_Y <= d) text = wider;
    }
    const { textW, label, sublabel } = text;
    const contentH = needed(text);
    let y = (d - contentH) / 2;
    const mediaBox = media ? { x: (d - textW) / 2, y, w: textW, h: mediaH } : null;
    y += mediaH ? mediaH + 10 : 0;
    const labelBox = { x: (d - textW) / 2, y, w: textW, h: label.height };
    y += label.height;
    const sublabelBox = sublabel ? { x: (d - textW) / 2, y: y + 4, w: textW, h: sublabel.height } : null;
    return { ...base, w: d, h: d, radius: d / 2, textAnchor: "middle", mediaBox, label, labelBox, sublabel, sublabelBox, chips: [] };
  }

  if (kind === "pill") {
    const iconW = media ? ARCH.ICON + 12 : 0;
    const maxText = cellW - 2 * ARCH.PILL_PAD_X - iconW;
    const label = layoutText(node.label, {
      fontSize: 24,
      lineHeight: ARCH.PILL_H,
      weight: 600,
      maxWidth: node.sublabel ? maxText * 0.6 : maxText,
      maxLines: 1,
      minFontSize: ARCH.LABEL_MIN_FS,
    });
    const sublabel = node.sublabel
      ? layoutText(node.sublabel, {
          fontSize: ARCH.SUB_FS,
          lineHeight: ARCH.PILL_H,
          weight: 400,
          maxWidth: maxText - label.width - 18,
          maxLines: 1,
        })
      : null;
    const textW = label.width + (sublabel ? 18 + sublabel.width : 0);
    const w = Math.min(cellW, Math.max(160, 2 * ARCH.PILL_PAD_X + iconW + textW));
    const h = ARCH.PILL_H;
    let x = (w - (iconW + textW)) / 2;
    const mediaBox = media ? { x, y: (h - ARCH.ICON) / 2, w: ARCH.ICON, h: ARCH.ICON } : null;
    x += iconW;
    const labelBox = { x, y: 0, w: label.width, h };
    x += label.width + 18;
    const sublabelBox = sublabel ? { x, y: 0, w: sublabel.width, h } : null;
    return { ...base, w, h, radius: h / 2, textAnchor: "start", mediaBox, label, labelBox, sublabel, sublabelBox, chips: [] };
  }

  if (kind === "band") {
    const w = cellW;
    const innerW = w - 2 * ARCH.CARD_PAD_X;
    const bandLogoW = logoSize ? Math.min(Math.max(150, logoSize.h * logoSize.ratio), innerW * 0.45) : 150;
    const mediaW = media === "logo" ? bandLogoW : media === "icon" ? (mediaTile ? ARCH.ICON_TILE : ARCH.ICON) : 0;
    const mediaGap = media ? (mediaTile || media === "logo" ? 18 : 12) : 0;
    const chipsMeasured = measureChips(node.chips, innerW * 0.55);
    const chipsInline = chipsMeasured.boxes.length > 0 && chipsMeasured.h <= ARCH.CHIP_H;
    const textMax = innerW - mediaW - mediaGap - (chipsInline ? chipsMeasured.w + 24 : 0);
    const label = layoutText(node.label, {
      fontSize: ARCH.LABEL_FS,
      lineHeight: ARCH.LABEL_LH,
      weight: 600,
      maxWidth: Math.max(120, textMax),
      maxLines: 2,
      minFontSize: ARCH.LABEL_MIN_FS,
    });
    const sublabel = node.sublabel
      ? layoutText(node.sublabel, {
          fontSize: ARCH.SUB_FS,
          lineHeight: ARCH.SUB_LH,
          weight: 400,
          maxWidth: Math.max(120, textMax),
          maxLines: 2,
        })
      : null;
    const textH = label.height + (sublabel ? 4 + sublabel.height : 0);
    const mediaH = media === "icon" ? (mediaTile ? ARCH.ICON_TILE : ARCH.ICON) : media === "logo" ? logoH : 0;
    const rowH = Math.max(mediaH, textH, chipsInline ? ARCH.CHIP_H : 0);
    const chipsBelowH = chipsMeasured.boxes.length && !chipsInline ? m.cardGap + chipsMeasured.h : 0;
    const h = Math.max(m.minBandH, 2 * m.cardPadY + rowH + chipsBelowH);
    const top = m.cardPadY + (h - 2 * m.cardPadY - chipsBelowH - rowH) / 2;
    let x = ARCH.CARD_PAD_X;
    const mediaBox = media ? { x, y: top + (rowH - mediaH) / 2, w: mediaW, h: mediaH } : null;
    x += mediaW + mediaGap;
    const labelBox = { x, y: top + (rowH - textH) / 2, w: textMax, h: label.height };
    const sublabelBox = sublabel ? { x, y: labelBox.y + label.height + 4, w: textMax, h: sublabel.height } : null;
    const chips = chipsMeasured.boxes.map((box) =>
      chipsInline
        ? { ...box, x: w - ARCH.CARD_PAD_X - chipsMeasured.w + box.x, y: top + (rowH - ARCH.CHIP_H) / 2 }
        : { ...box, x: ARCH.CARD_PAD_X + (innerW - chipsMeasured.w) / 2 + box.x, y: top + rowH + m.cardGap + box.y },
    );
    return { ...base, w, h, radius: 18, textAnchor: "start", mediaBox, label, labelBox, sublabel, sublabelBox, chips };
  }

  // card
  const w = m.colW;
  const innerW = w - 2 * ARCH.CARD_PAD_X;
  const chipsMeasured = measureChips(node.chips, innerW);
  const chipsH = chipsMeasured.h ? m.cardGap + chipsMeasured.h : 0;

  if (media === "icon" && !mediaTile) {
    // Inline icon: the icon before the label on one vertical centre,
    // the pair centred in the card, the text lines starting after the icon -
    // nothing above the label pushes it down, so two such cards on one row
    // keep their labels on the row's centre whatever their line counts.
    const iconGap = 12;
    const textMax = innerW - ARCH.ICON - iconGap;
    const label = layoutText(node.label, {
      fontSize: ARCH.LABEL_FS,
      lineHeight: ARCH.LABEL_LH,
      weight: 600,
      maxWidth: textMax,
      minFontSize: ARCH.LABEL_MIN_FS,
    });
    const sublabel = node.sublabel
      ? layoutText(node.sublabel, { fontSize: ARCH.SUB_FS, lineHeight: ARCH.SUB_LH, weight: 400, maxWidth: textMax, maxLines: 3 })
      : null;
    const textH = label.height + (sublabel ? 4 + sublabel.height : 0);
    const rowH = Math.max(ARCH.ICON, textH);
    const contentH = rowH + chipsH;
    // On a mixed logo / icon row it is as tall as the row's other cards.
    const h = Math.max(m.minCardH, 2 * m.cardPadY + Math.max(contentH, rowContentH ?? 0));
    const top = (h - contentH) / 2;
    const textW = Math.max(label.width, sublabel?.width ?? 0);
    const groupX = ARCH.CARD_PAD_X + (innerW - (ARCH.ICON + iconGap + textW)) / 2;
    const mediaBox = { x: groupX, y: top + (rowH - ARCH.ICON) / 2, w: ARCH.ICON, h: ARCH.ICON };
    const textX = groupX + ARCH.ICON + iconGap;
    const labelBox = { x: textX, y: top + (rowH - textH) / 2, w: textW, h: label.height };
    const sublabelBox = sublabel ? { x: textX, y: labelBox.y + label.height + 4, w: textW, h: sublabel.height } : null;
    const chips = chipsMeasured.boxes.map((box) => ({
      ...box,
      x: ARCH.CARD_PAD_X + (innerW - chipsMeasured.w) / 2 + box.x,
      y: top + rowH + m.cardGap + box.y,
    }));
    return { ...base, w, h, radius: 18, textAnchor: "start", mediaBox, label, labelBox, sublabel, sublabelBox, chips };
  }

  const label = layoutText(node.label, {
    fontSize: ARCH.LABEL_FS,
    lineHeight: ARCH.LABEL_LH,
    weight: 600,
    maxWidth: innerW,
    minFontSize: ARCH.LABEL_MIN_FS,
  });
  const sublabel = node.sublabel
    ? layoutText(node.sublabel, {
        fontSize: ARCH.SUB_FS,
        lineHeight: ARCH.SUB_LH,
        weight: 400,
        maxWidth: innerW,
        maxLines: 3,
      })
    : null;
  const mediaH = media === "icon" ? Math.max(ARCH.ICON_TILE, shared) : media === "logo" ? logoH : 0;
  const contentH = mediaH + (mediaH ? m.cardGap : 0) + label.height + (sublabel ? 4 + sublabel.height : 0) + chipsH;
  // On a mixed row every card is as tall as the row's tallest content and
  // starts its content at the same y: one card height, one label baseline.
  const spanH = Math.max(contentH, rowContentH ?? 0);
  const h = Math.max(m.minCardH, 2 * m.cardPadY + spanH);
  let y = (h - spanH) / 2;
  const mediaBox = !media
    ? null
    : logoSize
      ? { x: SIZED_LOGO_INSET, y, w: w - 2 * SIZED_LOGO_INSET, h: mediaH }
      : { x: ARCH.CARD_PAD_X, y, w: innerW, h: mediaH };
  y += mediaH ? mediaH + m.cardGap : 0;
  const labelBox = { x: ARCH.CARD_PAD_X, y, w: innerW, h: label.height };
  y += label.height;
  const sublabelBox = sublabel ? { x: ARCH.CARD_PAD_X, y: y + 4, w: innerW, h: sublabel.height } : null;
  y += sublabel ? 4 + sublabel.height : 0;
  const chipsTop = y + m.cardGap;
  const chips = chipsMeasured.boxes.map((box) => ({
    ...box,
    x: ARCH.CARD_PAD_X + (innerW - chipsMeasured.w) / 2 + box.x,
    y: chipsTop + box.y,
  }));
  return { ...base, w, h, radius: 18, textAnchor: "middle", mediaBox, label, labelBox, sublabel, sublabelBox, chips };
}

// ── Edge routing ──────────────────────────────────────────────────────────────

const OUT: Record<Side, Point> = {
  top: { x: 0, y: -1 },
  right: { x: 1, y: 0 },
  bottom: { x: 0, y: 1 },
  left: { x: -1, y: 0 },
};

const OPPOSITE: Record<Side, Side> = { top: "bottom", right: "left", bottom: "top", left: "right" };
const SIDES: readonly Side[] = ["top", "right", "bottom", "left"];
/** Does an edge leaving (or entering) this side run horizontally there? */
const runsHorizontally = (side: Side) => side === "left" || side === "right";

/**
 * What the router needs of an edge end: a node, or a zone's border met as one
 * (`zoneEnd`) - which carries its zone and the side it was built on.
 */
type RouteEnd = Pick<LayoutNode, "id" | "x" | "y" | "w" | "h" | "cx" | "cy" | "anchor" | "frame" | "kind" | "radius" | "emphasis"> & {
  zone?: LayoutZone;
  side?: Side;
};

/**
 * The shapes an orthogonal edge can take. `h` / `v` run straight between
 * aligned nodes (a lane difference becomes a Z), `hv` / `vh` are the two
 * elbows between diagonal nodes, `detour-h` / `detour-v` go around a row or a
 * column by a corridor outside both nodes, and `vhv` / `hvh` run
 * through a corridor BETWEEN the two nodes - the row gap or the column gap the
 * grid leaves free - so a diagonal pair whose two elbows both run through a
 * third node still has a route of its own. `via` is any of them and
 * more, spelled as the corridors it runs along: the one shape of an edge with
 * a pinned side or a zone end.
 */
type CandidateKind = "h" | "v" | "hv" | "vh" | "detour-h" | "detour-v" | "vhv" | "hvh" | "via";

interface Candidate {
  kind: CandidateKind;
  sourceSide: Side;
  targetSide: Side;
  /** Corridor coordinate: a y for a horizontal corridor, an x for a vertical one. */
  corridor?: number;
  /** The x (vertical) or y (horizontal) both anchors share on a direct edge. */
  align?: number;
  /** A `via` route's corridors in order: an x while it runs horizontally, a y while it runs vertically. */
  vias?: number[];
  /** A `via` route's ends: a zone end is built per side, so the candidate carries the one it meets. */
  sourceEnd?: RouteEnd;
  targetEnd?: RouteEnd;
}

/** Which axis a candidate's corridor is measured on, if it has one. */
function corridorAxis(kind: CandidateKind): "x" | "y" | null {
  if (kind === "detour-h" || kind === "vhv") return "y";
  if (kind === "detour-v" || kind === "hvh") return "x";
  return null;
}

/** The sides a straight tie meets its nodes on; `null` for a circle end (radial). */
interface StraightSides {
  sourceSide: Side | null;
  targetSide: Side | null;
}

/** The zone an edge end names, with its index in the block. */
interface ZoneRef {
  zone: LayoutZone;
  index: number;
}

interface Route {
  edge: DeckArchitectureEdge;
  index: number;
  source: RouteEnd;
  target: RouteEnd;
  /** The zone an end names instead of a node; `null` for a node end. */
  sourceZone: ZoneRef | null;
  targetZone: ZoneRef | null;
  mode: "straight" | "curve" | "ortho";
  candidate: Candidate | null;
  /** A straight tie's sides, so its rectangular ends take lanes like any other anchor. */
  straight: StraightSides | null;
  points: Point[];
}

interface AnchorRef {
  route: Route;
  end: "source" | "target";
  side: Side;
  /**
   * Along the side's axis: where the path comes from (its point next to the
   * anchor - a tie arriving from the left takes a lane left of an elbow coming
   * straight down), then the far end's coordinate, then the travel side.
   */
  sortKey: [number, number, number];
  offset: number;
}

function segmentsOf(points: Point[]): [Point, Point][] {
  const out: [Point, Point][] = [];
  for (let i = 0; i + 1 < points.length; i += 1) out.push([points[i], points[i + 1]]);
  return out;
}

function inflate(rect: Rect, by: number): Rect {
  return { x: rect.x - by, y: rect.y - by, w: rect.w + 2 * by, h: rect.h + 2 * by };
}

/** Liang–Barsky: does the segment enter the rectangle's interior? */
export function segmentIntersectsRect(a: Point, b: Point, rect: Rect): boolean {
  let t0 = 0;
  let t1 = 1;
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const clip = (p: number, q: number): boolean => {
    if (p === 0) return q >= 0;
    const r = q / p;
    if (p < 0) {
      if (r > t1) return false;
      if (r > t0) t0 = r;
    } else {
      if (r < t0) return false;
      if (r < t1) t1 = r;
    }
    return true;
  };
  const inside =
    clip(-dx, a.x - rect.x) &&
    clip(dx, rect.x + rect.w - a.x) &&
    clip(-dy, a.y - rect.y) &&
    clip(dy, rect.y + rect.h - a.y);
  return inside && t1 - t0 > 1e-6;
}

const cross = (o: Point, a: Point, b: Point) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);

/** Proper crossing only: shared endpoints and collinear overlaps do not count. */
export function segmentsCross(a: Point, b: Point, c: Point, d: Point): boolean {
  const d1 = cross(c, d, a);
  const d2 = cross(c, d, b);
  const d3 = cross(a, b, c);
  const d4 = cross(a, b, d);
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}

function length(points: Point[]): number {
  let total = 0;
  for (const [a, b] of segmentsOf(points)) total += Math.hypot(b.x - a.x, b.y - a.y);
  return total;
}

/**
 * Two axis-aligned segments on the same line with a common run: what an edge
 * routed along a straight tie would draw over it. Lanes part the edges that
 * share a node side, never a straight tie, so the scorer has to.
 */
function segmentsOverlapCollinear(a: Point, b: Point, c: Point, d: Point): boolean {
  const vertical = Math.abs(a.x - b.x) < 0.5 && Math.abs(c.x - d.x) < 0.5;
  const horizontal = Math.abs(a.y - b.y) < 0.5 && Math.abs(c.y - d.y) < 0.5;
  if (vertical) return Math.abs(a.x - c.x) < 1 && overlaps(Math.min(a.y, b.y), Math.max(a.y, b.y), Math.min(c.y, d.y), Math.max(c.y, d.y));
  if (horizontal) return Math.abs(a.y - c.y) < 1 && overlaps(Math.min(a.x, b.x), Math.max(a.x, b.x), Math.min(c.x, d.x), Math.max(c.x, d.x));
  return false;
}

/**
 * Where a straight tie meets its two nodes. A rectangle takes the
 * CENTRE of the side facing the other end - the top edge centre of a card
 * under a core, the left edge centre of one beside it - so a fan of ties from
 * a core to the cards of the row below lands on three like points and reads
 * symmetric; the centre-to-centre ray landed each on wherever it crossed the
 * card's edge, near a corner for the outer cards. A circle has no side: it is
 * left radially toward the far anchor, so the line stays straight.
 */
function straightSides(source: RouteEnd, target: RouteEnd): StraightSides {
  const facing = (node: RouteEnd, other: RouteEnd): Side | null => {
    if (node.side) return node.side;
    if (node.kind === "circle" && node.frame) return null;
    if (!overlaps(node.y, node.y + node.h, other.y, other.y + other.h)) return other.cy > node.cy ? "bottom" : "top";
    return other.cx > node.cx ? "right" : "left";
  };
  return { sourceSide: facing(source, target), targetSide: facing(target, source) };
}

/**
 * The two ends of a straight tie, each rectangular end `offset` along its
 * side (its lane) - from the line both ends share when `align` names one (an
 * aligned pair), so the tie is level even between two anchors whose
 * centres differ.
 */
function straightPoints(
  source: RouteEnd,
  target: RouteEnd,
  sides: StraightSides,
  sOff: number,
  tOff: number,
  align: number | null = null,
): Point[] {
  const onSource = sides.sourceSide ? sidePoint(source, sides.sourceSide, alignShift(source, sides.sourceSide, align) + sOff) : null;
  const onTarget = sides.targetSide ? sidePoint(target, sides.targetSide, alignShift(target, sides.targetSide, align) + tOff) : null;
  return [
    onSource ?? boundaryToward(source, onTarget ?? { x: target.cx, y: target.cy }),
    onTarget ?? boundaryToward(target, onSource ?? { x: source.cx, y: source.cy }),
  ];
}

/** The centre of what a node's edges meet: its shape's, or a frameless node's content's. */
const anchorCx = (node: RouteEnd) => node.anchor.x + node.anchor.w / 2;
const anchorCy = (node: RouteEnd) => node.anchor.y + node.anchor.h / 2;

/** The offset along `side` that puts an anchor on the shared line `align`; 0 without one. */
const alignShift = (node: RouteEnd, side: Side, align: number | null) =>
  align === null ? 0 : align - (runsHorizontally(side) ? anchorCy(node) : anchorCx(node));

/** Where a ray from the node's centre toward `toward` leaves its shape (a frameless node's content box). */
function boundaryToward(node: RouteEnd, toward: Point): Point {
  const cx = anchorCx(node);
  const cy = anchorCy(node);
  const dx = toward.x - cx;
  const dy = toward.y - cy;
  const dist = Math.hypot(dx, dy) || 1;
  const ux = dx / dist;
  const uy = dy / dist;
  if (node.kind === "circle" && node.frame) return { x: node.cx + ux * node.radius, y: node.cy + uy * node.radius };
  const tx = ux === 0 ? Infinity : node.anchor.w / 2 / Math.abs(ux);
  const ty = uy === 0 ? Infinity : node.anchor.h / 2 / Math.abs(uy);
  const t = Math.min(tx, ty);
  return { x: cx + ux * t, y: cy + uy * t };
}

/**
 * The point on a node side, `offset` along the side from its centre. A
 * frameless node is met on its content box, a plain rectangle whatever its
 * shape: nothing is drawn around the content for a lane to follow.
 */
function sidePoint(node: RouteEnd, side: Side, offset: number): Point {
  if (!node.frame) {
    const a = node.anchor;
    const along = (half: number) => Math.max(-half, Math.min(half, offset));
    const limitX = Math.max(0, a.w / 2 - 6);
    const limitY = Math.max(0, a.h / 2 - 6);
    switch (side) {
      case "top":
        return { x: anchorCx(node) + along(limitX), y: a.y };
      case "bottom":
        return { x: anchorCx(node) + along(limitX), y: a.y + a.h };
      case "left":
        return { x: a.x, y: anchorCy(node) + along(limitY) };
      default:
        return { x: a.x + a.w, y: anchorCy(node) + along(limitY) };
    }
  }
  if (node.kind === "circle") {
    const r = node.radius;
    const clamped = Math.max(-r * 0.7, Math.min(r * 0.7, offset));
    const along = Math.sqrt(Math.max(0, r * r - clamped * clamped));
    switch (side) {
      case "top":
        return { x: node.cx + clamped, y: node.cy - along };
      case "bottom":
        return { x: node.cx + clamped, y: node.cy + along };
      case "left":
        return { x: node.cx - along, y: node.cy + clamped };
      default:
        return { x: node.cx + along, y: node.cy + clamped };
    }
  }
  const limitX = Math.max(0, node.w / 2 - node.radius - 6);
  if (node.kind === "pill" && (side === "left" || side === "right")) {
    // A pill's short sides are semicircular caps with no flat run to spread
    // lanes along, so the anchors sit on the cap like on a circle: a lane
    // offset moves the point up or down the arc instead of collapsing onto
    // the centre, which is what keeps an exchange between two side-by-side
    // pills as two lines.
    const r = node.radius;
    const clamped = Math.max(-r * 0.7, Math.min(r * 0.7, offset));
    const along = Math.sqrt(Math.max(0, r * r - clamped * clamped));
    const capX = side === "left" ? node.x + r : node.x + node.w - r;
    return { x: side === "left" ? capX - along : capX + along, y: node.cy + clamped };
  }
  const limitY = Math.max(0, node.h / 2 - node.radius - 6);
  switch (side) {
    case "top":
      return { x: node.cx + Math.max(-limitX, Math.min(limitX, offset)), y: node.y };
    case "bottom":
      return { x: node.cx + Math.max(-limitX, Math.min(limitX, offset)), y: node.y + node.h };
    case "left":
      return { x: node.x, y: node.cy + Math.max(-limitY, Math.min(limitY, offset)) };
    default:
      return { x: node.x + node.w, y: node.cy + Math.max(-limitY, Math.min(limitY, offset)) };
  }
}

/** `align`: the line an aligned pair shares, for a `via` route between two facing sides. */
function candidatePoints(route: Route, candidate: Candidate, sOff: number, tOff: number, align: number | null = null): Point[] {
  const source = candidate.sourceEnd ?? route.source;
  const target = candidate.targetEnd ?? route.target;
  // The legacy direct shapes carry their own shared line (`candidate.align`).
  const shared = candidate.kind === "via" ? align : null;
  let s = sidePoint(source, candidate.sourceSide, alignShift(source, candidate.sourceSide, shared) + sOff);
  let t = sidePoint(target, candidate.targetSide, alignShift(target, candidate.targetSide, shared) + tOff);
  switch (candidate.kind) {
    case "via":
      return simplify(viaRaw(s, candidate.sourceSide, t, candidate.vias ?? []));
    case "h": {
      // Both anchors on the shared line; a lane difference becomes a Z.
      if (candidate.align !== undefined) {
        s = sidePoint(source, candidate.sourceSide, candidate.align - anchorCy(source) + sOff);
        t = sidePoint(target, candidate.targetSide, candidate.align - anchorCy(target) + tOff);
      }
      if (Math.abs(s.y - t.y) < 0.5) return [s, t];
      const mx = (s.x + t.x) / 2;
      return [s, { x: mx, y: s.y }, { x: mx, y: t.y }, t];
    }
    case "v": {
      if (candidate.align !== undefined) {
        s = sidePoint(source, candidate.sourceSide, candidate.align - anchorCx(source) + sOff);
        t = sidePoint(target, candidate.targetSide, candidate.align - anchorCx(target) + tOff);
      }
      if (Math.abs(s.x - t.x) < 0.5) return [s, t];
      const my = (s.y + t.y) / 2;
      return [s, { x: s.x, y: my }, { x: t.x, y: my }, t];
    }
    case "hv":
      return [s, { x: t.x, y: s.y }, t];
    case "vh":
      return [s, { x: s.x, y: t.y }, t];
    case "detour-h":
    case "vhv": {
      const y = candidate.corridor ?? Math.min(s.y, t.y) - ARCH.ROW_GAP / 2;
      return [s, { x: s.x, y }, { x: t.x, y }, t];
    }
    default: {
      const x = candidate.corridor ?? Math.min(s.x, t.x) - ARCH.COL_GAP / 2;
      return [s, { x, y: s.y }, { x, y: t.y }, t];
    }
  }
}

function overlaps(a0: number, a1: number, b0: number, b1: number): boolean {
  return Math.min(a1, b1) - Math.max(a0, b0) > 1;
}

/**
 * The area nodes occupy (a corridor outside it costs a little, one off the
 * canvas is clamped), the free corridors the grid leaves between its rows and
 * its columns (the centre of each gap), and the zone panels a label should
 * not sit across.
 */
interface Bounds {
  width: number;
  gridBottom: number;
  /** y of the centre of each gap between two consecutive rows, top to bottom. */
  rowGaps: number[];
  /** x of the centre of each gap between two consecutive columns, left to right. */
  colGaps: number[];
  zones: LayoutZone[];
  /** The zones' chip rows: an edge and a label keep off them as off a node. */
  chipRows: Rect[];
}

function candidatesFor(route: Route, bounds: Bounds): Candidate[] {
  const { source: s, target: t } = route;
  const out: Candidate[] = [];
  const xOverlap = overlaps(s.x, s.x + s.w, t.x, t.x + t.w);
  const yOverlap = overlaps(s.y, s.y + s.h, t.y, t.y + t.h);
  // The line both anchors share is read on what the edge meets (a frameless
  // node's content); which corridors exist is read on the boxes.
  const sa = s.anchor;
  const ta = t.anchor;
  if (yOverlap && !xOverlap) {
    // Side by side: straight across, or around by the row gap above / below.
    const toRight = t.cx > s.cx;
    const align = (Math.max(sa.y, ta.y) + Math.min(sa.y + sa.h, ta.y + ta.h)) / 2;
    out.push({ kind: "h", sourceSide: toRight ? "right" : "left", targetSide: toRight ? "left" : "right", align });
    out.push({ kind: "detour-h", sourceSide: "top", targetSide: "top", corridor: Math.min(s.y, t.y) - ARCH.ROW_GAP / 2 });
    out.push({
      kind: "detour-h",
      sourceSide: "bottom",
      targetSide: "bottom",
      corridor: Math.max(s.y + s.h, t.y + t.h) + ARCH.ROW_GAP / 2,
    });
    return out;
  }
  if (xOverlap && !yOverlap) {
    const toBottom = t.cy > s.cy;
    // The narrower node's centre, kept inside the wider one (a band over a card).
    const narrow = sa.w <= ta.w ? sa : ta;
    const wide = narrow === sa ? ta : sa;
    const inset = Math.min(24, wide.w / 2);
    const align = Math.max(wide.x + inset, Math.min(wide.x + wide.w - inset, narrow.x + narrow.w / 2));
    out.push({ kind: "v", sourceSide: toBottom ? "bottom" : "top", targetSide: toBottom ? "top" : "bottom", align });
    out.push({ kind: "detour-v", sourceSide: "left", targetSide: "left", corridor: Math.min(s.x, t.x) - ARCH.COL_GAP / 2 });
    out.push({
      kind: "detour-v",
      sourceSide: "right",
      targetSide: "right",
      corridor: Math.max(s.x + s.w, t.x + t.w) + ARCH.COL_GAP / 2,
    });
    return out;
  }
  const toRight = t.cx > s.cx;
  const toBottom = t.cy > s.cy;
  out.push({
    kind: "hv",
    sourceSide: toRight ? "right" : "left",
    targetSide: toBottom ? "top" : "bottom",
  });
  out.push({
    kind: "vh",
    sourceSide: toBottom ? "bottom" : "top",
    targetSide: toRight ? "left" : "right",
  });
  // Through the gaps between the two: leave vertically, run along a
  // row gap that lies between them, arrive vertically - and the same by a
  // column gap. Those corridors hold no node by construction (a band spans
  // columns, never rows, and the scorer still sees it), so a diagonal pair
  // boxed in by its neighbours keeps a route that touches neither.
  const upper = toBottom ? s : t;
  const lower = toBottom ? t : s;
  for (const y of bounds.rowGaps) {
    if (y <= upper.y + upper.h || y >= lower.y) continue;
    out.push({ kind: "vhv", sourceSide: toBottom ? "bottom" : "top", targetSide: toBottom ? "top" : "bottom", corridor: y });
  }
  const left = toRight ? s : t;
  const right = toRight ? t : s;
  for (const x of bounds.colGaps) {
    if (x <= left.x + left.w || x >= right.x) continue;
    out.push({ kind: "hvh", sourceSide: toRight ? "right" : "left", targetSide: toRight ? "left" : "right", corridor: x });
  }
  return out;
}

/** How near the canvas edge a `via` corridor may run: the stroke's width and a pixel. */
const VIA_EDGE_MARGIN = 4;

/** The axis each of a `via` route's corridors is measured on: x first when it leaves horizontally, then alternating. */
const viaAxes = (candidate: Candidate): ("x" | "y")[] =>
  (candidate.vias ?? []).map((_, index) => (runsHorizontally(candidate.sourceSide) === (index % 2 === 0) ? "x" : "y"));

/**
 * Keep a detour corridor on the canvas (the stroke is 3px, the label pill 34).
 * A `via` corridor may run nearer the edge - the margin past the outer
 * column is the only way round to a side facing out of the grid - and its
 * label finds another run.
 */
function clampCorridor(candidate: Candidate, bounds: Bounds): Candidate {
  const edge = candidate.kind === "via" ? VIA_EDGE_MARGIN : ARCH.EDGE_LABEL_H / 2 + 4;
  const clamp = (axis: "x" | "y", value: number) => (axis === "y" ? Math.max(edge, value) : Math.max(edge, Math.min(bounds.width - edge, value)));
  if (candidate.vias) {
    const axes = viaAxes(candidate);
    return { ...candidate, vias: candidate.vias.map((value, index) => clamp(axes[index], value)) };
  }
  if (candidate.corridor === undefined) return candidate;
  return { ...candidate, corridor: clamp(corridorAxis(candidate.kind) === "y" ? "y" : "x", candidate.corridor) };
}

// ── Pinned sides and zone ends ────────────────────────────────────────

/** The prefix of an edge end naming a zone's border - `ZONE_REF_PREFIX` in `presentation_spec.py`. */
/** @public */
export const ZONE_REF_PREFIX = "zone:";

/**
 * The zone a `zone:` edge end names - by id, else by 0-based index, else by
 * label (as written or as plain text) - or `null`: a node id, a zone that does
 * not exist, a label two zones share. A digit-only key past the last zone is
 * read as a label: a zone labelled `2026` is named by it. Mirrors
 * `resolve_zone_ref` in `presentation_spec.py`, which refuses the last two on save.
 */
export function resolveZoneRef(ref: string, zones: readonly DeckArchitectureZone[]): number | null {
  if (!ref.startsWith(ZONE_REF_PREFIX)) return null;
  const key = ref.slice(ZONE_REF_PREFIX.length).trim();
  const byId = zones.findIndex((zone) => zone.id === key);
  if (byId >= 0) return byId;
  if (/^[0-9]+$/.test(key) && Number(key) < zones.length) return Number(key);
  const byLabel = zones.flatMap((zone, index) => (zone.label && (zone.label === key || markupToPlainText(zone.label) === key) ? [index] : []));
  return byLabel.length === 1 ? byLabel[0] : null;
}

/** The shortest first and last run of a pinned or zone-ended route: room for its arrowhead. */
const STUB_MIN = 16;
/** How far a zone end keeps from the panel's rounded corners. */
const ZONE_END_INSET = 40;

/**
 * A zone's border met as an edge end: a flat box on the line of its `side`,
 * centred on the point of that side nearest `toward` and kept off the panel's
 * corners, so `sidePoint` meets it there and a lane offset slides it along
 * the border. Never drawn, and never an obstacle.
 */
function zoneEnd(ref: ZoneRef, side: Side, toward: Point): RouteEnd {
  const { zone } = ref;
  const horizontal = side === "top" || side === "bottom";
  const lo = (horizontal ? zone.x : zone.y) + ZONE_END_INSET;
  const hi = (horizontal ? zone.x + zone.w : zone.y + zone.h) - ZONE_END_INSET;
  const at = lo < hi ? Math.max(lo, Math.min(hi, horizontal ? toward.x : toward.y)) : (lo + hi) / 2;
  // `sidePoint` keeps 6px off a frameless anchor's ends: the slide reaches the nearer inset.
  const half = Math.max(0, Math.min(at - lo, hi - at)) + 6;
  const line = side === "top" ? zone.y : side === "bottom" ? zone.y + zone.h : side === "left" ? zone.x : zone.x + zone.w;
  const box: Rect = horizontal ? { x: at - half, y: line, w: 2 * half, h: 0 } : { x: line, y: at - half, w: 0, h: 2 * half };
  return {
    ...box,
    id: `${ZONE_REF_PREFIX}${ref.index}`,
    cx: horizontal ? at : line,
    cy: horizontal ? line : at,
    anchor: box,
    frame: false,
    kind: "card",
    radius: 0,
    emphasis: "normal",
    zone,
    side,
  };
}

/** The sides of `box` that face `rect`, the widest gap first; every side when the two overlap. */
function sidesToward(box: Rect, rect: Rect): Side[] {
  const gaps: [Side, number][] = [
    ["top", box.y - (rect.y + rect.h)],
    ["right", rect.x - (box.x + box.w)],
    ["bottom", rect.y - (box.y + box.h)],
    ["left", box.x - (rect.x + rect.w)],
  ];
  const facing = gaps.filter(([, gap]) => gap >= 0).sort((a, b) => b[1] - a[1]);
  return facing.length ? facing.map(([side]) => side) : [...SIDES];
}

/**
 * The polyline of a `via` route: out of `s` along its side's normal, to each
 * corridor of `vias` in turn - an x while running horizontally, a y while
 * running vertically - turning at each, then one last turn in line with `t`
 * and into it. Raw: its zero-length runs are kept for `viaFits` to read, which
 * also refuses a list whose last turn does not enter `t` along its side.
 */
function viaRaw(s: Point, sourceSide: Side, t: Point, vias: readonly number[]): Point[] {
  const points = [s];
  let p = s;
  let horizontal = runsHorizontally(sourceSide);
  for (const value of vias) {
    p = horizontal ? { x: value, y: p.y } : { x: p.x, y: value };
    points.push(p);
    horizontal = !horizontal;
  }
  points.push(horizontal ? { x: t.x, y: p.y } : { x: p.x, y: t.y }, t);
  return points;
}

/** A polyline without its zero-length runs and its straight-through points. */
function simplify(points: Point[]): Point[] {
  const out: Point[] = [];
  for (const p of points) {
    const last = out[out.length - 1];
    if (last && Math.abs(last.x - p.x) < 0.01 && Math.abs(last.y - p.y) < 0.01) continue;
    if (out.length >= 2 && Math.abs(cross(out[out.length - 2], last, p)) < 0.01) {
      out[out.length - 1] = p;
      continue;
    }
    out.push(p);
  }
  return out;
}

/**
 * Does a `via` route never double back on itself, leave its source along the
 * side's normal and enter its target against its side's normal, each by
 * `STUB_MIN` at least? Read on the raw polyline for the first, on the
 * simplified one for the rest - a Z whose jog is zero is one straight run.
 */
function viaFits(raw: Point[], sourceSide: Side, targetSide: Side): boolean {
  const runs = segmentsOf(raw)
    .map(([a, b]) => ({ x: b.x - a.x, y: b.y - a.y }))
    .filter((run) => Math.abs(run.x) + Math.abs(run.y) > 0.01);
  if (!runs.length || runs.some((run, index) => index > 0 && run.x * runs[index - 1].x + run.y * runs[index - 1].y < 0)) return false;
  const points = simplify(raw);
  const first = { x: points[1].x - points[0].x, y: points[1].y - points[0].y };
  const last = { x: points[points.length - 1].x - points[points.length - 2].x, y: points[points.length - 1].y - points[points.length - 2].y };
  const out = OUT[sourceSide];
  const into = OUT[targetSide];
  return first.x * out.x + first.y * out.y >= STUB_MIN && -(last.x * into.x + last.y * into.y) >= STUB_MIN;
}

/** Every corridor a `via` route may run along on one axis: the grid's free gaps, just outside either end, halfway between them. */
function corridorOptions(axis: "x" | "y", boxes: readonly Rect[], s: Point, t: Point, bounds: Bounds): number[] {
  const gap = axis === "x" ? ARCH.COL_GAP / 2 : ARCH.ROW_GAP / 2;
  const values = [...(axis === "x" ? bounds.colGaps : bounds.rowGaps)];
  for (const box of boxes) {
    values.push(axis === "x" ? box.x - gap : box.y - gap, axis === "x" ? box.x + box.w + gap : box.y + box.h + gap);
  }
  values.push(axis === "x" ? (s.x + t.x) / 2 : (s.y + t.y) / 2);
  const out: number[] = [];
  for (const value of values) if (out.every((kept) => Math.abs(kept - value) > 1)) out.push(value);
  return out;
}

/** The `count` corridors nearest `from` that lie `STUB_MIN` past it in the direction `sign`. */
function corridorsPast(options: readonly number[], from: number, sign: number, count: number): number[] {
  return options
    .filter((value) => (value - from) * sign >= STUB_MIN)
    .sort((a, b) => (a - from) * sign - (b - from) * sign)
    .slice(0, count);
}

/** One way an end of a pinned or zone-ended edge may meet its node or zone. */
interface EndOption {
  end: RouteEnd;
  side: Side;
}

/**
 * The `via` routes from one end option to another: the fewest bends the two
 * sides allow when they fit (an elbow between perpendicular sides; a straight
 * run or a Z between parallel ones), and the routes one detour longer - out
 * past the source, along any corridor, in past the target - which is how a
 * side facing away from the other end is honoured rather than corrected.
 */
function viaCandidates(source: EndOption, target: EndOption, bounds: Bounds): Candidate[] {
  const s = sidePoint(source.end, source.side, 0);
  const t = sidePoint(target.end, target.side, 0);
  const boxes = [source.end.zone ?? source.end, target.end.zone ?? target.end];
  const xs = corridorOptions("x", boxes, s, t, bounds);
  const ys = corridorOptions("y", boxes, s, t, bounds);
  const sh = runsHorizontally(source.side);
  const th = runsHorizontally(target.side);
  const out = OUT[source.side];
  const into = OUT[target.side];
  const firstPast = (count: number) => corridorsPast(sh ? xs : ys, sh ? s.x : s.y, sh ? out.x : out.y, count);
  const lastPast = (count: number) => corridorsPast(th ? xs : ys, th ? t.x : t.y, th ? into.x : into.y, count);
  const lists: number[][] = [];
  if (sh !== th) {
    lists.push([]);
    for (const first of firstPast(3)) for (const last of lastPast(3)) lists.push([first, last]);
  } else {
    for (const value of sh ? xs : ys) lists.push([value]);
    for (const first of firstPast(2)) for (const middle of sh ? ys : xs) for (const last of lastPast(2)) lists.push([first, middle, last]);
  }
  const candidates: Candidate[] = [];
  for (const vias of lists) {
    const candidate = clampCorridor(
      { kind: "via", sourceSide: source.side, targetSide: target.side, vias, sourceEnd: source.end, targetEnd: target.end },
      bounds,
    );
    if (viaFits(viaRaw(s, source.side, t, candidate.vias ?? []), source.side, target.side)) candidates.push(candidate);
  }
  return candidates;
}

/**
 * Where one end of a pinned or zone-ended edge may meet its node or zone: its
 * pinned side alone; else a zone's sides facing the node at the other end, a
 * node's sides facing the zone at the other end, or every side of a node
 * whose other end pinned its own.
 */
function endOptions(route: Route, which: "source" | "target"): EndOption[] {
  const pinned = which === "source" ? route.edge.source_side : route.edge.target_side;
  const zone = which === "source" ? route.sourceZone : route.targetZone;
  const other = which === "source" ? route.target : route.source;
  if (zone) {
    // The other end is a node: the validator refuses an edge between two zones.
    const toward = { x: anchorCx(other), y: anchorCy(other) };
    return (pinned ? [pinned] : sidesToward(zone.zone, other)).map((side) => ({ end: zoneEnd(zone, side, toward), side }));
  }
  const node = which === "source" ? route.source : route.target;
  const otherZone = which === "source" ? route.targetZone : route.sourceZone;
  const sides = pinned ? [pinned] : otherZone ? sidesToward(node, otherZone.zone) : SIDES;
  return sides.map((side) => ({ end: node, side }));
}

/** Every `via` route a pinned or zone-ended edge may take. */
function viaCandidatesFor(route: Route, bounds: Bounds): Candidate[] {
  const out: Candidate[] = [];
  for (const source of endOptions(route, "source")) {
    for (const target of endOptions(route, "target")) out.push(...viaCandidates(source, target, bounds));
  }
  return out;
}

function scoreCandidate(
  route: Route,
  candidate: Candidate,
  points: Point[],
  bends: number,
  nodes: LayoutNode[],
  routed: Route[],
  bounds: Bounds,
): number {
  let conflicts = 0;
  let crossings = 0;
  for (const [a, b] of segmentsOf(points)) {
    for (const node of nodes) {
      if (node === route.source || node === route.target) continue;
      if (segmentIntersectsRect(a, b, inflate(node, 10))) conflicts += 1;
    }
    for (const row of bounds.chipRows) if (segmentIntersectsRect(a, b, inflate(row, 10))) conflicts += 1;
    for (const other of routed) {
      // A straight tie is never laned apart from an edge drawn along it: a run
      // over one counts like a crossing. Elbows sharing a node side are not
      // scored that way - the lanes part them once every corridor is picked.
      const straight = other.mode === "straight";
      for (const [c, d] of segmentsOf(other.points)) {
        if (segmentsCross(a, b, c, d)) crossings += 1;
        else if (straight && segmentsOverlapCollinear(a, b, c, d)) crossings += 1;
      }
    }
  }
  // A route ending on a zone's border reaches it from outside: through the panel it would cross what it points at.
  for (const zone of [route.sourceZone, route.targetZone]) {
    if (!zone) continue;
    for (const [a, b] of segmentsOf(points)) if (segmentIntersectsRect(a, b, inflate(zone.zone, -2))) conflicts += 1;
  }
  // A side facing away is left by going around its own node, never back through it.
  if (candidate.kind === "via") {
    for (const end of [candidate.sourceEnd, candidate.targetEnd]) {
      if (!end || end.zone) continue;
      for (const [a, b] of segmentsOf(points)) if (segmentIntersectsRect(a, b, inflate(end.anchor, -1))) conflicts += 1;
    }
  }
  let outside = 0;
  const corridors: ["x" | "y", number][] = candidate.vias
    ? candidate.vias.map((value, index) => [viaAxes(candidate)[index], value])
    : candidate.corridor !== undefined
      ? [[corridorAxis(candidate.kind) === "y" ? "y" : "x", candidate.corridor]]
      : [];
  for (const [axis, value] of corridors) {
    const inside = axis === "y" ? value >= ARCH.PAD && value <= bounds.gridBottom : value >= ARCH.PAD && value <= bounds.width - ARCH.PAD;
    if (!inside) outside = 1;
  }
  // A side another pair's edge already uses costs a little: the lanes would
  // part the two, but a tie meant to land on a card's centre then does not,
  // and a free side of the same node is a better place. The two edges of an
  // exchange between the same nodes are what the lanes are for - exempt.
  let crowded = 0;
  for (const other of routed) {
    const samePair =
      (other.source === route.source && other.target === route.target) ||
      (other.source === route.target && other.target === route.source);
    if (samePair) continue;
    for (const end of ["source", "target"] as const) {
      const node = end === "source" ? route.source : route.target;
      const side = end === "source" ? candidate.sourceSide : candidate.targetSide;
      if (routeSide(other, other.source === node ? "source" : other.target === node ? "target" : null) === side) crowded += 1;
    }
  }
  // Fewer bends read cleaner: a corridor (two bends) is taken over the plain
  // elbow (one) only when the elbow runs through a node, across an edge or
  // onto a side already taken.
  return conflicts * 1000 + outside * 100 + crossings * 10 + crowded * 4 + bends * 3 + length(points) * 0.01;
}

/** The side a routed edge meets a node on at one end; `null` for a circle end of a tie, or no end there. */
function routeSide(route: Route, end: "source" | "target" | null): Side | null {
  if (!end) return null;
  if (route.candidate) return end === "source" ? route.candidate.sourceSide : route.candidate.targetSide;
  if (route.straight) return end === "source" ? route.straight.sourceSide : route.straight.targetSide;
  return null;
}

/** Straight runs a `route: "curve"` edge is sampled into for the extent audits. */
const CURVE_SAMPLES = 16;

/**
 * A `route: "curve"` edge drawn along the corridor the scorer picked for it:
 * the cubic's two control points are the interior waypoints of the
 * scored orthogonal polyline - the corridor's two corners for a detour, the
 * elbow's corner for a direct edge, the Z's two bends for a laned side-by-side
 * run - so the curve leaves and arrives along the same normals the elbow
 * would, bulges TOWARD the corridor and never past it. A Bezier lies inside
 * the convex hull of its four points, so the curve stays inside the elbow's
 * own bounding box: inside the canvas whenever the corridor is
 * (`clampCorridor`), and clear of the nodes the corridor was scored to avoid.
 * Control lengths derived from the anchor distance (the previous rule) put
 * the arc of a one-row detour hundreds of pixels off the canvas. Two aligned
 * anchors with no waypoint draw a straight line.
 *
 * `points` is the curve sampled into `CURVE_SAMPLES` runs, ends exact: what
 * `drawnBottom`, the crossing score of later edges and the spec's audits read
 * as the edge's true extent (the chord alone hid the arc from all of them).
 * The scorer sees the same samples while the curve's own corridor is picked
 * and as soon as it is - the arc cuts the corner the polyline turns, so a
 * node or a crossing in that corner shows up in the score only if the arc,
 * not the polyline, is what is measured.
 */
function corridorCurve(polyline: Point[]): { d: string; at: (u: number) => Point; points: Point[] } {
  const s = polyline[0];
  const t = polyline[polyline.length - 1];
  if (polyline.length < 3) {
    const at = (u: number) => ({ x: s.x + (t.x - s.x) * u, y: s.y + (t.y - s.y) * u });
    return { d: roundedPath([s, t]), at, points: [s, t] };
  }
  const c1 = polyline[1];
  const c2 = polyline[polyline.length - 2];
  const at = (u: number) => {
    const v = 1 - u;
    return {
      x: v * v * v * s.x + 3 * v * v * u * c1.x + 3 * v * u * u * c2.x + u * u * u * t.x,
      y: v * v * v * s.y + 3 * v * v * u * c1.y + 3 * v * u * u * c2.y + u * u * u * t.y,
    };
  };
  const points = [s];
  for (let i = 1; i < CURVE_SAMPLES; i += 1) points.push(at(i / CURVE_SAMPLES));
  points.push(t);
  const f = (n: number) => n.toFixed(1);
  return {
    d: `M ${f(s.x)} ${f(s.y)} C ${f(c1.x)} ${f(c1.y)}, ${f(c2.x)} ${f(c2.y)}, ${f(t.x)} ${f(t.y)}`,
    at,
    points,
  };
}

/** Polyline path data with rounded elbows. */
function roundedPath(points: Point[], radius = ARCH.CORNER): string {
  const f = (n: number) => n.toFixed(1);
  if (points.length < 2) return "";
  if (points.length === 2) return `M ${f(points[0].x)} ${f(points[0].y)} L ${f(points[1].x)} ${f(points[1].y)}`;
  let d = `M ${f(points[0].x)} ${f(points[0].y)}`;
  for (let i = 1; i < points.length - 1; i += 1) {
    const prev = points[i - 1];
    const here = points[i];
    const next = points[i + 1];
    const inLen = Math.hypot(here.x - prev.x, here.y - prev.y);
    const outLen = Math.hypot(next.x - here.x, next.y - here.y);
    const r = Math.min(radius, inLen / 2, outLen / 2);
    if (r < 1 || inLen === 0 || outLen === 0) {
      d += ` L ${f(here.x)} ${f(here.y)}`;
      continue;
    }
    const inX = here.x - ((here.x - prev.x) / inLen) * r;
    const inY = here.y - ((here.y - prev.y) / inLen) * r;
    const outX = here.x + ((next.x - here.x) / outLen) * r;
    const outY = here.y + ((next.y - here.y) / outLen) * r;
    d += ` L ${f(inX)} ${f(inY)} Q ${f(here.x)} ${f(here.y)} ${f(outX)} ${f(outY)}`;
  }
  const last = points[points.length - 1];
  return `${d} L ${f(last.x)} ${f(last.y)}`;
}

/** Fractions along a run tried for a label, the middle first, then either side of it. */
const LABEL_STOPS = [0.5, 0.42, 0.58, 0.34, 0.66, 0.26, 0.74, 0.18, 0.82];
/** How far a label stays from the end of the run it sits on (the node there). */
const LABEL_END_MARGIN = 6;
/** Which ends of a polyline carry an arrowhead: a label on the line keeps clear of it. */
interface LabelHeads {
  start: boolean;
  end: boolean;
}
const NO_HEADS: LabelHeads = { start: false, end: false };
/**
 * The clearance a label keeps from a node: `LABEL_NODE_MARGIN` is the room it
 * wants, `LABEL_NODE_MIN` the least it may keep. A label in between costs
 * `LABEL_NODE_NEAR` per node, about three slide steps, so a pill wider than
 * the gap it sits in stays on its edge with a few pixels to spare rather than
 * sliding past the nodes beside it, a whole node height away from its line
 * ("Telemetry" 116 px under its edge, a zone on the core column).
 */
const LABEL_NODE_MARGIN = 6;
const LABEL_NODE_MIN = 2;
const LABEL_NODE_NEAR = 3;
/** How far a label sits off its line once beside it, and how many steps further out it may go. */
const LABEL_SLIDE_STEP = 16;
const LABEL_SLIDE_STEPS = 8;
/** The rank every slide off the line starts from: a spot ranked under it is on its line. */
const LABEL_SLIDE_RANK = 1000;

/**
 * A place a label may take: its centre, the line's unit direction there (for
 * the slide off the line) and its rank, lower preferred - the middle of the
 * longest run first, then along it, then the other runs, then off the line.
 */
interface LabelSpot {
  at: Point;
  along: Point;
  rank: number;
}

const lerp = (a: Point, b: Point, t: number): Point => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });

/**
 * Where a `w` x `h` label may sit ON a polyline: the stops of every
 * run long enough to hold it, longest run first, the middle of each first -
 * so a label slides ALONG its path away from a node or a label already placed,
 * and two edges sharing a run put their labels at different stops of it. A
 * run too short for the label still offers its middle, ranked last: the slide
 * off the line (`labelSlides`) is what rescues it. An end with an arrowhead
 * (`heads`) keeps `ARCH.ARROW_HEAD` more room, so the pill stops before the
 * marker.
 *
 * One near-node rule for every edge: a pill sits on its line
 * when the line's gap holds it within `LABEL_NODE_MIN` of the nodes, zone or
 * no zone. A pill WIDER than the gap ("Telemetry" in the 96-unit gap between
 * two cards) cannot be on its line without covering a node, a hard
 * constraint, so it takes the nearest slide: just under the row, its top one
 * slide step below the nodes; above the row is the canvas edge on a first row.
 */
function labelSpots(points: Point[], w: number, h: number, heads: LabelHeads = NO_HEADS): LabelSpot[] {
  const segments = segmentsOf(points);
  const runs = segments
    .map(([a, b], index) => ({
      a,
      b,
      len: Math.hypot(b.x - a.x, b.y - a.y),
      // An arrowhead on this run: at the polyline's first point, at its last.
      headA: heads.start && index === 0,
      headB: heads.end && index === segments.length - 1,
    }))
    .filter((run) => run.len > 1)
    .sort((a, b) => b.len - a.len);
  if (!runs.length) return points.length ? [{ at: points[0], along: { x: 1, y: 0 }, rank: 0 }] : [];
  const spots: LabelSpot[] = [];
  runs.forEach((run, runIndex) => {
    const along = { x: (run.b.x - run.a.x) / run.len, y: (run.b.y - run.a.y) / run.len };
    // The label's half extent measured along the run: its half width on a
    // horizontal run, its half height on a vertical one.
    const extent = (Math.abs(along.x) * w + Math.abs(along.y) * h) / 2;
    // The room each end keeps: the node there, plus its arrowhead when it has one.
    const marginA = LABEL_END_MARGIN + (run.headA ? ARCH.ARROW_HEAD : 0);
    const marginB = LABEL_END_MARGIN + (run.headB ? ARCH.ARROW_HEAD : 0);
    const lo = (extent + marginA) / run.len;
    const hi = 1 - (extent + marginB) / run.len;
    const fits = lo <= hi;
    if (!fits) {
      // Too short for the pill and its margins: its middle, ranked last. With
      // an arrowhead at one end only, the pill is pushed against the other end
      // as far as `LABEL_NODE_MIN` allows, so as much of the head shows as the
      // gap leaves (4.6 of the 12 units showed beside "Telemetry").
      let u = 0.5;
      if (run.headA !== run.headB) {
        const pushed = (extent + LABEL_NODE_MIN + 0.5) / run.len;
        u = run.headB ? Math.min(0.5, pushed) : Math.max(0.5, 1 - pushed);
      }
      spots.push({ at: lerp(run.a, run.b, u), along, rank: 500 + runIndex * 20 });
      return;
    }
    LABEL_STOPS.forEach((t, stopIndex) => {
      // Keep the pill off the run's ends (the nodes, their arrowheads, or an elbow's corner).
      const u = Math.max(lo, Math.min(hi, t));
      spots.push({ at: lerp(run.a, run.b, u), along, rank: runIndex * 20 + stopIndex });
    });
  });
  return spots;
}

/**
 * The same spots moved off their line, both ways, by the pill's own clearance
 * first (its edge touching the line) and then further out by `LABEL_SLIDE_STEP`
 * - a label wider than the gap it crosses can only be clear of the two nodes
 * beside that gap once it is above or below them. Ranked after every on-line
 * spot, nearer first.
 */
function labelSlides(spots: LabelSpot[], w: number, h: number): LabelSpot[] {
  const out: LabelSpot[] = [];
  for (const spot of spots) {
    const normal = { x: -spot.along.y, y: spot.along.x };
    const beside = (Math.abs(normal.x) * w + Math.abs(normal.y) * h) / 2 + 4;
    for (let step = 0; step < LABEL_SLIDE_STEPS; step += 1) {
      const distance = beside + step * LABEL_SLIDE_STEP;
      for (const sign of [-1, 1]) {
        out.push({
          at: { x: spot.at.x + normal.x * distance * sign, y: spot.at.y + normal.y * distance * sign },
          along: spot.along,
          rank: LABEL_SLIDE_RANK + step * 100 + spot.rank,
        });
      }
    }
  }
  return out;
}

/**
 * Pick the spot whose label box is clear: a node (within `LABEL_NODE_MIN`;
 * nearer than `LABEL_NODE_MARGIN` costs a little), a zone title, a zone's chip
 * row or a label already placed are HARD constraints (any clear spot beats any
 * overlapping one, however far it had to slide), the canvas edge next, then a zone's
 * dashed border (a pill sitting across it reads as a hole in the panel; not
 * counted for a spot on the label's own line when that line runs from outside
 * the zone into it: the edge already cuts the border there), then
 * a spot under the last row (it grows the drawing; a slide into the row gap
 * above is the same label without the cost - less than a line's own spot
 * costs against a slide, so a corridor under the grid keeps its label), and
 * among equals the lowest rank - on the line, nearest its middle.
 */
function placeLabel(
  spots: LabelSpot[],
  w: number,
  h: number,
  nodes: LayoutNode[],
  placed: Rect[],
  bounds: Bounds,
  path: Point[],
): Point {
  let best: { at: Point; score: number } | null = null;
  const first = path[0];
  const last = path[path.length - 1];
  const entered = (zone: Rect): boolean =>
    first !== undefined && last !== undefined && pointInRect(first, zone) !== pointInRect(last, zone);
  for (const spot of spots) {
    const rect: Rect = { x: spot.at.x - w / 2, y: spot.at.y - h / 2, w, h };
    let score = spot.rank * 0.01;
    for (const node of nodes) {
      if (rectsOverlap(rect, inflate(node, LABEL_NODE_MIN))) score += 1e6;
      else if (rectsOverlap(rect, inflate(node, LABEL_NODE_MARGIN))) score += LABEL_NODE_NEAR;
    }
    for (const zone of bounds.zones) {
      if (rectsOverlap(rect, inflate(zoneHeadBox(zone), 6))) score += 1e6;
      if (crossesBorder(rect, zone) && !(spot.rank < LABEL_SLIDE_RANK && entered(zone))) score += 20;
    }
    for (const row of bounds.chipRows) if (rectsOverlap(rect, inflate(row, 6))) score += 1e6;
    for (const other of placed) if (rectsOverlap(rect, inflate(other, 8))) score += 1e5;
    if (rect.x < 0 || rect.x + w > bounds.width || rect.y < 0) score += 1e4;
    if (rect.y + h > bounds.gridBottom) score += 5;
    if (!best || score < best.score) best = { at: spot.at, score };
  }
  return best ? (best as { at: Point }).at : spots[0].at;
}

function headsOf(edge: DeckArchitectureEdge): LabelHeads {
  const arrows = edge.arrows ?? "end";
  return { start: arrows === "both", end: arrows !== "none" };
}

function pointInRect(p: Point, r: Rect): boolean {
  return p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h;
}

function rectsOverlap(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}

/** The zone's head as drawn - its icon or logo, then its label - on the title row. */
export function zoneHeadBox(zone: LayoutZone): Rect {
  const markH = zone.logo ? zone.logo.h : zone.zone.icon ? ARCH.ZONE_ICON : 0;
  const h = Math.max(markH, zone.title?.lineHeight ?? 0);
  const right = zone.title ? zone.titleX + zone.title.width : zone.logo ? zone.logo.x + zone.logo.w : zone.titleX;
  return { x: zone.iconX, y: zone.titleY - h / 2, w: right - zone.iconX, h };
}

/** Does the rect straddle one of the zone's four border lines? */
function crossesBorder(rect: Rect, zone: Rect): boolean {
  const spansX = rect.x < zone.x + zone.w && rect.x + rect.w > zone.x;
  const spansY = rect.y < zone.y + zone.h && rect.y + rect.h > zone.y;
  const acrossTop = rect.y < zone.y && rect.y + rect.h > zone.y;
  const acrossBottom = rect.y < zone.y + zone.h && rect.y + rect.h > zone.y + zone.h;
  const acrossLeft = rect.x < zone.x && rect.x + rect.w > zone.x;
  const acrossRight = rect.x < zone.x + zone.w && rect.x + rect.w > zone.x + zone.w;
  return (spansX && (acrossTop || acrossBottom)) || (spansY && (acrossLeft || acrossRight));
}

function effectiveEdgeTone(edge: DeckArchitectureEdge): DeckTone {
  if (edge.tone) return edge.tone;
  return (edge.arrows ?? "end") === "none" ? "muted" : "accent";
}

const zoneOfEnd = (route: Route, end: "source" | "target"): ZoneRef | null => (end === "source" ? route.sourceZone : route.targetZone);

/** How many lane widths an aligned pair is moved off its sides' middles, at most, to clear another pair. */
const MAX_LANE_SHIFTS = 3;

/**
 * The edges of one pair of nodes that run between two FACING sides -
 * a right side to the left side of a node beside it, a bottom to the top of
 * a node under it - each given ONE offset, the same on both nodes: the pair's
 * edges in `lane` order (then in the order they took on the side of the node
 * whose id sorts first), spread symmetrically around the centre - the -d / +d
 * of an exchange - so a straight or a direct elbow between them stays level
 * whatever else meets either side. A lone edge counts when it names a lane.
 * `aligns` is the line both nodes share on the cross axis, when they overlap
 * on it, which a straight or a `via` route then runs from.
 */
function alignPairs(routed: Route[], anchors: Map<string, AnchorRef[]>): { fixed: Map<AnchorRef, number>; aligns: Map<Route, number> } {
  const groups = new Map<string, { first: RouteEnd; side: Side; second: RouteEnd; routes: Route[] }>();
  for (const route of routed) {
    if (route.sourceZone || route.targetZone) continue;
    const sourceSide = routeSide(route, "source");
    const targetSide = routeSide(route, "target");
    if (!sourceSide || !targetSide || OPPOSITE[sourceSide] !== targetSide) continue;
    const out = OUT[sourceSide];
    const ahead = (anchorCx(route.target) - anchorCx(route.source)) * out.x + (anchorCy(route.target) - anchorCy(route.source)) * out.y;
    if (ahead <= 0) continue;
    const [first, side, second] =
      route.source.id < route.target.id ? [route.source, sourceSide, route.target] : [route.target, targetSide, route.source];
    const key = `${first.id}:${side}|${second.id}`;
    const group = groups.get(key) ?? { first, side, second, routes: [] };
    group.routes.push(route);
    groups.set(key, group);
  }
  const fixed = new Map<AnchorRef, number>();
  const aligns = new Map<Route, number>();
  for (const { first, side, second, routes } of groups.values()) {
    if (routes.length < 2 && routes.every((route) => route.edge.lane === undefined)) continue;
    const firstList = anchors.get(`${first.id}:${side}`) ?? [];
    const secondList = anchors.get(`${second.id}:${OPPOSITE[side]}`) ?? [];
    const refOf = (route: Route, list: AnchorRef[]) => list.find((ref) => ref.route === route);
    const rank = (route: Route) => firstList.findIndex((ref) => ref.route === route);
    const order = [...routes].sort(
      (a, b) => (a.edge.lane ?? Number.MAX_SAFE_INTEGER) - (b.edge.lane ?? Number.MAX_SAFE_INTEGER) || rank(a) - rank(b),
    );
    // Centred on the sides' middles, unless an offset another pair fixed on either side is in the way.
    const taken = [...firstList, ...secondList].flatMap((ref) => (fixed.has(ref) ? [fixed.get(ref)!] : []));
    const offsetsAround = (centre: number) => order.map((_, j) => centre + (j - (order.length - 1) / 2) * ARCH.LANE);
    const clear = (offsets: number[]) => offsets.every((offset) => taken.every((other) => Math.abs(offset - other) >= ARCH.LANE - 0.5));
    let offsets = offsetsAround(0);
    for (let step = 1; !clear(offsets) && step <= 2 * MAX_LANE_SHIFTS; step += 1) {
      offsets = offsetsAround((step % 2 ? 1 : -1) * Math.ceil(step / 2) * ARCH.LANE);
    }
    order.forEach((route, j) => {
      for (const ref of [refOf(route, firstList), refOf(route, secondList)]) if (ref) fixed.set(ref, offsets[j]);
    });
    const horizontal = runsHorizontally(side);
    const a = first.anchor;
    const b = second.anchor;
    const lo = horizontal ? Math.max(a.y, b.y) : Math.max(a.x, b.x);
    const hi = horizontal ? Math.min(a.y + a.h, b.y + b.h) : Math.min(a.x + a.w, b.x + b.w);
    if (hi - lo > 1) for (const route of routes) aligns.set(route, (lo + hi) / 2);
  }
  return { fixed, aligns };
}

/**
 * Offsets along one node side: evenly around its centre, `ARCH.LANE` apart,
 * in the order the anchors sort in. The anchors an aligned pair fixed keep
 * their offsets (in their order, at the places they sorted to) and the others
 * line up past them at the same spacing.
 */
function spreadAnchors(list: AnchorRef[], fixed: Map<AnchorRef, number>): void {
  const slots = list.flatMap((ref, index) => (fixed.has(ref) ? [index] : []));
  if (!slots.length) {
    list.forEach((anchor, index) => {
      anchor.offset = (index - (list.length - 1) / 2) * ARCH.LANE;
    });
    return;
  }
  const byOffset = slots.map((slot) => list[slot]).sort((a, b) => fixed.get(a)! - fixed.get(b)!);
  slots.forEach((slot, k) => {
    list[slot] = byOffset[k];
    list[slot].offset = fixed.get(byOffset[k])!;
  });
  const firstSlot = slots[0];
  const lastSlot = slots[slots.length - 1];
  list.forEach((anchor, index) => {
    if (fixed.has(anchor)) return;
    if (index < firstSlot) anchor.offset = list[firstSlot].offset - (firstSlot - index) * ARCH.LANE;
    else if (index > lastSlot) anchor.offset = list[lastSlot].offset + (index - lastSlot) * ARCH.LANE;
    else {
      const before = Math.max(...slots.filter((slot) => slot < index));
      const after = Math.min(...slots.filter((slot) => slot > index));
      anchor.offset = list[before].offset + ((list[after].offset - list[before].offset) * (index - before)) / (after - before);
    }
  });
}

function routeEdges(block: DeckArchitectureBlock, nodes: LayoutNode[], bounds: Bounds): LayoutEdge[] {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const zoneSpecs = bounds.zones.map((zone) => zone.zone);
  const zoneRef = (ref: string): ZoneRef | null => {
    const index = resolveZoneRef(ref, zoneSpecs);
    return index === null ? null : { zone: bounds.zones[index], index };
  };
  const routes: Route[] = [];
  (block.edges ?? []).forEach((edge, index) => {
    const sourceZone = zoneRef(edge.source);
    const targetZone = zoneRef(edge.target);
    const sourceNode = sourceZone ? undefined : byId.get(edge.source);
    const targetNode = targetZone ? undefined : byId.get(edge.target);
    const node = sourceNode ?? targetNode;
    if (!node || !(sourceNode || sourceZone) || !(targetNode || targetZone) || sourceNode === targetNode) return;
    // A zone end starts on the side of its zone facing the node; the router weighs every side it may take.
    const toward = { x: anchorCx(node), y: anchorCy(node) };
    const facing = (zone: ZoneRef, pinned: Side | undefined) => zoneEnd(zone, pinned ?? sidesToward(zone.zone, node)[0], toward);
    const source: RouteEnd = sourceNode ?? facing(sourceZone!, edge.source_side);
    const target: RouteEnd = targetNode ?? facing(targetZone!, edge.target_side);
    const wanted = edge.route ?? "auto";
    // A pinned side asks for the elbows it leaves or enters by; a zone is never a core.
    const pinned = !!(edge.source_side || edge.target_side);
    const touchesCore = !sourceZone && !targetZone && (source.emphasis === "core" || target.emphasis === "core");
    const mode: Route["mode"] =
      wanted === "straight" || (wanted === "auto" && touchesCore && !pinned) ? "straight" : wanted === "curve" ? "curve" : "ortho";
    routes.push({ edge, index, source, target, sourceZone, targetZone, mode, candidate: null, straight: null, points: [] });
  });

  // 1. Pick a corridor for every edge, in order, scoring against the nodes
  //    and the edges already placed (anchors provisionally at side centres).
  const routed: Route[] = [];
  for (const route of routes) {
    if (route.mode === "straight") {
      // One straight line between the two shapes: a tie into the core is
      // radial, and it meets a card on the centre of the facing side (its
      // lane, once the side's anchors are spread) - or on the side it pins.
      const facing = straightSides(route.source, route.target);
      const sides: StraightSides = {
        sourceSide: route.edge.source_side ?? facing.sourceSide,
        targetSide: route.edge.target_side ?? facing.targetSide,
      };
      const chord = straightPoints(route.source, route.target, sides, 0, 0);
      // `auto` earns the straight line only when it is clear: a chord through
      // a third node is the one thing the routing contract rules out, so such
      // a tie falls back to the scored elbows below. An explicit
      // `route: "straight"` is drawn as written.
      const blocked =
        route.edge.route !== "straight" &&
        (nodes.some(
          (node) => node !== route.source && node !== route.target && segmentIntersectsRect(chord[0], chord[1], inflate(node, 10)),
        ) ||
          bounds.chipRows.some((row) => segmentIntersectsRect(chord[0], chord[1], inflate(row, 10))));
      if (!blocked) {
        route.points = chord;
        route.straight = sides;
        routed.push(route);
        continue;
      }
      route.mode = "ortho";
    }
    // A pinned side or a zone end takes the `via` routes, which honour the
    // side as written; every other edge keeps the shapes it always had.
    const via = !!(route.sourceZone || route.targetZone || route.edge.source_side || route.edge.target_side);
    let best: { candidate: Candidate; points: Point[]; score: number } | null = null;
    for (const raw of via ? viaCandidatesFor(route, bounds) : candidatesFor(route, bounds)) {
      const candidate = clampCorridor(raw, bounds);
      const polyline = candidatePoints(route, candidate, 0, 0);
      // Score what will be drawn: a curve is measured as its sampled arc, so
      // a node or a crossing in the corner it cuts counts against it, and the
      // edges routed after it score their crossings against that same arc.
      const points = route.mode === "curve" ? corridorCurve(polyline).points : polyline;
      const score = scoreCandidate(route, candidate, points, polyline.length - 2, nodes, routed, bounds);
      if (!best || score < best.score) best = { candidate, points, score };
    }
    if (best) {
      route.candidate = best.candidate;
      route.points = best.points;
      route.source = best.candidate.sourceEnd ?? route.source;
      route.target = best.candidate.targetEnd ?? route.target;
    } else {
      // No route fits (a canvas too small for any stub): a straight line between the facing sides.
      route.mode = "straight";
      route.straight = straightSides(route.source, route.target);
      route.points = straightPoints(route.source, route.target, route.straight, 0, 0);
    }
    routed.push(route);
  }

  // 2. Lanes: edges sharing a node side - a straight tie's rectangular end
  //    included - are spread evenly around its centre, ordered by where they
  //    come from, then where they go and, for two edges to the same place, by
  //    the side of travel — an exchange in both directions runs as two
  //    parallel lines, never as a cross, and an elbow never lands on a tie.
  //    The edges of a pair running between two facing sides take one offset
  //    each, the same on both nodes; a zone's border spreads nothing.
  const anchors = new Map<string, AnchorRef[]>();
  for (const route of routed) {
    for (const end of ["source", "target"] as const) {
      const side = routeSide(route, end);
      if (!side || zoneOfEnd(route, end)) continue;
      const node = end === "source" ? route.source : route.target;
      const far = end === "source" ? route.target : route.source;
      const out = OUT[side];
      // Travel direction at the anchor: leaving along the side's normal, or
      // arriving against it. Right-of-travel in screen coordinates.
      const travel = end === "source" ? out : { x: -out.x, y: -out.y };
      const normal = { x: -travel.y, y: travel.x };
      const horizontalSide = side === "top" || side === "bottom";
      const adjacent = end === "source" ? route.points[1] : route.points[route.points.length - 2];
      const sortKey: [number, number, number] = [
        horizontalSide ? adjacent.x : adjacent.y,
        horizontalSide ? far.cx : far.cy,
        horizontalSide ? normal.x : normal.y,
      ];
      const key = `${node.id}:${side}`;
      const list = anchors.get(key) ?? [];
      list.push({ route, end, side, sortKey, offset: 0 });
      anchors.set(key, list);
    }
  }
  for (const list of anchors.values()) {
    list.sort((a, b) => a.sortKey[0] - b.sortKey[0] || a.sortKey[1] - b.sortKey[1] || a.sortKey[2] - b.sortKey[2]);
  }
  const { fixed, aligns } = alignPairs(routed, anchors);
  for (const list of anchors.values()) spreadAnchors(list, fixed);
  const offsetOf = (route: Route, end: "source" | "target"): number => {
    const side = routeSide(route, end);
    if (!side) return 0;
    if (zoneOfEnd(route, end)) {
      // A zone end follows its node's lane straight across when the two face each other.
      const other = end === "source" ? "target" : "source";
      return routeSide(route, other) === OPPOSITE[side] ? offsetOf(route, other) : 0;
    }
    const node = end === "source" ? route.source : route.target;
    const list = anchors.get(`${node.id}:${side}`) ?? [];
    return list.find((anchor) => anchor.route === route && anchor.end === end)?.offset ?? 0;
  };

  // 3. Final geometry from the laned anchors; labels placed shortest-edge first,
  //    so the label with the least room takes its midpoint and the one with the
  //    most room is the one that slides along its line to avoid it.
  const edges: LayoutEdge[] = [];
  const placedLabels: Rect[] = [];
  const geometry = routed.map((route) => {
    let d: string;
    // Where a label may sit, for a `w` x `h` pill: on the line first, beside it after.
    let spots: (w: number, h: number) => LabelSpot[];
    const align = aligns.get(route) ?? null;
    if (route.mode === "straight" || !route.candidate) {
      if (route.straight) {
        route.points = straightPoints(
          route.source,
          route.target,
          route.straight,
          offsetOf(route, "source"),
          offsetOf(route, "target"),
          align,
        );
      }
      d = roundedPath(route.points);
      spots = (w, h) => labelSpots(route.points, w, h, headsOf(route.edge));
    } else if (route.mode === "curve") {
      // The same laned polyline the elbow would draw, curved along it.
      const curve = corridorCurve(
        candidatePoints(route, route.candidate, offsetOf(route, "source"), offsetOf(route, "target"), align),
      );
      route.points = curve.points;
      d = curve.d;
      // On the arc at its stops, the slide off it along the chord's normal.
      const chord = route.points[route.points.length - 1];
      const start = route.points[0];
      const len = Math.hypot(chord.x - start.x, chord.y - start.y) || 1;
      const along = { x: (chord.x - start.x) / len, y: (chord.y - start.y) / len };
      spots = () => LABEL_STOPS.map((u, rank) => ({ at: curve.at(u), along, rank }));
    } else {
      route.points = candidatePoints(route, route.candidate, offsetOf(route, "source"), offsetOf(route, "target"), align);
      d = roundedPath(route.points);
      spots = (w, h) => labelSpots(route.points, w, h, headsOf(route.edge));
    }
    return { route, d, spots };
  });
  const labelOrder = geometry
    .map((entry, index) => ({ index, room: entry.route.edge.label ? length(entry.route.points) : -1 }))
    .filter((entry) => entry.room >= 0)
    .sort((a, b) => a.room - b.room)
    .map((entry) => entry.index);
  const labels = new Map<number, EdgeLabel>();
  for (const index of labelOrder) {
    const { route, spots } = geometry[index];
    const text = layoutText(route.edge.label ?? "", {
      fontSize: ARCH.EDGE_FS,
      lineHeight: ARCH.EDGE_LABEL_H,
      weight: 500,
      maxWidth: ARCH.COL_W + ARCH.COL_GAP,
      maxLines: 1,
    });
    const w = text.width + 2 * ARCH.EDGE_LABEL_PAD;
    const h = ARCH.EDGE_LABEL_H;
    const onLine = spots(w, h);
    const at = placeLabel([...onLine, ...labelSlides(onLine, w, h)], w, h, nodes, placedLabels, bounds, route.points);
    const label: EdgeLabel = { text, x: at.x - w / 2, y: at.y - h / 2, w, h };
    placedLabels.push(label);
    labels.set(index, label);
  }
  geometry.forEach(({ route, d }, index) => {
    const label = labels.get(index) ?? null;
    edges.push({
      key: `${route.edge.source}->${route.edge.target}#${route.index}`,
      edge: route.edge,
      tone: effectiveEdgeTone(route.edge),
      arrows: route.edge.arrows ?? "end",
      dashed: route.edge.dashed === true,
      d,
      points: route.points,
      label,
    });
  });
  return edges;
}

// ── The whole picture ─────────────────────────────────────────────────────────

/** The title row's room a zone logo keeps above and below itself, so it never meets the nodes under it. */
const ZONE_LOGO_MARGIN = 12;
/** The gap between a zone logo and the label after it. */
const ZONE_LOGO_GAP = 12;
/** The share of the title row a zone logo may take when a label follows it. */
const ZONE_LOGO_SHARE_WITH_LABEL = 0.6;
/** The gap a zone's chips keep from its logo / label. */
const ZONE_CHIPS_GAP = 16;
/** The sizes a zone's chip row is tried at, in order: a node chip's, then one step down. */
const ZONE_CHIP_SIZES = [
  { fontSize: ARCH.CHIP_FS, h: ARCH.CHIP_H, pad: ARCH.CHIP_PAD, gap: ARCH.CHIP_GAP },
  { fontSize: 15, h: 26, pad: 10, gap: 6 },
] as const;

/** Where a zone's chips may go on its title row: its padding, and the end of its logo / label (`null` when it has neither). */
interface ZoneHeadRoom {
  left: number;
  right: number;
  headEnd: number | null;
}

/**
 * A zone's chips: ONE row on the title row, centred on `titleY`, at
 * the zone's end, its start (after the logo / label) or its centre. Never
 * wrapped and never cut: a row that meets the head or runs past the padding
 * at a node chip's size is tried one size down, and one that still does not
 * fit is drawn whole where its alignment puts it, `overflow` saying by how
 * much - the layout check reports it.
 */
function layoutZoneChips(
  labels: readonly string[],
  align: DeckArchitectureZone["chips_align"],
  room: ZoneHeadRoom,
  titleY: number,
): Pick<LayoutZone, "chips" | "chipsBox" | "chipsOverflow"> {
  const shown = labels.filter(Boolean);
  if (!shown.length) return { chips: [], chipsBox: null, chipsOverflow: 0 };
  const floor = room.headEnd === null ? room.left : room.headEnd + ZONE_CHIPS_GAP;
  let row: Pick<LayoutZone, "chips" | "chipsBox" | "chipsOverflow"> | null = null;
  for (const size of ZONE_CHIP_SIZES) {
    const texts = shown.map((label) =>
      layoutText(label, {
        fontSize: size.fontSize,
        lineHeight: size.h,
        weight: 500,
        maxWidth: Number.POSITIVE_INFINITY,
        maxLines: 1,
      }),
    );
    const widths = texts.map((text) => text.width + 2 * size.pad);
    const rowW = widths.reduce((sum, w) => sum + w, 0) + (widths.length - 1) * size.gap;
    const start = align === "start" ? floor : align === "center" ? (room.left + room.right - rowW) / 2 : room.right - rowW;
    const y = titleY - size.h / 2;
    let x = start;
    const chips = texts.map((text, index) => {
      const chip = { text, x, y, w: widths[index], h: size.h };
      x += widths[index] + size.gap;
      return chip;
    });
    row = {
      chips,
      chipsBox: { x: start, y, w: rowW, h: size.h },
      chipsOverflow: Math.max(0, floor - start, start + rowW - room.right),
    };
    if (row.chipsOverflow <= FIT_TOLERANCE) return { ...row, chipsOverflow: 0 };
  }
  return row!;
}

/** Is `node` a one-column card whose icon is drawn in its tile above the label? */
const isTiledIconCard = (node: DeckArchitectureNode) =>
  nodeKind(node) === "card" && !node.logo_file_id && !!node.icon && node.icon_tile !== false;

/** Is `node` a one-column card drawing a logo sized by `logo_height`? */
const isSizedLogoCard = (node: DeckArchitectureNode) => nodeKind(node) === "card" && !!node.logo_file_id && !!node.logo_height;

/**
 * The media height every card of a row shares when the row mixes logo cards
 * sized by `logo_height` and tiled-icon cards: the taller of the icon tile and
 * the row's sized marks (`rowLogoH`), so the tile grows with the row's step
 * and the cards end up one height. A row without both kinds is absent: its
 * geometry is unchanged (a row of unsized logos and icons included).
 */
function mixedRowMediaH(block: DeckArchitectureBlock, rowLogoH: ReadonlyMap<number, number>): Map<number, number> {
  const icons = new Set<number>();
  const logos = new Set<number>();
  for (const node of block.nodes) {
    if (isTiledIconCard(node)) icons.add(node.row);
    else if (isSizedLogoCard(node)) logos.add(node.row);
  }
  const shared = new Map<number, number>();
  for (const row of icons) {
    if (logos.has(row)) shared.set(row, Math.max(ARCH.ICON_TILE, rowLogoH.get(row) ?? 0));
  }
  return shared;
}

/** Is `node` a one-column card whose logo is sized by `logo_height`? */
const isSizedCard = (node: DeckArchitectureNode, assets: ArchitectureAssets | undefined) =>
  nodeKind(node) === "card" && !!nodeLogoSize(node, assets);

/** The number of grid columns `block` spans. */
const columnsOf = (block: DeckArchitectureBlock) => Math.max(1, ...block.nodes.map((node) => node.col + span(node) - 1));

/**
 * The widest a sized column may grow: in a room, as wide as keeps the whole
 * drawing's fit scale at `SIZED_FIT_MIN` or more on the `SIZED_COL_GAP` grid
 * (the labels stay readable); without one, `SIZED_COL_W_MAX`.
 */
function sizedColCap(block: DeckArchitectureBlock, roomWidth: number | null): number {
  if (!roomWidth) return SIZED_COL_W_MAX;
  const cols = columnsOf(block);
  return Math.floor((roomWidth / SIZED_FIT_MIN - 2 * ARCH.PAD - (cols - 1) * SIZED_COL_GAP) / cols);
}

/**
 * The column grid a block's sized card logos ask for at `boost`: each column
 * as wide as the widest sized mark needs to be drawn at its step (plus its
 * inset on both sides), on the narrower `SIZED_COL_GAP` gaps, never narrower
 * than `ARCH.COL_W`, never wider than `sizedColCap`. A block without a sized
 * card, or whose sized marks fit the default column, keeps the default grid.
 */
function gridFor(block: DeckArchitectureBlock, assets: ArchitectureAssets | undefined, boost: number, cap: number): Grid {
  let need = 0;
  for (const node of block.nodes) {
    if (!isSizedCard(node, assets)) continue;
    const size = nodeLogoSize(node, assets, boost)!;
    need = Math.max(need, Math.ceil(size.h * size.ratio + 2 * SIZED_LOGO_INSET));
  }
  if (need <= ARCH.COL_W) return DEFAULT_GRID;
  return { colW: Math.max(ARCH.COL_W, Math.min(cap, need)), colGap: SIZED_COL_GAP };
}

/** The layout at vertical density `v` (1 = the natural metrics), on `grid`, sized logos raised by `logoBoost`. */
function layoutAt(
  block: DeckArchitectureBlock,
  v: number,
  assets: ArchitectureAssets | undefined,
  grid: Grid = DEFAULT_GRID,
  logoBoost = 1,
): ArchitectureLayout {
  const m = metricsAt(v, grid, logoBoost);
  // The sized cards of one row share one media box: the row's tallest drawn mark.
  const rowLogoH = new Map<number, number>();
  for (const node of block.nodes) {
    if (!isSizedCard(node, assets)) continue;
    const drawn = cardLogoDrawnH(nodeLogoSize(node, assets, logoBoost)!, m.colW);
    rowLogoH.set(node.row, Math.max(rowLogoH.get(node.row) ?? 0, drawn));
  }
  // A row that mixes logo and icon cards: one media size, then one card height.
  const rowMediaH = mixedRowMediaH(block, rowLogoH);
  const measureAll = (rowContentH?: Map<number, number>) =>
    block.nodes.map((node) =>
      measureNode(node, m, assets, rowLogoH.get(node.row), rowMediaH.get(node.row), rowContentH?.get(node.row)),
    );
  let measured = measureAll();
  if (rowMediaH.size) {
    const rowContentH = new Map<number, number>();
    measured.forEach((node, i) => {
      const row = block.nodes[i].row;
      if (node.kind !== "card" || !rowMediaH.has(row)) return;
      rowContentH.set(row, Math.max(rowContentH.get(row) ?? 0, node.h - 2 * m.cardPadY));
    });
    measured = measureAll(rowContentH);
  }
  const rows = Math.max(1, ...block.nodes.map((node) => node.row));
  const cols = Math.max(1, ...block.nodes.map((node) => node.col + span(node) - 1));
  const zones = block.zones ?? [];

  const colX = (col: number) => ARCH.PAD + (col - 1) * (m.colW + m.colGap);
  const rowHeights = new Array<number>(rows + 1).fill(0);
  measured.forEach((node, i) => {
    const row = block.nodes[i].row;
    rowHeights[row] = Math.max(rowHeights[row], node.h);
  });
  const headRows = new Set(zones.map((zone) => zone.row));
  const rowStart = new Array<number>(rows + 1).fill(0);
  const nodesTop = new Array<number>(rows + 1).fill(0);
  // The free corridors between the rows: the centre of each gap, measured
  // from the row above's nodes to the next row's start (a zone's title band
  // belongs to the row it heads, so a corridor never runs through a title).
  const rowGaps: number[] = [];
  let y: number = m.padY;
  for (let row = 1; row <= rows; row += 1) {
    const head = headRows.has(row) ? m.zoneHead : 0;
    const height = rowHeights[row] || m.emptyRowH;
    rowHeights[row] = height;
    if (row > 1) rowGaps.push((nodesTop[row - 1] + rowHeights[row - 1] + y) / 2);
    rowStart[row] = y;
    nodesTop[row] = y + head;
    y += head + height + (row < rows ? m.rowGap : 0);
  }
  const gridBottom = y;
  const width = 2 * ARCH.PAD + spanWidth(cols, m);
  const colGaps: number[] = [];
  for (let col = 1; col < cols; col += 1) colGaps.push(colX(col) + m.colW + m.colGap / 2);

  const nodes: LayoutNode[] = measured.map((node, i) => {
    const spec = block.nodes[i];
    const cellW = spanWidth(span(spec), m);
    const x = colX(spec.col) + (cellW - node.w) / 2;
    const top = nodesTop[spec.row] + (rowHeights[spec.row] - node.h) / 2;
    const anchor = node.frame
      ? { x, y: top, w: node.w, h: node.h }
      : { x: x + node.content.x, y: top + node.content.y, w: node.content.w, h: node.content.h };
    return { ...node, x, y: top, cx: x + node.w / 2, cy: top + node.h / 2, anchor };
  });

  const layoutZones: LayoutZone[] = zones.map((zone) => {
    const rowSpan = Math.max(1, zone.row_span ?? 1);
    const colSpan = Math.max(1, zone.col_span ?? 1);
    const lastRow = Math.min(rows, zone.row + rowSpan - 1);
    const lastCol = Math.min(cols, zone.col + colSpan - 1);
    const x = colX(zone.col) - ARCH.ZONE_INSET;
    const right = colX(lastCol) + m.colW + ARCH.ZONE_INSET;
    const top = rowStart[zone.row] - 10;
    const bottom = nodesTop[lastRow] + rowHeights[lastRow] + ARCH.ZONE_INSET;
    const headX = x + 24;
    const titleY = top + 10 + m.zoneHead / 2 - 4;
    const headW = right - x - 48;
    // The zone logo sits where the icon would, as tall as the title row
    // leaves it and as wide as its mark's ratio; the label follows it.
    let logo: Rect | null = null;
    if (zone.logo_file_id) {
      const ratio = markRatio(assets?.[zone.logo_file_id]);
      const height = Math.min(ARCH.ZONE_LOGO_H, m.zoneHead - ZONE_LOGO_MARGIN);
      const hundredths = (n: number) => Math.round(n * 100) / 100;
      const w = hundredths(Math.min(height * ratio, zone.label ? headW * ZONE_LOGO_SHARE_WITH_LABEL : headW));
      const h = hundredths(w / ratio);
      logo = { x: headX, y: titleY - h / 2, w, h };
    }
    const markW = logo ? logo.w + ZONE_LOGO_GAP : zone.icon ? ARCH.ZONE_ICON + 10 : 0;
    const title = zone.label
      ? layoutText(zone.label, {
          fontSize: ARCH.ZONE_FS,
          lineHeight: ARCH.ZONE_HEAD - 16,
          weight: 600,
          maxWidth: Math.max(1, headW - markW),
          maxLines: 1,
        })
      : null;
    const headEnd = title ? headX + markW + title.width : logo ? logo.x + logo.w : zone.icon ? headX + ARCH.ZONE_ICON : null;
    return {
      zone,
      x,
      y: top,
      w: right - x,
      h: bottom - top,
      title,
      iconX: headX,
      titleX: headX + markW,
      titleY,
      logo,
      ...layoutZoneChips(zone.chips ?? [], zone.chips_align, { left: headX, right: headX + headW, headEnd }, titleY),
    };
  });

  const chipRows = layoutZones.flatMap((zone) => (zone.chipsBox ? [zone.chipsBox] : []));
  const edges = routeEdges(block, nodes, { width, gridBottom, rowGaps, colGaps, zones: layoutZones, chipRows });
  // A detour under the last row, or a label on it, needs room of its own.
  let drawnBottom = gridBottom;
  for (const edge of edges) {
    for (const point of edge.points) drawnBottom = Math.max(drawnBottom, point.y + 8);
    if (edge.label) drawnBottom = Math.max(drawnBottom, edge.label.y + edge.label.h);
  }

  const legendItems = block.legend ?? [];
  const legend: LegendItemLayout[] = [];
  let height = drawnBottom + m.padY;
  if (legendItems.length) {
    const measuredItems = legendItems.map((item) => ({
      item,
      text: layoutText(item.label, {
        fontSize: ARCH.LEGEND_FS,
        lineHeight: ARCH.LEGEND_H,
        weight: 400,
        maxWidth: width / 2,
        maxLines: 1,
      }),
    }));
    // Items are packed into centred rows within the drawing's width: a legend
    // wider than a narrow diagram wraps instead of running past the canvas.
    const itemWidth = (item: (typeof measuredItems)[number]) => ARCH.LEGEND_MARKER_W + 12 + item.text.width;
    const available = width - 2 * ARCH.PAD;
    const rows: (typeof measuredItems)[] = [[]];
    let rowWidth = 0;
    for (const item of measuredItems) {
      const w = itemWidth(item);
      const row = rows[rows.length - 1];
      if (row.length && rowWidth + ARCH.LEGEND_GAP + w > available) {
        rows.push([item]);
        rowWidth = w;
      } else {
        row.push(item);
        rowWidth += (row.length > 1 ? ARCH.LEGEND_GAP : 0) + w;
      }
    }
    const legendY = drawnBottom + m.legendGap;
    rows.forEach((row, rowIndex) => {
      const total = row.reduce((sum, item) => sum + itemWidth(item), 0) + (row.length - 1) * ARCH.LEGEND_GAP;
      let x = Math.max(ARCH.PAD, (width - total) / 2);
      const y = legendY + rowIndex * ARCH.LEGEND_H;
      for (const item of row) {
        const marker = item.item.marker ?? "arrow";
        legend.push({
          item: item.item,
          marker,
          tone: item.item.tone ?? (marker === "arrow" || marker === "node" ? "accent" : "muted"),
          x,
          y,
          text: item.text,
          textX: x + ARCH.LEGEND_MARKER_W + 12,
        });
        x += itemWidth(item) + ARCH.LEGEND_GAP;
      }
    });
    height = legendY + rows.length * ARCH.LEGEND_H + m.padY;
  }

  const neighbours = new Map<string, Set<string>>();
  for (const edge of edges) {
    const a = neighbours.get(edge.edge.source) ?? new Set<string>();
    a.add(edge.edge.target);
    neighbours.set(edge.edge.source, a);
    const b = neighbours.get(edge.edge.target) ?? new Set<string>();
    b.add(edge.edge.source);
    neighbours.set(edge.edge.target, b);
  }

  return { width, height, naturalHeight: height, density: v, nodes, zones: layoutZones, edges, legend, neighbours };
}

/** Density steps are hundredths: finer than the eye, coarse enough to settle in a few probes. */
const DENSITY_STEP = 0.01;
/**
 * Passes that settle a sized logo's boost against the room's shrink: a wider
 * column shrinks the drawing, which asks for a larger boost, converging
 * geometrically (a factor near 0.8 per pass for five wordmark columns).
 */
const LOGO_FIT_PASSES = 60;
/** The boost is taken 1% over the room's shrink so the column's rounding never leaves a mark under its step. */
const LOGO_FIT_MARGIN = 1.01;
/** Half a pixel of overflow is invisible (the metrics are whole pixels). */
const FIT_TOLERANCE = 0.5;

/**
 * The drawing for `block`, laid out for the `room` it has when one is given.
 * The drawing is never magnified: the room's width only tells how
 * far it is SHRUNK when the drawing is wider than its box, which is that much
 * more height for the rows. A drawing taller than that budget at 1:1 is
 * compacted - the largest density in `[DENSITY_MIN, 1]` whose layout fits,
 * found by bisection on the (monotone) height - so its rows close up before
 * the slide's shrink-to-fit scales the type; past the floor it is handed over
 * as it stands. Without a room, or inside it, the natural layout. `assets` is
 * the stored geometry of the logos it draws (see the module comment).
 */
export function layoutArchitecture(
  block: DeckArchitectureBlock,
  options?: { room?: ArchitectureRoom | null; assets?: ArchitectureAssets },
): ArchitectureLayout {
  const assets = options?.assets;
  const room = options?.room;
  // A sized logo is asked for in slide px: when the room shrinks the drawing,
  // the logos (and the columns they need) are raised by the inverse of that
  // shrink, a few passes until the grid settles. No sized logo: one pass, as before.
  // The column cap keeps the drawing's fit at `SIZED_FIT_MIN` or more, so a
  // wide wordmark never shrinks every label of the block below 16 px.
  const cap = sizedColCap(block, room?.width && room.width > 0 ? room.width : null);
  let boost = 1;
  let grid = gridFor(block, assets, boost, cap);
  const sized = block.nodes.some((node) => nodeKind(node) !== "pill" && nodeKind(node) !== "circle" && !!nodeLogoSize(node, assets));
  if (sized && room?.width && room.width > 0) {
    for (let pass = 0; pass < LOGO_FIT_PASSES; pass += 1) {
      const width = 2 * ARCH.PAD + spanWidth(columnsOf(block), grid);
      const next = LOGO_FIT_MARGIN / Math.min(1, room.width / width);
      if (Math.abs(next - boost) < 0.0005) break;
      boost = next;
      grid = gridFor(block, assets, boost, cap);
    }
  }
  const natural = layoutAt(block, 1, assets, grid, boost);
  if (!room || !(room.height > 0)) return natural;
  const scale = room.width && room.width > 0 ? Math.min(1, room.width / natural.width) : 1;
  const budget = room.height / scale + FIT_TOLERANCE;
  if (natural.height <= budget) return natural;
  const withNatural = (layout: ArchitectureLayout): ArchitectureLayout => ({ ...layout, naturalHeight: natural.height });
  let best = layoutAt(block, DENSITY_MIN, assets, grid, boost);
  if (best.height > budget) return withNatural(best);
  let lo = DENSITY_MIN;
  let hi = 1;
  while (hi - lo > DENSITY_STEP + 1e-9) {
    const mid = Math.round(((lo + hi) / 2) / DENSITY_STEP) * DENSITY_STEP;
    if (mid <= lo || mid >= hi) break;
    const candidate = layoutAt(block, mid, assets, grid, boost);
    if (candidate.height <= budget) {
      lo = mid;
      best = candidate;
    } else {
      hi = mid;
    }
  }
  return withNatural(best);
}
