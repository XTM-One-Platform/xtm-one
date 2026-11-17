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
 * The room of a gauge: how large its dial is drawn and where it stands
 * in the canvas. Pure - no React, no Recharts, no DOM - so the drawing
 * (`ChartCanvas.tsx`) and the tests read ONE decision, as `chart-slices.ts`
 * is for a pie.
 *
 * The gauge is measured by what it draws: a half disc standing on its
 * baseline, with its reading - the value over the goal label - written inside
 * it on that baseline. That box (the half disc, or the reading where a small
 * dial's reading stands taller than its radius) is centred in the canvas, on
 * a content slide and in a dashboard panel alike, so the dial never sits on
 * the floor of a tall panel under an empty half.
 *
 * `size` sets the dial's diameter (`GAUGE_DIAMETERS`), so gauges of three
 * sizes read as three sizes wherever they stand; `fill` - or `size: "full"`,
 * which the stylesheet reads as `fill` - draws the largest dial the canvas
 * holds. Either way the dial is never wider than the canvas less its insets,
 * nor its box taller, so the reading is never cut.
 *
 * The reading arc (`gaugeArcPath`) is square where the dial starts, flush with
 * the track's end, and rounded at its tip; a reading that fills the dial ends
 * square on the track's other end too.
 *
 * The exports need nothing of their own: the PPTX pastes the gauge as a
 * picture of this canvas (`PICTURE_CHART_KINDS` in `chart_export.py`), and the
 * PDF prints the slide.
 */
import type { DeckChartBlock, DeckMediaSize } from "@/types";

/** A dial's diameter at each `size` (canvas px), before the canvas bounds it. */
export const GAUGE_DIAMETERS: Readonly<Record<Exclude<DeckMediaSize, "full">, number>> = { sm: 240, md: 360, lg: 480 };
/** The room kept between the gauge's box and each edge of the canvas (canvas px). */
export const GAUGE_INSET = 8;
/** A dial is never drawn under this radius (canvas px). */
const MIN_RADIUS = 24;
/** The ring is this share of the radius, and never thicker than `MAX_THICKNESS` (canvas px). */
const THICKNESS_SHARE = 0.26;
const MAX_THICKNESS = 56;
/** The value is this share of the radius, within these bounds (canvas px). */
const READING_SHARE = 0.3;
const MIN_READING = 36;
const MAX_READING = 112;
/**
 * How tall the reading stands, mirroring `.deck-chart-gauge` in
 * `presentation.css`: the value on a line of 1em, a 2px gap, then the goal
 * label at `max(0.4em, --deck-fs-xs)` on the deck's 1.45 line height. The
 * floor is the portrait page's `--deck-fs-xs` (26px; a slide's is 20px), so a
 * reading is never given less room than it takes.
 */
const READING_GAP = 2;
const GOAL_SHARE = 0.4;
const GOAL_MIN = 26;
const GOAL_LINE = 1.45;

interface GaugeLayout {
  /** The dial's centre: the middle of its baseline (canvas px). */
  cx: number;
  cy: number;
  radius: number;
  /** The ring's width, from the outer radius in. */
  thickness: number;
  /** The value's font size; the goal label's follows it in the stylesheet. */
  readingSize: number;
  /** The box centred in the canvas: the half disc, or the reading where it is taller (canvas px). */
  box: { top: number; height: number };
}

/** How tall the reading stands at a value size of `size` (canvas px). */
export const gaugeReadingHeight = (size: number): number =>
  size + READING_GAP + Math.max(size * GOAL_SHARE, GOAL_MIN) * GOAL_LINE;

/** The dial `block` draws in a canvas of `width` x `height`, its box centred. */
export function gaugeLayout(block: Pick<DeckChartBlock, "size" | "fill">, width: number, height: number): GaugeLayout {
  // The largest dial the canvas holds: as wide as the canvas less its insets,
  // its half disc as tall.
  const room = Math.min(width / 2 - GAUGE_INSET, height - 2 * GAUGE_INSET);
  const size = block.size ?? "md";
  const asked = block.fill || size === "full" ? room : GAUGE_DIAMETERS[size] / 2;
  const radius = Math.max(Math.min(asked, room), MIN_RADIUS);
  const readingSize = Math.round(Math.min(Math.max(radius * READING_SHARE, MIN_READING), MAX_READING));
  const boxHeight = Math.max(radius, gaugeReadingHeight(readingSize));
  const top = (height - boxHeight) / 2;
  return {
    cx: width / 2,
    cy: top + boxHeight,
    radius,
    thickness: Math.min(radius * THICKNESS_SHARE, MAX_THICKNESS),
    readingSize,
    box: { top, height: boxHeight },
  };
}

/** A sector as Recharts hands it to a `shape`: angles in degrees, counter-clockwise from 3 o'clock. */
interface GaugeSector {
  cx: number;
  cy: number;
  innerRadius: number;
  outerRadius: number;
  startAngle: number;
  endAngle: number;
}

const RADIAN = Math.PI / 180;
const rounded2 = (value: number): number => Math.round(value * 100) / 100;

/** The point at `radius` and `angle` from the centre, written for a path (Recharts' `polarToCartesian`). */
const polar = (cx: number, cy: number, radius: number, angle: number): string =>
  `${rounded2(cx + radius * Math.cos(-angle * RADIAN))},${rounded2(cy + radius * Math.sin(-angle * RADIAN))}`;

/**
 * The reading arc as an SVG path, from the dial's start (`startAngle`) to the
 * value (`endAngle`). Its start is square, a straight edge across the ring on
 * the baseline, so it covers the track's end there instead of leaving a
 * corner of the track around a rounded cap. Its tip is a half disc as wide as
 * the ring, ending at the value, unless `roundTip` is off - a reading that
 * fills the dial ends square on the track's other end - or the arc is too
 * short to hold the cap, when it ends square at the value.
 */
export function gaugeArcPath(sector: GaugeSector, roundTip: boolean): string {
  const { cx, cy, innerRadius, outerRadius, startAngle, endAngle } = sector;
  const sweep = startAngle - endAngle;
  if (!(sweep > 0) || !(outerRadius > innerRadius)) return "";
  const cap = (outerRadius - innerRadius) / 2;
  const capAngle = cap / ((outerRadius + innerRadius) / 2) / RADIAN;
  const capped = roundTip && sweep > capAngle;
  const base = capped ? endAngle + capAngle : endAngle;
  const large = startAngle - base > 180 ? 1 : 0;
  const at = (radius: number, angle: number) => polar(cx, cy, radius, angle);
  const arc = (radius: number) => `A ${rounded2(radius)},${rounded2(radius)},0`;
  return [
    `M ${at(outerRadius, startAngle)}`,
    `${arc(outerRadius)},${large},1,${at(outerRadius, base)}`,
    capped ? `${arc(cap)},0,1,${at(innerRadius, base)}` : `L ${at(innerRadius, base)}`,
    `${arc(innerRadius)},${large},0,${at(innerRadius, startAngle)}`,
    "Z",
  ].join(" ");
}
