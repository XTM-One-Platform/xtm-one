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
 * The room of a pie or a donut: how large its disc is drawn and where
 * its values are written. Pure - no React, no Recharts, no DOM - so the
 * drawing (`ChartCanvas.tsx`), the facts it publishes for the exporter and the
 * layout audit, and the tests read ONE decision, as `chart-layout.ts` is for
 * an axis chart, whose measuring it shares.
 *
 * A value is written outside its slice, at the end of a leader from the rim:
 * `LEADER` px out along the slice's middle, anchored on the leader's end and
 * reading away from the disc, where Recharts writes a pie's label. The ring
 * around the disc is sized from the values: each one's run - estimated as the
 * axis charts estimate a run that meets the canvas edge (`labelRun`) - at its
 * own angle across, its height along, never more than the `VALUE_RING` every
 * pie kept before its values were measured, so a pie whose values fit keeps
 * its radius. While that ring leaves the disc under its minimum, or while two
 * values would touch, the values are tried a size down the chart's text scale
 * (`label_size`, `chartTextScale`); the larger slices are written first, so a
 * value given up is a small slice's. When even the smallest size leaves the
 * disc no room, the disc takes the canvas and the values are written INSIDE
 * the slices large enough to hold them at that size, centred, inked from the
 * slice's colour as a stack's segment value is; a slice too small keeps none.
 * No value crosses the canvas edge. What is not written is counted with the
 * reason (`narrow`), and values set under the size asked say why (`shrunk`):
 * the layout check reports both, as it does for an axis chart.
 */
import type { DeckChartBlock } from "@/types";

import { chartValueWriter, MAX_LABELLED_VALUES, paletteColor, valuesAsked, valuesShown, writtenValues } from "./chart-format";
import {
  chartTextScale,
  EDGE_PAD,
  grown,
  INK_EM,
  LABEL_HALO,
  labelInk,
  labelRun,
  overlap,
  type LabelPlacement,
  type Rect,
  type ValuePlan,
} from "./chart-layout";

/** Where the sweep starts and ends (Recharts' `startAngle` / `endAngle`): 12 o'clock, clockwise. */
export const START_ANGLE = 90;
export const END_ANGLE = -270;
/** The room between two slices (Recharts' `paddingAngle`, degrees). */
const PIE_PADDING_ANGLE = 1;
const DONUT_PADDING_ANGLE = 2;
/** A donut's hole, as a share of its radius (`_DONUT_HOLE_PERCENT` in `pptx_writer.py`). */
export const DONUT_HOLE = 0.62;
/** The ring a disc keeps inside the canvas with no values around it (canvas px). */
const BARE_RING = 12;
/** The ring every pie kept for its values before they were measured (canvas px): the most one is given. */
const VALUE_RING = 64;
/** A disc is never drawn under this radius (canvas px). */
const MIN_RADIUS = 24;
/**
 * The values stay outside while the disc keeps this share of the radius it
 * has with no values around it, and this many px - never more than the
 * radius the ring of every pie left it before (`VALUE_RING`).
 */
const MIN_DISC_SHARE = 0.5;
const MIN_DISC_PX = 48;
/** The leader from the rim to a value written outside (canvas px): Recharts' `offsetRadius`. */
export const LEADER = 20;
/** The stroke of a leader (canvas px). */
export const LEADER_STROKE = 2;
/** A slice whose middle is this near the vertical writes its value centred over (or under) its leader's end. */
const UPRIGHT_COS = 1e-6;
/**
 * Where a value inside its slice may sit, along the slice's middle, in the
 * order they are tried: a share of a pie's radius, of a donut's ring from its
 * hole - the first is where PowerPoint centres a slice's label.
 */
