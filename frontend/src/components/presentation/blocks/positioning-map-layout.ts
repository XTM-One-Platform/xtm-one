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
 * Placement of a positioning map's labels: every mark at its x / y (0-100)
 * in the plot, then every quadrant label in the first spot of its quadrant
 * that no mark and no axis end label covers (the outer corner first), then
 * every item label beside its mark on the first side that collides with
 * nothing already placed (no label over another label, a mark, a quadrant
 * label, an axis end label, a crossing axis line or the plot's edge). Pure and deterministic: the
 * same items in the same room give the same picture, so the map renders once
 * and stays still (the room is the stage, sized by CSS alone - nothing here
 * feeds back into it).
 *
 * A label that fits nowhere keeps its least-overlapping spot and is counted
 * in `overlaps`, which the figure publishes for the layout check.
 */
import { estimateTextWidth } from "./architecture-layout";

type MapLabelSide = "below" | "above" | "right" | "left";
type MapCorner = "top_left" | "top_right" | "bottom_left" | "bottom_right";

export interface MapLayoutInput {
  x: number;
  y: number;
  label: string;
  sublabel?: string;
  highlight?: boolean;
}

/** The fixed text of the plot the item labels must avoid. */
export interface MapLayoutFrame {
  /** `center`: the end labels sit inside the plot along the crossing axes;
   * `edges`: the y end labels sit inside along the left edge and the x end
   * labels under the plot, outside it (the stylesheet), so no obstacle. */
  axes?: "center" | "edges";
  quadrants?: Partial<Record<MapCorner, string>>;
  ends?: { xLow?: string; xHigh?: string; yLow?: string; yHigh?: string };
}

interface MapRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface MapLayoutItem {
  /** Centre of the mark, in px of the plot. */
  cx: number;
  cy: number;
  /** Side of the mark the mark box spans. */
  mark: number;
  label: MapRect;
  side: MapLabelSide;
  /** The label overlaps something: no side was free. */
  overlap: boolean;
}

interface MapLayoutQuadrant {
  /** The label's box, in px of the plot. */
  rect: MapRect;
  overlap: boolean;
}

export interface MapLayout {
  items: MapLayoutItem[];
  quadrants: Partial<Record<MapCorner, MapLayoutQuadrant>>;
  overlaps: number;
}

/** The mark box of a normal item and of the highlighted one, in canvas px. */
export const MAP_MARK_PX = 40;
export const MAP_HIGHLIGHT_MARK_PX = 60;
const MAP_LABEL_PX = 15;
const MAP_SUBLABEL_PX = 12;
/** Quadrant labels and axis end labels, as `presentation.css` draws them. */
const MAP_QUADRANT_PX = 14;
const MAP_END_PX = 12;
const QUADRANT_PAD_X = 12;
const QUADRANT_PAD_Y = 10;
const LINE = 1.25;
const GAP = 6;

const SIDES: MapLabelSide[] = ["below", "above", "right", "left"];

function intersection(a: MapRect, b: MapRect): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
}

function outside(r: MapRect, width: number, height: number): number {
  const left = Math.max(0, -r.x);
  const right = Math.max(0, r.x + r.w - width);
  const top = Math.max(0, -r.y);
  const bottom = Math.max(0, r.y + r.h - height);
  return (left + right) * r.h + (top + bottom) * r.w;
}

function labelRect(side: MapLabelSide, cx: number, cy: number, mark: number, w: number, h: number): MapRect {
  const half = mark / 2;
  switch (side) {
    case "below":
      return { x: cx - w / 2, y: cy + half + GAP / 2, w, h };
    case "above":
      return { x: cx - w / 2, y: cy - half - GAP / 2 - h, w, h };
    case "right":
      return { x: cx + half + GAP, y: cy - h / 2, w, h };
    case "left":
      return { x: cx - half - GAP - w, y: cy - h / 2, w, h };
  }
}

