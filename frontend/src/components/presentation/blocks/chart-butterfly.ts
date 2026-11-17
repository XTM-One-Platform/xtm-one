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
 * The room of a butterfly: two series mirrored about a central spine,
 * a population pyramid. Pure - no React, no Recharts, no DOM - so the drawing
 * (`ChartCanvas.tsx`), the facts it publishes for the exporter and the layout
 * audit, and the tests read ONE decision, as `chart-gauge.ts` is for a gauge.
 *
 * The categories are the rungs, one row each from the top in the given order.
 * The first series is drawn leftwards from the spine, the second rightwards,
 * both read as the positive sizes they are given. One scale serves both
 * sides: `symmetric` (unset) draws them to the same extent, the spine in the
 * middle of the plot; `symmetric: false` ends each at its own largest value
 * on that scale, the spine moving toward the smaller side. `spine: "center"`
 * (unset) writes the category labels in a column on the spine, between the
 * sides; `left` writes them on the left axis, the sides meeting at a rule.
 * A rung is read by its name: where the rows are too close for the tick
 * size, the names are set smaller down the text scale, and only past its
 * smallest is every other one left out.
 *
 * Recharts draws it as ONE horizontal bar chart whose value axis runs in
 * POSITIONS: the left side stored negative - as the PPTX stores it - the two
 * sides stacked with Recharts' `sign` offset so they share a row, and a
 * centre spine's column held by an invisible spacer either side of zero
 * (`gap` value units: the column's half width at the plot's scale). Every
 * position is written as the size it stands for: the tick at -(gap + 40)
 * reads `40`, and so does a value and its tooltip.
 *
 * Each value is written past the outer end of its bar, at the largest size of
 * the chart's value scale (`value_label_size`, else `label_size`) whose row
 * holds it and whose run past the longest bars leaves the sides their
 * readable share of the width - that room is solved, as an axis chart solves
 * its margins. A zero is a rung that side does not have: it draws no bar and
 * writes no value (`marks` leaves it out, and the PPTX labels write a zero
 * as nothing), while its hover still reads 0 and it adds nothing to the
 * side's total. What cannot be written is counted with its reason (`narrow`,
 * `short`) for the layout check, never dropped silently.
 */
import type { DeckButterflySpine, DeckChartBlock } from "@/types";

import {
  butterflySpine,
  butterflySymmetric,
  chartValueWriter,
  labelSurface,
  seriesValuesAsked,
  valueAxisScale,
  writtenValues,
  type ValueAxisScale,
} from "./chart-format";
import {
  AXIS_PADDING,
  axisTick,
  BAR_CATEGORY_GAP_DENSE,
  barSlot,
  CATEGORY_AXIS_HEIGHT,
  CATEGORY_AXIS_MAX_SHARE,
  chartTextScale,
  chartValueScale,
  EDGE_PAD,
  LABEL_GAP,
  LABEL_HALO,
  LABEL_OFFSET,
  labelRun,
  MAX_BAR_SIZE_HORIZONTAL,
  MIN_PLOT_PX,
  MIN_PLOT_SHARE,
  PLOT_MARGIN_BOTTOM,
  PLOT_MARGIN_LEFT,
  PLOT_MARGIN_TOP,
  textWidth,
  TICK_TEXT_GAP,
  type HiddenValuesReason,
  type PlotBox,
  type ValuePlan,
} from "./chart-layout";

/** The room a spine label keeps from the bars on each side of it (canvas px). */
const SPINE_PAD = 12;
/** The most round steps a side's axis is cut into. */
const MAX_SIDE_INTERVALS = 4;
/** How many times the room past the bars is solved again on the scale it leaves. */
const ROOM_PASSES = 8;

/** One side's axis, in the sizes it reads: from 0 at the spine out to `max`, round ticks `step` apart. */
interface ButterflySide {
  max: number;
  ticks: readonly number[];
}

/** A value written past the outer end of its bar: its side, its rung, the size it stands for and its text. */
interface ButterflyMark {
  side: 0 | 1;
  category: number;
  size: number;
  text: string;
}