const INSIDE_PIE = [0.6, 0.5, 0.7, 0.4, 0.8, 0.3] as const;
const INSIDE_RING = [0.5, 0.35, 0.65, 0.2, 0.8] as const;
/** The clear room a value inside its slice keeps from the rim, the hole and the slice's sides (canvas px). */
const INSIDE_PAD = 4;
/** The step a value's box is walked along its edges when it is tried inside a slice (canvas px). */
const INSIDE_SAMPLE = 2;
/** How far past the disc's radius the radius a value needs inside its slice is looked for, and in how many halvings. */
const INSIDE_NEED_REACH = 8;
const INSIDE_NEED_STEPS = 16;
/** Recharts' degrees to radians (`RADIAN`): the same product, so a label's side reads as Recharts' does. */
const RADIAN = Math.PI / 180;
/** What a box solved to meet the canvas edge exactly may overshoot it by in floating point (canvas px). */
const ROUNDING = 0.01;

type Point = readonly [number, number];

/** One category of a pie or a donut as Recharts draws it. */
export interface PieSlice {
  name: string;
  value: number;
  color: string;
  /** The category's position in the block (the legend's, the palette's). */
  index: number;
}

/** The categories the legend leaves visible, a negative value drawn as none. */
export function buildSlices(block: DeckChartBlock, hidden: ReadonlySet<number>): PieSlice[] {
  const values = block.series[0]?.values ?? [];
  return block.categories
    .map((name, index) => ({ name, value: Math.max(values[index] ?? 0, 0), color: paletteColor(index), index }))
    .filter((slice) => !hidden.has(slice.index));
}

/** A slice's sweep (degrees, Recharts' convention: counter-clockwise from 3 o'clock, `start` > `end`). */
interface SliceArc {
  index: number;
  value: number;
  startAngle: number;
  endAngle: number;
  midAngle: number;
}

/**
 * The sectors Recharts draws (`computePieSectors`): each slice its share of
 * the sweep less the padding between non-zero slices, from 12 o'clock.
 */
export function sliceArcs(slices: readonly PieSlice[], paddingAngle: number): SliceArc[] {
  const sum = slices.reduce((total, slice) => total + slice.value, 0);
  if (sum <= 0) return [];
  const padding = slices.length <= 1 ? 0 : paddingAngle;
  const sweep = Math.abs(END_ANGLE - START_ANGLE) - slices.filter((slice) => slice.value !== 0).length * padding;
  let previous: number | null = null;
  return slices.map((slice) => {
    const startAngle = previous === null ? START_ANGLE : previous - (slice.value !== 0 ? padding : 0);
    const endAngle = startAngle - (slice.value / sum) * sweep;
    previous = endAngle;
    return { index: slice.index, value: slice.value, startAngle, endAngle, midAngle: (startAngle + endAngle) / 2 };
  });
}

/** A disc around `(cx, cy)`: its radius and its hole's (0 for a pie), canvas px. */
interface SliceGeometry {
  cx: number;
  cy: number;
  outer: number;
  inner: number;
}

/** Recharts' `polarToCartesian`. */
const polar = (cx: number, cy: number, radius: number, angle: number): Point => [
  cx + Math.cos(-RADIAN * angle) * radius,
  cy + Math.sin(-RADIAN * angle) * radius,
];

/** One value on the figure. */
export interface SliceValue {
  text: string;
  placement: LabelPlacement;
  /** Written inside its slice, inked from the slice's colour; else outside, at the end of `leader`. */
  inside: boolean;
  /** The leader from the rim to the value's anchor (canvas px); `null` inside. */
  leader: readonly [Point, Point] | null;
}

/**
 * The values set under the size asked: the size asked (the chart's
 * tick size), the radius the disc would keep with the values outside at that
 * size and the least it keeps (canvas px). A radius at or over the least
 * means the values at that size would touch one another.
 */
interface ShrunkValues {
  size: number;
  room: number;
  need: number;
}