/** The axis end labels drawn inside the plot, as the stylesheet places them.
 *  On `edges` axes every end label sits outside the plot (x under it, y in the
 *  y title's column), so none is an obstacle. */
function endRects(frame: MapLayoutFrame, width: number, height: number): MapRect[] {
  if (!frame.ends) return [];
  const h = Math.ceil(MAP_END_PX * LINE);
  const w = (text: string) => Math.ceil(estimateTextWidth(text, MAP_END_PX));
  const rects: MapRect[] = [];
  const { xLow, xHigh, yLow, yHigh } = frame.ends;
  if ((frame.axes ?? "center") === "edges") return rects;
  if (xLow) rects.push({ x: 4, y: height / 2 + 6, w: w(xLow), h });
  if (xHigh) rects.push({ x: width - 4 - w(xHigh), y: height / 2 + 6, w: w(xHigh), h });
  if (yLow) rects.push({ x: width / 2 + 8, y: height - 4 - h, w: w(yLow), h });
  if (yHigh) rects.push({ x: width / 2 + 8, y: 4, w: w(yHigh), h });
  return rects;
}

/** Half the band an item label keeps clear around a crossing axis line: the
 *  2 px stroke `presentation.css` draws plus a 3 px clearance on each side. */
const AXIS_CLEAR_PX = 4;

/** The crossing axis lines of `center` axes, as bands no item label may
 *  straddle (UX round 3 B1: a label across the x axis). `edges` axes run along
 *  the plot's border, which `outside` already keeps labels inside of. */
function axisRects(frame: MapLayoutFrame, width: number, height: number): MapRect[] {
  if ((frame.axes ?? "center") === "edges") return [];
  return [
    { x: 0, y: height / 2 - AXIS_CLEAR_PX, w: width, h: AXIS_CLEAR_PX * 2 },
    { x: width / 2 - AXIS_CLEAR_PX, y: 0, w: AXIS_CLEAR_PX * 2, h: height },
  ];
}

/** The spots a quadrant label may take, outer corner first, in px of the plot. */
function quadrantSpots(corner: MapCorner, width: number, height: number, w: number, h: number): MapRect[] {
  const left = corner.endsWith("left");
  const top = corner.startsWith("top");
  const x0 = left ? 0 : width / 2;
  const y0 = top ? 0 : height / 2;
  const outerX = left ? x0 + QUADRANT_PAD_X : x0 + width / 2 - QUADRANT_PAD_X - w;
  const innerX = left ? x0 + width / 2 - QUADRANT_PAD_X - w : x0 + QUADRANT_PAD_X;
  const outerY = top ? y0 + QUADRANT_PAD_Y : y0 + height / 2 - QUADRANT_PAD_Y - h;
  const innerY = top ? y0 + height / 2 - QUADRANT_PAD_Y - h : y0 + QUADRANT_PAD_Y;
  return [
    { x: outerX, y: outerY, w, h },
    { x: innerX, y: outerY, w, h },
    { x: outerX, y: innerY, w, h },
    { x: innerX, y: innerY, w, h },
  ];
}

const CORNERS: MapCorner[] = ["top_left", "top_right", "bottom_left", "bottom_right"];