/** Everything `ChartCanvas` hands Recharts about the room of a butterfly. */
export interface ButterflyLayout {
  spine: DeckButterflySpine;
  /** The size every glyph of the axes and the spine is set in (`chartTextScale`). */
  tickSize: number;
  margin: { top: number; right: number; bottom: number; left: number };
  plot: PlotBox;
  /** The value axis Recharts draws, in positions: the left side negative, `gap` either side of zero. */
  domain: readonly [number, number];
  /** Where each tick stands (a position), and the size it writes there. */
  ticks: readonly number[];
  tickLabels: ReadonlyMap<number, string>;
  /** Half the spine's column in value units: the invisible spacer each side of zero. */
  gap: number;
  /** One step for both sides, so a bar's length reads the same on either. */
  step: number;
  left: ButterflySide;
  right: ButterflySide;
  /** The axis as the PPTX writes it (`.deck-chart-facts`): the left side's extent negative, one step. */
  axis: ValueAxisScale;
  /** The spine's column on the canvas (its two edges meet on a `left` spine) and its middle (canvas px). */
  spineLeft: number;
  spineRight: number;
  spineCenter: number;
  /**
   * Where the category labels are written: centred on the spine's column, or
   * right-aligned against the left side's room on a `left` spine, where a
   * value axis' tick would stand (canvas px).
   */
  categoryX: number;
  categoryAnchor: "middle" | "end";
  /** The size the category labels are set in: the ticks', or smaller down the text scale until a row holds one. */
  categorySize: number;
  /** Every n-th category is written (1: all), when the rows are too close for each even at the smallest size. */
  categoryStep: number;
  band: number;
  barSize: number;
  barCategoryGap: string;
  maxBarSize: number;
  /** The bars as drawn, for the PPTX writer's gap width (`data-bar-*`): one row, no gap between the sides. */
  barSpacing: { width: number; gap: number; categoryGap: number };
  values: ValuePlan;
  /** The values `values.outer` writes: each visible side's that asks for them, every rung but a zero. */
  marks: readonly ButterflyMark[];
}

/** Rounds away the float noise of a multiple of a step. */
const clean = (value: number): number => Number(value.toPrecision(12));

/** The first multiple of `step` at or past `value`, and never under one step: a side of zeros still has an axis. */
const extentOf = (value: number, step: number): number => Math.max(step, clean(Math.ceil(value / step - 1e-9) * step));

/** `0, step, ..., max`. */
const sideTicks = (max: number, step: number): number[] => {
  const ticks: number[] = [];
  for (let index = 0; index * step <= max + step * 1e-6; index += 1) ticks.push(clean(index * step));
  return ticks;
};

/**
 * The two sides' axes cut in about `intervals` round steps of one size: the
 * longest side's largest value read off the plain round ladder an axis chart
 * takes (`valueAxisScale`, never a percent dial - a share of a population is
 * drawn to its own size), each side ending on the first step past its own
 * largest value, or both on the longer one's when symmetric.
 */
function sideScales(most: readonly number[], symmetric: boolean, intervals: number) {
  const top = Math.max(most[0], most[1]);
  const { step } = valueAxisScale(
    { kind: "bar", series: [{ name: "", values: [top] }], categories: [""], format: "number" },
    intervals + 1,
  );
  const left = extentOf(symmetric ? top : most[0], step);
  const right = extentOf(symmetric ? top : most[1], step);
  return { step, left, right };
}