/** Everything `ChartCanvas` hands Recharts about a pie or a donut, and what it publishes. */
export interface SliceChartLayout {
  slices: PieSlice[];
  cx: number;
  cy: number;
  outerRadius: number;
  innerRadius: number;
  paddingAngle: number;
  /**
   * Where the values are written - outside the disc, or inside the slices -
   * or were tried when none fits; `null` when the block writes none.
   */
  placement: "outside" | "inside" | null;
  /** Each written value, by its slice's category. */
  labels: ReadonlyMap<number, SliceValue>;
  values: ValuePlan;
  /** The share of the whole of the smallest slice whose value is not written (`narrow`); `null` otherwise. */
  hiddenShare: number | null;
  shrunk: ShrunkValues | null;
}

/**
 * Whether the segment from `a` to `b` crosses `rect` (Liang-Barsky): a leader
 * that runs through another slice's value.
 */
function crosses(rect: Rect, [ax, ay]: Point, [bx, by]: Point): boolean {
  const dx = bx - ax;
  const dy = by - ay;
  let low = 0;
  let high = 1;
  const edges: [number, number][] = [
    [-dx, ax - rect.left],
    [dx, rect.right - ax],
    [-dy, ay - rect.top],
    [dy, rect.bottom - ay],
  ];
  for (const [p, q] of edges) {
    if (p === 0) {
      if (q < 0) return false;
      continue;
    }
    const t = q / p;
    if (p < 0) low = Math.max(low, t);
    else high = Math.min(high, t);
    if (low > high) return false;
  }
  return true;
}

/** Where a value outside its slice is written: Recharts' label on the leader's end. */
function outsidePlacement(arc: SliceArc, cx: number, cy: number, radius: number): { placement: LabelPlacement; leader: [Point, Point] } {
  const rim = polar(cx, cy, radius, arc.midAngle);
  const end = polar(cx, cy, radius + LEADER, arc.midAngle);
  const upright = Math.abs(Math.cos(-RADIAN * arc.midAngle)) < UPRIGHT_COS;
  const placement: LabelPlacement = upright
    ? { x: end[0], y: end[1], anchor: "middle", baseline: end[1] > cy ? "hanging" : "auto", rotate: false }
    : { x: end[0], y: end[1], anchor: end[0] > cx ? "start" : "end", baseline: "central", rotate: false };
  return { placement, leader: [rim, end] };
}

/**
 * The largest radius at which every value written outside stays inside the
 * canvas at `size`: each one's run past its leader's end at its own angle
 * across, its height along (centred on the leader's end, past it for a slice
 * whose middle is vertical).
 */
function outsideRadius(arcs: readonly SliceArc[], texts: ReadonlyMap<number, string>, size: number, width: number, height: number): number {
  const across = width / 2 - EDGE_PAD;
  const along = height / 2 - EDGE_PAD;
  const ink = size * INK_EM;
  let radius = Infinity;
  for (const arc of arcs) {
    const text = texts.get(arc.index);
    if (text === undefined) continue;
    const run = labelRun(text, size);
    const cos = Math.abs(Math.cos(-RADIAN * arc.midAngle));
    const sin = Math.abs(Math.sin(-RADIAN * arc.midAngle));
    if (cos < UPRIGHT_COS) {
      radius = Math.min(radius, run / 2 <= across ? along - ink - LEADER : -Infinity);
      continue;
    }
    radius = Math.min(radius, (across - run) / cos - LEADER);
    if (sin > 0) radius = Math.min(radius, (along - ink / 2) / sin - LEADER);
    else if (ink / 2 > along) radius = -Infinity;
  }
  return radius;
}

/** Whether `point` lies inside the slice `arc` between radii `inner` and `outer` around `(cx, cy)`. */
function inSlice([x, y]: Point, cx: number, cy: number, inner: number, outer: number, arc: SliceArc): boolean {
  const distance = Math.hypot(x - cx, y - cy);
  if (distance < inner || distance > outer) return false;
  const span = arc.startAngle - arc.endAngle;
  if (span >= 360) return true;
  const angle = Math.atan2(cy - y, x - cx) / RADIAN;
  const from = (((arc.startAngle - angle) % 360) + 360) % 360;
  return from <= span;
}