/** Lay the items out in a plot of `width` x `height` px. */
export function layoutPositioningMap(
  items: MapLayoutInput[],
  width: number,
  height: number,
  frame: MapLayoutFrame = {},
): MapLayout {
  const marks = items.map((item) => {
    const mark = item.highlight ? MAP_HIGHLIGHT_MARK_PX : MAP_MARK_PX;
    const clamp = (v: number, size: number) => Math.min(size - mark / 2, Math.max(mark / 2, v));
    const cx = clamp((Math.min(100, Math.max(0, item.x)) / 100) * width, width);
    const cy = clamp((1 - Math.min(100, Math.max(0, item.y)) / 100) * height, height);
    return { cx, cy, mark };
  });
  const markBoxes: MapRect[] = marks.map(({ cx, cy, mark }) => ({ x: cx - mark / 2, y: cy - mark / 2, w: mark, h: mark }));
  const ends = endRects(frame, width, height);
  let overlaps = 0;

  // The quadrant labels first: they belong to the plot, never to an item, so
  // they leave their corner only for a spot no mark and no end label covers.
  const quadrants: MapLayout["quadrants"] = {};
  const fixed: MapRect[] = [...ends];
  for (const corner of CORNERS) {
    const text = frame.quadrants?.[corner];
    if (!text) continue;
    const w = Math.ceil(estimateTextWidth(text, MAP_QUADRANT_PX, 600));
    const h = Math.ceil(MAP_QUADRANT_PX * LINE);
    let best: { rect: MapRect; cost: number } | null = null;
    for (const rect of quadrantSpots(corner, width, height, w, h)) {
      let cost = 0;
      for (const box of markBoxes) cost += intersection(rect, box);
      for (const other of fixed) cost += intersection(rect, other);
      if (!best || cost < best.cost) best = { rect, cost };
      if (cost === 0) break;
    }
    if (!best) continue;
    const overlap = best.cost > 0;
    if (overlap) overlaps += 1;
    quadrants[corner] = { rect: best.rect, overlap };
    fixed.push(best.rect);
  }

  // The crossing axis lines only bind the item labels: the quadrant spots
  // already keep their padding off both axes.
  const axes = axisRects(frame, width, height);
  const placed: MapRect[] = [...fixed];
  const out: MapLayoutItem[] = new Array(items.length);
  // The highlight first, then top to bottom, left to right: the order a
  // reader scans, so the label a reader sees first keeps its natural place.
  const order = items
    .map((_, index) => index)
    .sort((a, b) => Number(!!items[b].highlight) - Number(!!items[a].highlight) || marks[a].cy - marks[b].cy || marks[a].cx - marks[b].cx);
  for (const index of order) {
    const item = items[index];
    const { cx, cy, mark } = marks[index];
    const size = item.highlight ? MAP_LABEL_PX + 1 : MAP_LABEL_PX;
    const w = Math.ceil(
      Math.max(
        estimateTextWidth(item.label, size, item.highlight ? 600 : 500),
        item.sublabel ? estimateTextWidth(item.sublabel, MAP_SUBLABEL_PX) : 0,
      ),
    );
    const h = Math.ceil(size * LINE + (item.sublabel ? MAP_SUBLABEL_PX * LINE : 0));
    let best: { side: MapLabelSide; rect: MapRect; cost: number; clash: number } | null = null;
    for (const side of SIDES) {
      const rect = labelRect(side, cx, cy, mark, w, h);
      let cost = outside(rect, width, height) * 4;
      for (const other of placed) cost += intersection(rect, other) * 2;
      markBoxes.forEach((box, o) => {
        if (o !== index) cost += intersection(rect, box);
      });
      // Every axis band is charged on every side (UX round 4 B1: Tyrell at
      // (50, 70), on the y axis, kept its label below, across that axis), so
      // a mark on an axis takes the side that leaves it. Only a crossing the
      // mark cannot avoid (a mark on both axes) is not a layout finding.
      let clash = cost;
      for (const axis of axes) {
        const crossing = intersection(rect, axis) * 2;
        cost += crossing;
        if (intersection(axis, markBoxes[index]) === 0) clash += crossing;
      }
      if (!best || cost < best.cost) best = { side, rect, cost, clash };
      if (cost === 0) break;
    }
    const chosen = best ?? { side: "below" as const, rect: labelRect("below", cx, cy, mark, w, h), cost: 0, clash: 0 };
    const overlap = chosen.clash > 0;
    if (overlap) overlaps += 1;
    placed.push(chosen.rect);
    out[index] = { cx, cy, mark, label: chosen.rect, side: chosen.side, overlap };
  }
  return { items: out, quadrants, overlaps };
}