/** The room a butterfly gives its axes, its spine, its bars and its values, at `width` x `height` canvas px. */
export function butterflyLayout(
  block: DeckChartBlock,
  width: number,
  height: number,
  hidden: ReadonlySet<number>,
  locale: string,
): ButterflyLayout {
  const spine = butterflySpine(block);
  const symmetric = butterflySymmetric(block);
  const tickSize = chartTextScale(block).tick;
  const valueSizes = chartValueScale(block).values;
  const categories = block.categories;
  const count = Math.max(1, categories.length);
  const sizesOf = (side: number): number[] => (block.series[side]?.values ?? []).map((value) => Math.abs(value));
  const most = [Math.max(0, ...sizesOf(0)), Math.max(0, ...sizesOf(1))];
  const label = chartValueWriter(block, writtenValues(block), locale, labelSurface(block));

  // Across: one row per rung, the plot's height shared; the two sides of a
  // rung stand on ONE row, so a bar takes the band as a lone bar does.
  const plotTop = PLOT_MARGIN_TOP;
  const plotBottom = Math.max(plotTop, height - PLOT_MARGIN_BOTTOM - CATEGORY_AXIS_HEIGHT);
  const band = (plotBottom - plotTop) / count;
  const slot = barSlot(band, 1, BAR_CATEGORY_GAP_DENSE, MAX_BAR_SIZE_HORIZONTAL);
  // A rung is read by its name: set smaller down the chart's text scale
  // until each row holds it, and only past the smallest every other one.
  const categorySizes = [tickSize, ...chartTextScale(block).values.filter((size) => size < tickSize)];
  const rowHolds = (size: number) => size + LABEL_GAP / 2 <= band;
  const categorySize = categorySizes.find(rowHolds) ?? categorySizes[categorySizes.length - 1];
  const categoryStep = rowHolds(categorySize) ? 1 : Math.ceil((categorySize + LABEL_GAP / 2) / Math.max(band, 1));

  // Along: the category labels' column - on the left axis, or on the spine
  // between the sides - then the room both sides share with the values
  // written past their outer ends, which the plot's margins hold so that the
  // plot (and its grid) is exactly the two sides and the spine.
  const longestCategory = Math.max(0, ...categories.map((category) => textWidth(category, categorySize)));
  const column = (pad: number) => Math.ceil(Math.min(longestCategory + pad, width * CATEGORY_AXIS_MAX_SHARE));
  const categoryAxisWidth = spine === "left" ? column(AXIS_PADDING) : 0;
  const spineWidth = spine === "center" ? column(2 * SPINE_PAD) : 0;
  const innerLeft = PLOT_MARGIN_LEFT + categoryAxisWidth;
  const innerRight = width - PLOT_MARGIN_LEFT;
  const length = Math.max(0, innerRight - innerLeft - spineWidth);
  const minSides = Math.min(length, Math.max(MIN_PLOT_PX, MIN_PLOT_SHARE * length));

  // The values a visible side writes: a zero has no bar to write one past.
  const marks: ButterflyMark[] = [];
  for (const side of [0, 1] as const) {
    if (hidden.has(side) || !seriesValuesAsked(block, side)) continue;
    sizesOf(side).forEach((size, category) => {
      if (size > 0) marks.push({ side, category, size, text: label(size) });
    });
  }
  const asked = marks.length;

  // The room past each side's outer end: its values' runs past their bars,
  // and half its end tick, centred there, inside the canvas. Solved again
  // on the scale each pass leaves (`u`, canvas px per unit of size).
  const solve = (scale: ReturnType<typeof sideScales>, valueSize: number | null, tick: (value: number) => string) => {
    const halfTick = (value: number) => textWidth(tick(value), tickSize) / 2;
    const tickRoom = (value: number, outside: number) => Math.max(0, halfTick(value) - outside + EDGE_PAD);
    const unit = (left: number, right: number) => Math.max(0, length - left - right) / (scale.left + scale.right);
    let outerLeft = Math.ceil(tickRoom(scale.left, innerLeft));
    let outerRight = Math.ceil(tickRoom(scale.right, width - innerRight));
    for (let pass = 0; pass < ROOM_PASSES; pass += 1) {
      const u = unit(outerLeft, outerRight);
      let needLeft = tickRoom(scale.left, innerLeft);
      let needRight = tickRoom(scale.right, width - innerRight);
      if (valueSize !== null) {
        for (const mark of marks) {
          const extent = mark.side === 0 ? scale.left : scale.right;
          const reach = LABEL_OFFSET + labelRun(mark.text, valueSize) + EDGE_PAD - (extent - mark.size) * u;
          if (mark.side === 0) needLeft = Math.max(needLeft, reach);
          else needRight = Math.max(needRight, reach);
        }
      }
      const next = [Math.ceil(needLeft), Math.ceil(needRight)];
      if (next[0] === outerLeft && next[1] === outerRight) break;
      [outerLeft, outerRight] = next;
    }
    const u = unit(outerLeft, outerRight);
    return { outerLeft, outerRight, u, sides: (scale.left + scale.right) * u };
  };

  // The values' size: the largest whose row holds it and whose run leaves the
  // sides their least; the steps: the most that keep the ticks apart there.
  const choose = (intervals: number) => {
    const scale = sideScales(most, symmetric, intervals);
    const tickValues = [...new Set([...sideTicks(scale.left, scale.step), ...sideTicks(scale.right, scale.step)])];
    const tick = axisTick(block, "primary", locale, tickValues);
    let style: number | null = null;
    let reason: HiddenValuesReason | null = null;
    let room = 0;
    let need = 0;
    let fit = solve(scale, null, tick);
    if (asked > 0) {
      const across = valueSizes.filter((size) => size + LABEL_HALO <= slot.pitch);
      for (const size of across) {
        const tried = solve(scale, size, tick);
        if (tried.sides >= minSides) {
          style = size;
          fit = tried;
          break;
        }
      }
      if (style === null) {
        const smallest = valueSizes[valueSizes.length - 1] ?? tickSize;
        if (across.length === 0) {
          reason = "narrow";
          room = Math.round(slot.pitch);
          need = smallest + LABEL_HALO;
        } else {
          reason = "short";
          room = Math.round(solve(scale, across[across.length - 1], tick).sides);
          need = Math.round(minSides);
        }
      }
    }
    const widestTick = Math.max(0, ...tickValues.map((value) => textWidth(tick(value), tickSize)));
    const ticksApart = scale.step * fit.u >= widestTick + LABEL_GAP;
    return { scale, tick, style, reason, room, need, fit, ticksApart };
  };
  let plan = choose(MAX_SIDE_INTERVALS);
  for (let intervals = MAX_SIDE_INTERVALS - 1; intervals >= 1 && !plan.ticksApart; intervals -= 1) plan = choose(intervals);

  const { scale, tick, style, fit } = plan;
  const u = fit.u > 0 ? fit.u : 1e-6;
  const gap = spineWidth / 2 / u;
  const plotLeft = innerLeft + fit.outerLeft;
  const plotRight = innerRight - fit.outerRight;
  const spineLeft = plotLeft + scale.left * u;
  const spineRight = spineLeft + spineWidth;
  const domain: readonly [number, number] = [-(scale.left + gap), scale.right + gap];

  // Each side's ticks at the position it stands at, written as the size it
  // reads; sides that meet (a left spine) share their zero.
  const leftTicks = sideTicks(scale.left, scale.step);
  const rightTicks = sideTicks(scale.right, scale.step);
  const tickLabels = new Map<number, string>();
  for (const size of leftTicks) tickLabels.set(-(gap + size), tick(size));
  for (const size of rightTicks) {
    if (size === 0 && gap === 0) continue;
    tickLabels.set(gap + size, tick(size));
  }
  const ticks = [...tickLabels.keys()].sort((a, b) => a - b);
  const axisTicks = [...leftTicks.slice(1).reverse().map((size) => -size), ...rightTicks];

  const outer = style === null ? null : { size: style, rotate: false };
  const values: ValuePlan = {
    outer,
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
    written: outer ? asked : 0,
    reason: outer ? null : plan.reason,
    room: plan.room,
    need: plan.need,
    turned: null,
  };

  return {
    spine,
    tickSize,
    margin: { top: PLOT_MARGIN_TOP, right: width - plotRight, bottom: PLOT_MARGIN_BOTTOM, left: plotLeft },
    plot: { left: plotLeft, top: plotTop, right: plotRight, bottom: plotBottom },
    domain,
    ticks,
    tickLabels,
    gap,
    step: scale.step,
    left: { max: scale.left, ticks: leftTicks },
    right: { max: scale.right, ticks: rightTicks },
    axis: { min: -scale.left, max: scale.right, step: scale.step, ticks: axisTicks },
    spineLeft,
    spineRight,
    spineCenter: (spineLeft + spineRight) / 2,
    categoryX: spine === "center" ? (spineLeft + spineRight) / 2 : innerLeft - TICK_TEXT_GAP,
    categoryAnchor: spine === "center" ? "middle" : "end",
    categorySize,
    categoryStep,
    band,
    barSize: slot.size,
    barCategoryGap: `${BAR_CATEGORY_GAP_DENSE * 100}%`,
    maxBarSize: MAX_BAR_SIZE_HORIZONTAL,
    barSpacing: { width: slot.size, gap: 0, categoryGap: Math.max(0, band - slot.size) },
    values,
    marks,
  };
}