/** Points along the edges of `rect`, `INSIDE_SAMPLE` px apart at most. */
function perimeter(rect: Rect): Point[] {
  const points: Point[] = [];
  const across = Math.max(1, Math.ceil((rect.right - rect.left) / INSIDE_SAMPLE));
  const along = Math.max(1, Math.ceil((rect.bottom - rect.top) / INSIDE_SAMPLE));
  for (let step = 0; step <= across; step += 1) {
    const x = rect.left + ((rect.right - rect.left) * step) / across;
    points.push([x, rect.top], [x, rect.bottom]);
  }
  for (let step = 1; step < along; step += 1) {
    const y = rect.top + ((rect.bottom - rect.top) * step) / along;
    points.push([rect.left, y], [rect.right, y]);
  }
  return points;
}

/**
 * A value's ink as it meets the canvas edge: its run keeps the excess of its
 * widest glyph (`labelRun`) on the side it reads toward.
 */
function runBox(placement: LabelPlacement, ink: Rect, text: string, size: number): Rect {
  const excess = labelRun(text, size) - (ink.right - ink.left);
  const { anchor } = placement;
  return {
    left: ink.left - (anchor === "end" ? excess : anchor === "middle" ? excess / 2 : 0),
    right: ink.right + (anchor === "start" ? excess : anchor === "middle" ? excess / 2 : 0),
    top: ink.top,
    bottom: ink.bottom,
  };
}

/** Whether `rect` stays `EDGE_PAD` inside the canvas, give or take the rounding of the radius it was solved for. */
const onCanvas = (rect: Rect, width: number, height: number): boolean =>
  rect.left >= EDGE_PAD - ROUNDING &&
  rect.right <= width - EDGE_PAD + ROUNDING &&
  rect.top >= EDGE_PAD - ROUNDING &&
  rect.bottom <= height - EDGE_PAD + ROUNDING;

/** The room a value takes along the rim at its slice's middle: its box across the tangent there (canvas px). */
const alongRim = (rect: Rect, angle: number): number =>
  Math.abs((rect.right - rect.left) * Math.sin(-RADIAN * angle)) + Math.abs((rect.bottom - rect.top) * Math.cos(-RADIAN * angle));

/** The values outside the disc at `size` and `radius`: the larger slices first, a value that would touch one already written left out. */
function placeOutside(
  arcs: readonly SliceArc[],
  texts: ReadonlyMap<number, string>,
  size: number,
  geometry: { cx: number; cy: number; radius: number; width: number; height: number },
): Map<number, SliceValue> {
  const { cx, cy, radius, width, height } = geometry;
  const written = new Map<number, SliceValue>();
  const inks: Rect[] = [];
  const leaders: [Point, Point][] = [];
  const order = [...arcs].filter((arc) => texts.has(arc.index)).sort((a, b) => b.value - a.value || a.index - b.index);
  for (const arc of order) {
    const text = texts.get(arc.index) ?? "";
    const { placement, leader } = outsidePlacement(arc, cx, cy, radius);
    const ink = labelInk(placement, text, size);
    if (!onCanvas(runBox(placement, ink, text, size), width, height)) continue;
    const halo = grown(ink, LABEL_HALO / 2);
    const clear =
      inks.every((other) => !overlap(halo, grown(other, LABEL_HALO / 2))) &&
      leaders.every(([a, b]) => !crosses(halo, a, b)) &&
      inks.every((other) => !crosses(grown(other, LABEL_HALO / 2), leader[0], leader[1]));
    if (!clear) continue;
    written.set(arc.index, { text, placement, inside: false, leader });
    inks.push(ink);
    leaders.push(leader);
  }
  return written;
}

/**
 * The values inside the slices at `size`: each centred on its slice's middle,
 * at the first place along it where its box and its clear room fit.
 */
function placeInside(arcs: readonly SliceArc[], texts: ReadonlyMap<number, string>, size: number, geometry: SliceGeometry): Map<number, SliceValue> {
  const written = new Map<number, SliceValue>();
  for (const arc of arcs) {
    const text = texts.get(arc.index);
    const placement = text === undefined ? undefined : insideAt(arc, text, size, geometry);
    if (text !== undefined && placement) written.set(arc.index, { text, placement, inside: true, leader: null });
  }
  return written;
}

/**
 * The first place along the middle of the slice `arc` (of a disc of `outer`
 * px around `(cx, cy)`) where `text` at `size` fits with its clear room;
 * `undefined` when none does.
 */
function insideAt(arc: SliceArc, text: string, size: number, geometry: SliceGeometry): LabelPlacement | undefined {
  const { cx, cy, outer, inner } = geometry;
  return insidePlacements(arc, geometry).find((at) =>
    perimeter(grown(labelInk(at, text, size), INSIDE_PAD)).every((point) => inSlice(point, cx, cy, inner, outer, arc)),
  );
}

/**
 * The radius a disc needs for `text` at `size` to fit inside the slice `arc`
 * (canvas px), from `radius` up - the slice keeps its angles, the value its
 * size - capped at `INSIDE_NEED_REACH` times `radius`.
 */
function insideNeed(arc: SliceArc, text: string, size: number, donut: boolean, radius: number): number {
  const fits = (outer: number) => insideAt(arc, text, size, { cx: 0, cy: 0, outer, inner: donut ? outer * DONUT_HOLE : 0 }) !== undefined;
  let low = Math.max(radius, 1);
  let high = low * INSIDE_NEED_REACH;
  if (!fits(high)) return high;
  for (let step = 0; step < INSIDE_NEED_STEPS; step += 1) {
    const middle = (low + high) / 2;
    if (fits(middle)) high = middle;
    else low = middle;
  }
  return high;
}

/** The places a value inside its slice is tried at, along the slice's middle (`INSIDE_PIE`, `INSIDE_RING`). */
function insidePlacements(arc: SliceArc, { cx, cy, outer, inner }: SliceGeometry): LabelPlacement[] {
  const radii = inner > 0 ? INSIDE_RING.map((share) => inner + (outer - inner) * share) : INSIDE_PIE.map((share) => outer * share);
  return radii.map((radius) => {
    const [x, y] = polar(cx, cy, radius, arc.midAngle);
    return { x, y, anchor: "middle", baseline: "central", rotate: false };
  });
}

/**
 * The room of a pie or a donut at `width` x `height` canvas px with the
 * legend's `hidden` slices left out (see the module comment).
 */
export function sliceChartLayout(
  block: DeckChartBlock,
  width: number,
  height: number,
  hidden: ReadonlySet<number>,
  locale: string,
): SliceChartLayout {
  const slices = buildSlices(block, hidden);
  const donut = block.kind === "donut";
  const paddingAngle = donut ? DONUT_PADDING_ANGLE : PIE_PADDING_ANGLE;
  const cx = width / 2;
  const cy = height / 2;
  const half = Math.min(width, height) / 2;
  const bare = Math.max(half - BARE_RING, MIN_RADIUS);
  const showValues = valuesShown(block);
  // A pie past `MAX_LABELLED_VALUES` slices writes none: counted, so the
  // layout check says so instead of a silent drop.
  const asked = valuesAsked(block) ? slices.filter((slice) => slice.value > 0).length : 0;
  const scale = chartTextScale(block);
  const plan = (written: number, size: number | null, narrow: { room: number; need: number } | null): ValuePlan => ({
    outer: size !== null && written > 0 ? { size, rotate: false } : null,
    fit: "auto",
    lifts: null,
    alternate: false,
    inner: null,
    segments: new Set(),
    annotation: null,
    annotations: new Map(),
    thin: null,
    points: null,
    placed: new Map(),
    asked,
    written,
    reason: asked > 0 && !showValues ? "dense" : narrow ? "narrow" : null,
    room: narrow ? narrow.room : MAX_LABELLED_VALUES,
    need: narrow ? narrow.need : block.categories.length,
    turned: null,
  });
  const ring = (radius: number) => ({ outerRadius: radius, innerRadius: donut ? radius * DONUT_HOLE : 0 });
  const none = {
    slices,
    cx,
    cy,
    paddingAngle,
    placement: null,
    labels: new Map<number, SliceValue>(),
    hiddenShare: null,
    shrunk: null,
  } as const;
  if (!showValues || asked === 0) return { ...none, ...ring(bare), values: plan(0, null, null) };

  const arcs = sliceArcs(slices, paddingAngle);
  const write = chartValueWriter(block, writtenValues(block), locale);
  const texts = new Map(arcs.filter((arc) => arc.value > 0).map((arc) => [arc.index, write(arc.value)]));
  const today = Math.max(half - VALUE_RING, MIN_RADIUS);
  const least = Math.min(today, Math.max(MIN_DISC_PX, bare * MIN_DISC_SHARE));
  const radiusAt = (size: number) => Math.min(today, outsideRadius(arcs, texts, size, width, height));

  // Outside, at the first size that writes every value with the disc at its
  // least; else at the size that writes the most of them.
  let best: { size: number; radius: number; written: Map<number, SliceValue> } | null = null;
  for (const size of scale.values) {
    const radius = radiusAt(size);
    if (radius < least) continue;
    const written = placeOutside(arcs, texts, size, { cx, cy, radius, width, height });
    if (!best || written.size > best.written.size) best = { size, radius, written };
    if (written.size === texts.size) break;
  }
  const smallest = scale.values[scale.values.length - 1];
  const chosen = best
    ? { placement: "outside" as const, size: best.size, radius: best.radius, written: best.written }
    : {
        placement: "inside" as const,
        size: smallest,
        radius: bare,
        written: placeInside(arcs, texts, smallest, { cx, cy, outer: bare, inner: donut ? bare * DONUT_HOLE : 0 }),
      };
  const { placement, size, radius, written } = chosen;

  // The smallest slice left without its value, for the layout check. Outside:
  // the room its arc gives along the rim where the value would be written,
  // and what the value takes there. Inside: the disc's radius, and the
  // radius at which the value would fit inside that slice.
  const total = arcs.reduce((sum, arc) => sum + arc.value, 0);
  const missing = arcs.filter((arc) => texts.has(arc.index) && !written.has(arc.index)).sort((a, b) => a.value - b.value || a.index - b.index)[0];
  let narrow: { room: number; need: number } | null = null;
  if (missing && placement === "outside") {
    const box = labelInk(outsidePlacement(missing, cx, cy, radius).placement, texts.get(missing.index) ?? "", size);
    narrow = {
      room: Math.max(0, Math.floor((radius + LEADER) * (missing.startAngle - missing.endAngle) * RADIAN)),
      need: Math.ceil(alongRim(box, missing.midAngle) + LABEL_HALO),
    };
  } else if (missing) {
    narrow = { room: Math.floor(radius), need: Math.ceil(insideNeed(missing, texts.get(missing.index) ?? "", size, donut, radius)) };
  }
  const asking = scale.values[0];
  const shrunk =
    written.size > 0 && size < asking
      ? { size: asking, room: Math.max(0, Math.floor(radiusAt(asking))), need: Math.ceil(least) }
      : null;
  return {
    ...none,
    ...ring(radius),
    placement,
    labels: written,
    values: plan(written.size, size, narrow),
    hiddenShare: missing && total > 0 ? missing.value / total : null,
    shrunk,
  };
}
