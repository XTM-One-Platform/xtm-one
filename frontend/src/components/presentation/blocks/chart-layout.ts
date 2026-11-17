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
 * The room of an axis chart - `bar`, `stacked_bar`, `line`, `area`, `combo`,
 * `waterfall`: how wide its axes are, where its plot sits, and how it
 * writes the values it was asked for. Pure - no React, no Recharts, no DOM -
 * so the drawing (`ChartCanvas.tsx`), the facts it publishes for the exporter
 * and the layout audit, and the tests all read ONE decision.
 *
 * Every size is estimated from the glyph count, never measured: cheap,
 * deterministic, and the same on the export page, where Recharts would
 * measure in whatever font had loaded at that instant. A run that meets the
 * edge of the canvas - a horizontal chart's values past its longest bar -
 * is given the excess of the body face's widest glyph over that estimate
 * (`labelRun`), so the frame never cuts the `%` of a `100%` total.
 *
 * A value the block asks for is not dropped while another way to write it
 * exists. A label is written upright just past its bar, as a
 * dashboard card writes it, and set smaller - down to the smallest size of the
 * chart's text scale (`label_size`) - while it would touch another
 * label or another bar; only then is it turned on its side. Two labels at
 * different heights do not touch, however near their bars stand. The plot
 * gives up the height the labels need above the bars; a stack writes each
 * segment's value inside the segment when it fits there, and its total past
 * its end. What still cannot be written is COUNTED, with the reason, and the
 * drawing publishes the count - the layout check reports it as "values hidden
 * on chart N", so the agent resizes the chart or cuts series instead of
 * shipping a board deck without its numbers. Values turned on their side are
 * reported too, as advisory ("values rotated on chart N").
 *
 * The values may be sized apart from the axis text (`value_label_size`):
 * the sizes they are tried at are then another text scale's, or a
 * ladder down from a number of canvas px (`chartValueScale`), while the ticks
 * and the category labels keep the size `label_size` sets.
 *
 * A chart that plots two units has a second value axis on the right:
 * both axes come from `dualAxisScales` (one grid, the same steps), each value
 * is written in the format of the axis its series reads against and placed
 * on that axis' scale, and the right-hand axis takes its width from the plot.
 *
 * A combo's line writes its values too: over each point, under it, or
 * raised clear of the bars' values in its column; one with no room is counted.
 *
 * A value that sits on a point - a combo's line, a line or an area chart -
 * keeps clear of every other value and the value axes' ticks, and
 * stays next to its point whatever bar it falls on, its halo keeping it
 * legible there; a value that would run into a tick is slid along the
 * category axis, back into the plot. A value that touches nothing is written
 * where it always was.
 *
 * A line's or an area's values that have no room in one row over their
 * points at a size are tried at that size ALTERNATING - the even points'
 * over them, the odd points' under them - before the size is lowered or the
 * values hidden: a run of eight `€240k` in a one-third dashboard
 * panel has twice a point's room that way. The drawing publishes the choice
 * (`data-values-alternate`) so the PPTX writer places the same values below.
 *
 * An upright bar, stacked bar or combo chart may say how its values past the
 * bars are fitted instead (`value_label_fit`). `auto` is the rule
 * above. `stagger` and `upright` set every value at the first size of the
 * values' scale, upright: the bars give way first - the bars of a group stand
 * further apart, then narrow, until a value clears the bars beside it - and
 * the plot gives up the height the values need above them. Each value is
 * written in its place, along the axis, unless a value written before it, a
 * bar but its own, a tick or a combo's line stands there: `stagger`
 * then moves it one row out, raised further past what is still in its way,
 * and `upright` moves it that one row out above upright bars and no further,
 * where nothing stands there and the plot already gives it the room - the
 * bars stay where they are - else leaves it out, counted. A chart
 * where nothing touches lifts nothing. A group of upright bars keeps the
 * room `auto` gives it instead: its values may overhang the gap and
 * the bars beside them, written over a taller neighbour's fill in their halo
 * as a value on a point is, and only a value or a tick moves one or
 * leaves it out. The drawing publishes each value's lift (`data-lift`) and
 * the bars' spacing so the PPTX writer draws the same.
 * A horizontal bar or stacked bar chart is fitted alike: its rows
 * give way, a value in the way of another moves along its row past it under
 * `stagger`, and the values inside a stack's segments keep the size asked -
 * one too long for its segment is written past the stack under `stagger`
 * (the thin segment's leader, below, at that size) and left out under
 * `upright`, counted, never set smaller.
 *
 * A segment of a stack too thin to hold its value inside is written past the
 * stack instead: after its total - above an upright stack, right of a
 * horizontal one - set small in its series' colour, with a leader from the
 * segment's side. The annotations of one stack are stacked outward, the
 * segment nearest the end first, so their leaders nest without crossing; the
 * plot gives up the room they need past the bars as it does for the totals.
 * Only a value with no room there either is counted, and the drawing then
 * says whether the same chart taller or across a full row would write it, so
 * the layout check never sends the agent after room that changes nothing.
 * Past a fitted total (`value_label_fit`), it comes after the total where the
 * fit wrote it.
 */
import type { DeckChartBlock, DeckSeriesAxis, DeckSeriesKind, DeckSize, DeckValueLabelFit } from "@/types";

import {
  anyValuesAsked,
  axisExtent,
  axisSurface,
  axisView,
  chartValueWriter,
  dualAxisScales,
  hasSecondaryAxis,
  isStacked,
  labelSurface,
  seriesAxis,
  seriesValuesAsked,
  VALUE_LABEL_SIZE_MIN,
  valueAxisScale,
  valueLabelFit,
  valueLabelSize,
  valuesAsked,
  waterfallLabel,
  writtenValues,
  type ValueAxisScale,
  type WaterfallStep,
} from "./chart-format";

/** How large a chart writes (canvas px): see `CHART_TEXT_SCALES`. */
interface ChartTextScale {
  /** Every glyph Recharts draws: the ticks, and the size a pie's values are tried at first (`chart-slices.ts`). */
  tick: number;
  /** The sizes a value may be set in, largest first: the tick size down to the smallest it is set upright in. */
  values: readonly number[];
  /** A value inside a segment is never set under this while a size at or above it fits across the bar. */
  segmentFloor: number;
}

/**
 * The text of a chart at each `label_size`. `md` is the slide's; `sm`
 * is a dashboard card's - Metabase writes its values and ticks one step under
 * a slide's; `lg` is read from the back of the room. The tick size is
 * mirrored by `.deck-chart[data-label-size]` in `presentation.css` and the
 * whole table by `CHART_TEXT_SCALES` in `chart_export.py` (a test pins both).
 */
export const CHART_TEXT_SCALES: Readonly<Record<DeckSize, ChartTextScale>> = {
  sm: { tick: 18, values: [18, 16, 14, 12, 10], segmentFloor: 14 },
  md: { tick: 22, values: [22, 20, 18, 16, 14, 12], segmentFloor: 16 },
  lg: { tick: 26, values: [26, 24, 22, 20, 18, 16, 14], segmentFloor: 18 },
};

/** The text scale a block asks for (`label_size`, `md` when unset). */
export function chartTextScale(block: Pick<DeckChartBlock, "label_size">): ChartTextScale {
  return CHART_TEXT_SCALES[block.label_size ?? "md"] ?? CHART_TEXT_SCALES.md;
}

/** The sizes the values on the figure are tried at, and the floor of the values inside segments (`chartValueScale`). */
type ChartValueScale = Pick<ChartTextScale, "values" | "segmentFloor">;

/**
 * A ladder down from a number of canvas px (`value_label_size`), as
 * the text scales step theirs: 2px at a time, reaching 8px under its first
 * size; its segments' floor 6px under it - neither under `VALUE_LABEL_SIZE_MIN`.
 */
const VALUE_LADDER_STEP = 2;
const VALUE_LADDER_REACH = 8;
const VALUE_SEGMENT_REACH = 6;

/**
 * The sizes the values on the figure are tried at: the text scale a
 * block's `value_label_size` names, or the ladder down from its number of
 * canvas px - 20 tries 20, 18, 16, 14 and 12, its segments never under 14 -
 * else `label_size`'s own. The ticks and the category labels always take
 * `label_size`'s tick size (`chartTextScale`).
 */
export function chartValueScale(block: Pick<DeckChartBlock, "kind" | "label_size" | "value_label_size">): ChartValueScale {
  const size = valueLabelSize(block);
  if (size === undefined) return chartTextScale(block);
  if (typeof size === "string") return CHART_TEXT_SCALES[size];
  const lowest = Math.max(VALUE_LABEL_SIZE_MIN, size - VALUE_LADDER_REACH);
  const values: number[] = [];
  for (let step = size; step >= lowest; step -= VALUE_LADDER_STEP) values.push(step);
  return { values, segmentFloor: Math.max(VALUE_LABEL_SIZE_MIN, size - VALUE_SEGMENT_REACH) };
}

/** Approximate advance of one glyph of the tick font, as a share of its size. */
const GLYPH_EM = 0.58;
/** The same for a value label, which is set semibold (`.deck-chart-value`). */
const LABEL_GLYPH_EM = 0.6;
/**
 * The widest glyph of the body face set semibold: the `%` of a share, 0.96
 * em in IBM Plex Sans (an `M` is 0.82, a digit 0.6). `labelWidth` counts
 * every glyph at the digit's advance, so a written number - which holds at
 * most one such glyph, its `%` or its `M` - is estimated short by at most
 * that glyph's excess. A run that meets the EDGE of the canvas keeps it
 * (`labelRun`): past the end of a horizontal bar at the top of its
 * axis, a `100%` estimated at 2.4 em was given 2.4 em of room, and the frame
 * cut the `%` the 2.76 em it takes ran into. Where a run meets another value
 * or a bar, the estimate stands: there the shortfall is a near miss, not a cut.
 */
const WIDE_GLYPH_EM = 0.96;
/** How tall a label reads (or how thick, turned on its side): the ink of its figures and capitals. */
export const INK_EM = 0.74;
/** Breathing room between two neighbouring labels (canvas px). */
export const LABEL_GAP = 12;
/** The halo a value wears (`.deck-chart-value`, a 6px stroke): two labels nearer than it paint over each other. */
export const LABEL_HALO = 6;
/**
 * The room a value on a point keeps past its halo from another value, a bar
 * or a tick (canvas px): two values whose halos just meet read as one block
 * of figures.
 */
const LABEL_CLEARANCE = 4;
/**
 * The least a value on a point is raised by per thing in its way (canvas px).
 * A raise lands the value exactly at its clearance, where rounding can
 * leave a sliver of overlap smaller than the precision of the rise: adding it
 * changes nothing, and the value would be raised by zero forever.
 */
const MIN_RISE_STEP = 0.01;
/** How far Recharts writes a value axis' tick from the plot (its `tickSize` 6 plus `tickMargin` 2, canvas px). */
export const TICK_TEXT_GAP = 8;
/** How many times the plot gives up room for the values raised over the bars before the rest are counted. */
const MAX_ROOM_STEPS = 8;
/** How far a value sits from the end of its bar (canvas px): just past it, as a dashboard card writes it. */
export const LABEL_OFFSET = 6;
/** How far a value sits above its point (canvas px): clear of the dot, `r` 6 in a 2px ring. */
const POINT_LABEL_OFFSET = 10;
/** The clear room a value keeps around it inside a segment (canvas px, each side). */
const SEGMENT_PAD_X = 4;
const SEGMENT_PAD_Y = 3;
/** The stroke of a leader from a thin segment to its value past the stack (canvas px). */
export const LEADER_WIDTH = 2;
/** The stroke of a line, a combo's included (canvas px). */
export const LINE_WIDTH = 4;
/**
 * The longest run of a combo's line one box of its stroke covers, where it is
 * in the way of the values past the bars (`value_label_fit`, canvas px): short
 * enough that the box of a steep run is hardly wider than its stroke.
 */
const LINE_SAMPLE = 4;
/**
 * Where a stack's first leader runs beside it, past its side and anything
 * written over that side (canvas px), and how far each next one runs out: a
 * stack's leaders nest, two strokes apart.
 */
const LEADER_INSET = 4;
const LEADER_STEP = 4;
/**
 * The tallest canvas a slide gives a chart (canvas px): `size: lg`, the
 * `--deck-chart-h` of `.deck-chart[data-size="lg"]` in `presentation.css` and
 * `CHART_TALLEST_CANVAS_PX` in `presentation_layout.py` (a test pins all
 * three) - a `fill` chart alone under a slide title is about as tall.
 * @public
 */
export const TALLEST_CHART_CANVAS = 640;
/** The canvas of a chart across a whole row of a 16:9 slide, or a span 3 dashboard panel (canvas px). */
const FULL_ROW_CANVAS = 1670;
/** No label is painted nearer than this to the edge of the canvas (canvas px). */
export const EDGE_PAD = 2;
/** Room between an axis label and the plot it reads against (canvas px). */
export const AXIS_PADDING = 16;
/** A category axis of a horizontal chart never takes more than this share of the width. */
export const CATEGORY_AXIS_MAX_SHARE = 0.4;
/** The plot's margins (canvas px); the right one keeps the last category label inside the canvas. */
export const PLOT_MARGIN_LEFT = 4;
const PLOT_MARGIN_RIGHT = 40;
export const PLOT_MARGIN_TOP = 16;
export const PLOT_MARGIN_BOTTOM = 4;
/** The room Recharts gives the axis along the bottom (its default `height`). */
export const CATEGORY_AXIS_HEIGHT = 30;
/** Below this height the value axis keeps three ticks instead of five. */
const DENSE_TICKS_BELOW = 260;
/**
 * Below this canvas height a line or an area writes no values: a panel on a
 * two-row dashboard is about 180px tall, and a label row there sits on the
 * line it describes. Bars have no such floor - their labels sit past the bar.
 */
const LABELLED_MIN_HEIGHT = 220;
/** The labels never squeeze the plot below this share of its length, nor below `MIN_PLOT_PX`. */
export const MIN_PLOT_SHARE = 0.55;
export const MIN_PLOT_PX = 96;
/**
 * The share of a category band left clear on EACH side of its bars
 * (`barCategoryGap`): a lone series breathes, a group or a stack takes more of
 * the band - those are the bars whose values must fit beside or inside them
 * (three bars of a group in one column of a slide are 11px wide at 24%, 18px
 * here; a stack's `€25.6M` segment fits a 16px label only from 12%).
 * Mirrored by `_BAR_GAP_WIDTH*` in `pptx_writer.py`.
 */
const BAR_CATEGORY_GAP = 0.24;
export const BAR_CATEGORY_GAP_DENSE = 0.12;
/** Room between two bars of one group (`barGap`, canvas px). */
export const BAR_GAP = 6;
/**
 * The least share of its own width a bar keeps when it narrows for the values
 * beside it (`value_label_fit` stagger / upright): past it, a bar would
 * read as a rule, and the values left touching it are raised or counted.
 */
const GIVE_WAY_MIN_SHARE = 0.4;
/** The widest a bar is drawn (`maxBarSize`, canvas px), upright and on its side. */
const MAX_BAR_SIZE = 120;
export const MAX_BAR_SIZE_HORIZONTAL = 64;
/** The room a line's first and last points keep from the plot's edges (canvas px, each side). */
const POINT_PADDING = 24;

/** Estimated width of a run of text set in the tick font at `size` (canvas px). */
export const textWidth = (text: string, size: number): number => text.length * size * GLYPH_EM;
/** Estimated width of a value label set at `size` (canvas px). */
export const labelWidth = (text: string, size: number): number => text.length * size * LABEL_GLYPH_EM;
/**
 * The room a value label's run takes where it meets the edge of the canvas
 * (canvas px): its estimated width and the excess of the one wide glyph it
 * may hold (`WIDE_GLYPH_EM`). The room a horizontal chart keeps past its
 * longest bar is measured with this, so its totals are never cut.
 */
export const labelRun = (text: string, size: number): number => labelWidth(text, size) + (WIDE_GLYPH_EM - LABEL_GLYPH_EM) * size;

/** How a series is drawn: the block's kind, or a combo series' own word. */
export function drawnKind(block: Pick<DeckChartBlock, "kind">, seriesKind: DeckSeriesKind | undefined): DeckSeriesKind {
  if (block.kind === "line") return "line";
  if (block.kind === "combo") return seriesKind ?? "bar";
  return "bar";
}

/** The key of one segment of a stack: its series and its category. */
export const segmentKey = (series: number, category: number): string => `${series}:${category}`;
/** The key of the total over a stacked area's category (`ValuePlan.placed`). */
export const totalKey = (category: number): string => `total:${category}`;
/**
 * The key of a value written past a bar (`ValuePlan.lifts`): a bar's
 * own by its series and category, a stack's total by its category and the
 * way it runs - a category holding both signs has a total each way.
 */
export const pastBarKey = (role: "value" | "total", toward: 1 | -1, series: number, category: number): string =>
  role === "total" ? `total:${toward === 1 ? "up" : "down"}:${category}` : segmentKey(series, category);

/** How a run of values is set: its size (canvas px), upright or turned on its side (read bottom to top). */
export interface LabelStyle {
  size: number;
  rotate: boolean;
}

/**
 * Where a value on a point sits: `under` its point, or over it,
 * `rise` px higher than the room right over it when what is written or drawn
 * there holds that room (0 when nothing does), and `shift` px along the
 * category axis when it would run into a value axis' ticks (0 otherwise).
 */
interface PointPlacement {
  under: boolean;
  rise: number;
  shift: number;
}

/**
 * What the drawing says of a value moved off its place (`data-placement`):
 * `lifted` raised, `shifted` slid along the category axis, `below` under
 * its point. The PPTX writer gives PowerPoint's own label to a value over or
 * under its point and writes the others as the text the viewer placed.
 */
export function pointPlacementName({ under, rise, shift }: PointPlacement): "below" | "lifted" | "shifted" | undefined {
  if (rise > 0) return "lifted";
  if (shift !== 0) return "shifted";
  return under ? "below" : undefined;
}

/**
 * Why some of the values a block asked for are not on the figure
 * (`.deck-chart-facts[data-values-reason]`, read by the layout audit):
 * `narrow` - even on its side at the smallest size a label is thicker than the
 * room between two bars (or, on a line, wider than the run between points, in
 * one row and alternating over and under them; on a pie or a donut, a
 * slice's value finds no room outside it nor inside it, `chart-slices.ts`);
 * `short` - the plot is too short to give the labels their room past the bars
 * (or a line's canvas is under `LABELLED_MIN_HEIGHT`); `segments` - some
 * segments of a stack are too thin or too narrow to hold their value inside
 * and find no room past their stack either, the totals written
 * (`ValuePlan.thin` says what more room would do); `dense` - a pie or a donut
 * past `MAX_LABELLED_VALUES` slices; `overlap` - a value on a point (a combo's
 * line, a line or an area) finds the room over its point and under it taken
 * by a bar, a bar's value or another point's, and no room left on the canvas
 * to raise it clear of them; `upright` - a value kept upright at the size
 * asked (`value_label_fit` upright) still touches the value or the bar
 * beside it once the bars gave way, or a combo's line; `region` - a
 * map's country lies beyond the region it is zoomed on (`chart-map.ts`).
 */
export type HiddenValuesReason = "narrow" | "short" | "segments" | "dense" | "overlap" | "upright" | "region";

/**
 * A segment's value written past its stack: where the text sits, and
 * the leader's corners (canvas px) from the segment's side to the text.
 */
export interface SegmentAnnotation {
  placement: LabelPlacement;
  leader: readonly (readonly [number, number])[];
}

/**
 * The segments still hidden once inside and past the stack were tried,
 * for the layout check: the thinnest one's share of the axis range;
 * its length (canvas px) with the most room its value axis can be given -
 * `TALLEST_CHART_CANVAS` high upright, `FULL_ROW_CANVAS` wide on its side;
 * whether the same chart at that height (`taller`) or that width (`wider`)
 * writes every value; and the series whose values stay hidden.
 */
interface HiddenSegments {
  share: number;
  reach: number;
  taller: boolean;
  wider: boolean;
  series: readonly number[];
}

/** What the figure writes of the values its block asked for. */
export interface ValuePlan {
  /** Each bar's or point's own value, or each stack's total: how it is set, `null` when not written. */
  outer: LabelStyle | null;
  /** How the values past the bars were fitted (`valueLabelFit`): `auto` on every chart the word does not act on. */
  fit: DeckValueLabelFit;
  /**
   * Under `stagger` / `upright`, how far past its place each value
   * written past a bar sits (canvas px, by `pastBarKey`): 0 on the near row,
   * a row or more on the far one. A value with no entry is not written.
   * `null` under `auto`, where every value `outer` writes sits in its place.
   */
  lifts: ReadonlyMap<string, number> | null;
  /**
   * A line's or an area's values alternate over and under their points:
   * the even categories' over, the odd ones' under - they had no
   * room in one row at `outer`'s size. `false` while they sit in one row.
   */
  alternate: boolean;
  /** The values written inside the segments of a stacked bar: how, `null` when none is. */
  inner: LabelStyle | null;
  /** The segments that hold their value (`segmentKey`). */
  segments: ReadonlySet<string>;
  /** The values of thin segments written past their stack: how, `null` when none is. */
  annotation: LabelStyle | null;
  /** Each segment whose value is written past its stack (`segmentKey`). */
  annotations: ReadonlyMap<string, SegmentAnnotation>;
  /** What the layout check needs of segments still hidden (`segments`); `null` otherwise. */
  thin: HiddenSegments | null;
  /** The values of a combo's lines: how they are set, `null` when none is written. */
  points: LabelStyle | null;
  /**
   * Where each value written on a point sits against it: a combo line's, a
   * line's or an area's (`segmentKey`), a stacked area's total (`totalKey`).
   * A value on a point with no entry is not written.
   */
  placed: ReadonlyMap<string, PointPlacement>;
  /** Values the block asked to see, and how many of them the figure writes. */
  asked: number;
  written: number;
  /** Why the rest are not written; `null` when every one is. */
  reason: HiddenValuesReason | null;
  /**
   * The room the tightest label had and the room it needed (canvas px), for
   * the layout check; for `segments`, the thinnest hidden segment's length
   * and what its value needs inside it.
   */
  room: number;
  need: number;
  /**
   * Why the values are turned on their side, for the layout check:
   * the smallest size they were tried upright in, the room between two bars
   * and what the widest of them needs there upright. `null` while upright.
   */
  turned: { size: number; room: number; need: number } | null;
}

/** A plot's box on the canvas (canvas px). */
export interface PlotBox {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/** Everything `ChartCanvas` hands Recharts about the room of an axis chart. */
export interface AxisChartLayout {
  horizontal: boolean;
  stacked: boolean;
  /** Line / area: the values sit on points, not on bars in bands. */
  pointsOnly: boolean;
  /** The size every glyph Recharts draws is set in (`chartTextScale`). */
  tickSize: number;
  /** How far a value sits past its bar or above its point (`outerLabelPlacement`). */
  labelOffset: number;
  tickCount: number;
  axis: ValueAxisScale;
  /** The width of the axis the chart is read against: values upright, categories on a horizontal chart. */
  readingAxisWidth: number;
  /** The right-hand value axis of a chart that plots two units, and its width; `0` without one. */
  secondaryAxis?: ValueAxisScale;
  secondaryAxisWidth: number;
  /** The room a line's first and last points keep from the plot's edges (canvas px, each side). */
  pointPadding: number;
  margin: { top: number; right: number; bottom: number; left: number };
  plot: PlotBox;
  /** `0` writes every category label; a category axis too dense for them keeps its first and last. */
  categoryAxisInterval: 0 | "preserveStartEnd";
  barCategoryGap: string;
  maxBarSize: number;
  /** The width Recharts gives one bar (`barSlot`); a waterfall's connectors run between two of them. */
  barSize: number;
  /**
   * The bars as they stand once they gave way to the values beside them
   * (`value_label_fit`): the width each is drawn at (Recharts'
   * `barSize`), the room between two bars of a group (its `barGap`) and
   * between two groups (canvas px). `null` while they stand where
   * `barCategoryGap` puts them.
   */
  gaveWay: { width: number; gap: number; categoryGap: number } | null;
  /**
   * The bars as the values past them were fitted around (`value_label_fit`),
   * in `gaveWay`'s terms, published for the PPTX writer: the bars that gave
   * way, or a group of upright bars where `barCategoryGap` and `BAR_GAP` put
   * them. `null` while the values are not fitted.
   */
  barSpacing: { width: number; gap: number; categoryGap: number } | null;
  values: ValuePlan;
}

/**
 * Recharts' room for one bar of a band (`getBarPositions`): the bar's size,
 * the distance from its centre to its neighbour's - one slot within a group,
 * a whole band between two lone bars - and where the first bar's centre sits
 * from the start of its band (the n-th one's is `first + n * pitch`).
 */
export function barSlot(
  band: number,
  lanes: number,
  gapShare: number,
  maxBarSize: number,
): { size: number; pitch: number; first: number } {
  const offset = band * gapShare;
  const gap = band - 2 * offset - (lanes - 1) * BAR_GAP <= 0 ? 0 : BAR_GAP;
  let original = (band - 2 * offset - (lanes - 1) * gap) / lanes;
  if (original > 1) original = Math.floor(original);
  return {
    size: Math.max(0, Math.min(original, maxBarSize)),
    pitch: lanes > 1 ? original + gap : band,
    first: offset + original / 2,
  };
}

/**
 * The same room when every bar is given its width (`barSize`) and the room
 * between two bars of a group (`barGap`): Recharts centres the group in its
 * band, its offset cut to a whole pixel, and drops the gap when the group
 * would fill the band.
 */
function fixedBarSlot(band: number, lanes: number, size: number, gap: number): { size: number; pitch: number; first: number } {
  let room = gap;
  let sum = lanes * size + (lanes - 1) * room;
  if (sum >= band) {
    sum -= (lanes - 1) * room;
    room = 0;
  }
  const offset = Math.trunc((band - sum) / 2);
  return { size, pitch: lanes > 1 ? size + room : band, first: offset + size / 2 };
}

/**
 * The width of a value axis, from the longest tick it writes. Recharts can
 * measure its ticks itself (`width="auto"`), but it does so in a hidden span
 * outside the deck, in whatever font the page has loaded at that instant - on
 * the export page that is the fallback font, and the `€22.5M` column then
 * paints half a glyph past its room. The ticks ARE known here: the axis is
 * built by `valueAxisScale` before the chart is drawn.
 */
function valueAxisWidth(ticks: readonly number[], tick: (value: number) => string, size: number): number {
  const longest = Math.max(0, ...ticks.map((value) => textWidth(tick(value), size)));
  return Math.ceil(longest + AXIS_PADDING);
}

/**
 * A bar as drawn, in plot terms: its centre across the category axis (canvas
 * px from the plot's start - its left upright, its top on a horizontal chart)
 * and the two ends it spans along the value axis (0 at the axis floor, 1 at
 * its top). A stack is one bar per direction, a waterfall step the span it
 * floats over.
 */
interface DrawnBar {
  at: number;
  low: number;
  high: number;
  /** Its band and its place in it, for the bars that give way to their values. */
  category: number;
  lane: number;
}

/** A value written past the end of its bar (or above its point): where the end sits, which way it reads. */
interface OuterMark {
  value: number;
  text: string;
  toward: 1 | -1;
  /** Where the end sits along its own value axis: 0 at its floor, 1 at its top. */
  ratio: number;
  /** The bar it is written past (an index of the chart's drawn bars); none for a point. */
  bar?: number;
  /** A point's category, and the key its place is planned under (`ValuePlan.placed`); none past a bar. */
  category?: number;
  key?: string;
  /** The key a value past a bar is fitted under (`pastBarKey`, `ValuePlan.lifts`). */
  pastKey?: string;
}

/**
 * One direction of one stacked bar: its category, which way it runs,
 * the total written past it (`null` when the chart writes none), and its
 * segments from the baseline out, each spanning `from` to `to` in value units.
 */
interface StackRun {
  category: number;
  toward: 1 | -1;
  total: string | null;
  /** The total's index among the chart's marks, whose fitted place it takes under `value_label_fit`. */
  mark: number | null;
  segments: { series: number; value: number; text: string; from: number; to: number }[];
}

/** A box on the canvas (canvas px). */
export interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export const overlap = (a: Rect, b: Rect): boolean =>
  a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

/** A box grown by `by` px on every side. */
export const grown = (rect: Rect, by: number): Rect => ({
  left: rect.left - by,
  top: rect.top - by,
  right: rect.right + by,
  bottom: rect.bottom + by,
});

/**
 * The stroke of each leg of a leader, its corners joined by legs
 * that run along one axis. A leg is not stretched past its ends: the first
 * one starts ON its segment's side and so touches its bar without covering it.
 */
const leaderLegs = (corners: readonly (readonly [number, number])[]): Rect[] =>
  corners.slice(1).map(([x2, y2], index) => {
    const [x1, y1] = corners[index];
    const half = LEADER_WIDTH / 2;
    return y1 === y2
      ? { left: Math.min(x1, x2), right: Math.max(x1, x2), top: y1 - half, bottom: y1 + half }
      : { left: x1 - half, right: x1 + half, top: Math.min(y1, y2), bottom: Math.max(y1, y2) };
  });

type Point = readonly [number, number];

/**
 * The path a `monotone` line is drawn along through its points, left to
 * right: d3's `curveMonotoneX`, which Recharts draws it with (Steffen 1990) -
 * one cubic per two neighbouring points, `[from, control, control, to]`, its
 * tangent at an inner point the gentler of the slopes beside it (flat at a
 * peak or a trough), one-sided at the ends. Two points are joined straight.
 */
export function monotoneCurve(points: readonly Point[]): Point[][] {
  if (points.length < 2) return [];
  if (points.length === 2) return [[points[0], points[0], points[1], points[1]]];
  const sign = (value: number): number => (value < 0 ? -1 : 1);
  const slope = (a: Point, b: Point): number => (b[1] - a[1]) / (b[0] - a[0]);
  const tangents = points.map((point, index) => {
    if (index === 0 || index === points.length - 1) return 0;
    const [before, after] = [points[index - 1], points[index + 1]];
    const [s0, s1] = [slope(before, point), slope(point, after)];
    const [h0, h1] = [point[0] - before[0], after[0] - point[0]];
    const mean = (s0 * h1 + s1 * h0) / (h0 + h1);
    return (sign(s0) + sign(s1)) * Math.min(Math.abs(s0), Math.abs(s1), 0.5 * Math.abs(mean)) || 0;
  });
  const last = points.length - 1;
  tangents[0] = (3 * slope(points[0], points[1]) - tangents[1]) / 2;
  tangents[last] = (3 * slope(points[last - 1], points[last]) - tangents[last - 1]) / 2;
  return points.slice(1).map((to, index) => {
    const from = points[index];
    const third = (to[0] - from[0]) / 3;
    return [from, [from[0] + third, from[1] + third * tangents[index]], [to[0] - third, to[1] - third * tangents[index + 1]], to];
  });
}

/**
 * A drawn curve's stroke as thin boxes: each cubic cut into runs of at most
 * `LINE_SAMPLE` px, each run's box grown by half the stroke and `clearance`.
 */
function strokeBoxes(curve: readonly Point[][], stroke: number, clearance: number): Rect[] {
  const grow = stroke / 2 + clearance;
  const boxes: Rect[] = [];
  for (const [p0, p1, p2, p3] of curve) {
    const at = (t: number): Point => {
      const u = 1 - t;
      const [a, b, c, d] = [u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t];
      return [a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]];
    };
    // The control polygon is never shorter than the curve it holds.
    const reach = Math.hypot(p1[0] - p0[0], p1[1] - p0[1]) + Math.hypot(p2[0] - p1[0], p2[1] - p1[1]) + Math.hypot(p3[0] - p2[0], p3[1] - p2[1]);
    const runs = Math.max(1, Math.ceil(reach / LINE_SAMPLE));
    let start = p0;
    for (let run = 1; run <= runs; run += 1) {
      const end = at(run / runs);
      boxes.push({
        left: Math.min(start[0], end[0]) - grow,
        right: Math.max(start[0], end[0]) + grow,
        top: Math.min(start[1], end[1]) - grow,
        bottom: Math.max(start[1], end[1]) + grow,
      });
      start = end;
    }
  }
  return boxes;
}

/**
 * The ink a value takes where `outerLabelPlacement` puts it, as the drawing
 * sets it: upright, the run of its glyphs on its anchor, the height of its
 * figures on its baseline; turned on its side (read bottom to top, `rotate(-90)`
 * about the anchor), the same two lengths swapped.
 */
export function labelInk(placement: LabelPlacement, text: string, size: number): Rect {
  const length = labelWidth(text, size);
  const ink = size * INK_EM;
  const { x, y, anchor, baseline, rotate } = placement;
  if (rotate) {
    const [top, bottom] = anchor === "start" ? [y - length, y] : anchor === "end" ? [y, y + length] : [y - length / 2, y + length / 2];
    return { left: x - ink / 2, right: x + ink / 2, top, bottom };
  }
  const [left, right] = anchor === "start" ? [x, x + length] : anchor === "end" ? [x - length, x] : [x - length / 2, x + length / 2];
  const [top, bottom] = baseline === "auto" ? [y - ink, y] : baseline === "hanging" ? [y, y + ink] : [y - ink / 2, y + ink / 2];
  return { left, right, top, bottom };
}

/** One way to set the outer values, and whether the room across the bars takes it. */
interface Candidate {
  style: LabelStyle;
  /** The room the label takes across the category axis, against the pitch. */
  across: number;
  /** The room a mark's label takes along the value axis, past the end it hangs on. */
  along: (mark: OuterMark) => number;
  /**
   * The values alternate over and under their points: `across` is
   * held to TWICE the pitch, the values under their points claim no room over
   * the plot and must stay above the category axis.
   */
  alternate?: boolean;
}

/**
 * How the `ticks` of one value axis read: compact from its own extent, its
 * unit kept between two axes, as precise as the axis' decimals or,
 * unset, its ticks need.
 */
export function axisTick(
  block: DeckChartBlock,
  axis: DeckSeriesAxis,
  locale: string,
  ticks: readonly number[],
): (value: number) => string {
  const view = axisView(block, axis);
  const surface = axisSurface(axisExtent(view));
  return chartValueWriter(view, ticks, locale, surface, { axisUnit: hasSecondaryAxis(block) });
}

/**
 * How the values of each series read, in series order: in the format of the
 * axis it is read against, one run per axis - every value that axis
 * writes takes the same decimals.
 */
export function seriesLabels(block: DeckChartBlock, locale: string): ((value: number) => string)[] {
  const twoAxes = hasSecondaryAxis(block);
  const writers = new Map<DeckSeriesAxis, (value: number) => string>();
  return block.series.map((entry) => {
    const axis: DeckSeriesAxis = twoAxes ? seriesAxis(entry) : "primary";
    let write = writers.get(axis);
    if (!write) {
      const view = axisView(block, axis);
      write = chartValueWriter(view, writtenValues(view), locale, labelSurface(view));
      writers.set(axis, write);
    }
    return write;
  });
}

/**
 * The room an axis chart gives its axes, its plot and its values, at
 * `width` x `height` canvas px with the legend's `hidden` series left out.
 * A waterfall is laid out from the bars it draws (`steps`, `waterfallSteps`):
 * their labels are the categories, a gap bar's included.
 */
export function axisChartLayout(
  block: DeckChartBlock,
  width: number,
  height: number,
  hidden: ReadonlySet<number>,
  locale: string,
  steps: readonly WaterfallStep[] | null = null,
): AxisChartLayout {
  return planAxisChart(block, width, height, hidden, locale, steps, false);
}

/**
 * `axisChartLayout`, `probing` when it is the plan of the same chart at
 * another size, asked whether more room would write values a stack hides -
 * a probe asks no further.
 */
function planAxisChart(
  block: DeckChartBlock,
  width: number,
  height: number,
  hidden: ReadonlySet<number>,
  locale: string,
  steps: readonly WaterfallStep[] | null,
  probing: boolean,
): AxisChartLayout {
  const stacked = isStacked(block);
  const horizontal = block.horizontal === true && (block.kind === "bar" || block.kind === "stacked_bar");
  const pointsOnly = block.kind === "line" || block.kind === "area";
  // The values' sizes (`value_label_size`) apart from the ticks' (`label_size`).
  const scale = chartValueScale(block);
  const tickSize = chartTextScale(block).tick;
  const labelOffset = pointsOnly ? POINT_LABEL_OFFSET : LABEL_OFFSET;
  const tickCount = height < DENSE_TICKS_BELOW ? 3 : 5;
  // Two units on one figure: two value axes on one grid.
  const twoAxes = hasSecondaryAxis(block);
  const scales: { primary: ValueAxisScale; secondary?: ValueAxisScale } = twoAxes
    ? dualAxisScales(block, tickCount)
    : { primary: valueAxisScale(block, tickCount) };
  const axis = scales.primary;
  const secondaryAxis = scales.secondary;
  const tick = axisTick(block, "primary", locale, axis.ticks);
  const labels = seriesLabels(block, locale);
  const label = (value: number, series = 0): string => (labels[series] ?? labels[0])(value);

  // Both axes are given their width outright (see `valueAxisWidth`): the value
  // axis from the widest tick it writes, a horizontal chart's category axis
  // from its longest category, within a share of the canvas. A right-hand
  // axis takes the room the last category label needed on the right.
  const categories = steps ? steps.map((step) => step.category) : block.categories;
  const count = Math.max(1, categories.length);
  const longestCategory = Math.max(0, ...categories.map((category) => textWidth(category, tickSize)));
  const readingAxisWidth = horizontal
    ? Math.ceil(Math.min(longestCategory + AXIS_PADDING, width * CATEGORY_AXIS_MAX_SHARE))
    : valueAxisWidth(axis.ticks, tick, tickSize);
  const secondaryTick = secondaryAxis ? axisTick(block, "secondary", locale, secondaryAxis.ticks) : null;
  const secondaryAxisWidth = secondaryAxis && secondaryTick ? valueAxisWidth(secondaryAxis.ticks, secondaryTick, tickSize) : 0;
  const marginRight = twoAxes ? PLOT_MARGIN_LEFT : PLOT_MARGIN_RIGHT;

  // Between two value axes, a value written over a line's first or last point
  // needs half its width inside the plot, or it runs into that axis' ticks.
  const pointPadding =
    pointsOnly && twoAxes && anyValuesAsked(block)
      ? Math.max(
          POINT_PADDING,
          Math.ceil(
            Math.max(
              0,
              ...block.series.flatMap((entry, series) =>
                hidden.has(series) || !seriesValuesAsked(block, series)
                  ? []
                  : entry.values.map((value) => labelWidth(label(value, series), scale.values[0])),
              ),
            ) / 2,
          ),
        )
      : POINT_PADDING;

  // Each category owns a band of the plot across the category axis (for a
  // line, the run between two neighbouring points; a horizontal chart's
  // categories share the plot HEIGHT, one row each). A category label wider
  // than its band - `Q4 24` eight times across a three-column dashboard panel
  // - would overprint its neighbours, so the axis then thins its ticks, first
  // and last kept.
  const across = horizontal
    ? Math.max(0, height - PLOT_MARGIN_TOP - PLOT_MARGIN_BOTTOM - CATEGORY_AXIS_HEIGHT)
    : Math.max(0, width - readingAxisWidth - secondaryAxisWidth - PLOT_MARGIN_LEFT - marginRight);
  const band = pointsOnly ? (across - 2 * pointPadding) / Math.max(1, count - 1) : across / count;
  const categoriesFit = horizontal ? tickSize + LABEL_GAP / 2 <= band : longestCategory + LABEL_GAP <= band;

  const bars = pointsOnly
    ? []
    : block.series.map((entry, index) => ({ index, drawn: drawnKind(block, entry.kind) })).filter(({ drawn }) => drawn === "bar");
  const gapShare = stacked || bars.length > 1 ? BAR_CATEGORY_GAP_DENSE : BAR_CATEGORY_GAP;
  const maxBarSize = horizontal ? MAX_BAR_SIZE_HORIZONTAL : MAX_BAR_SIZE;
  const visibleBars = bars.filter(({ index }) => !hidden.has(index)).map(({ index }) => index);
  let slot = barSlot(band, stacked ? 1 : Math.max(1, visibleBars.length), gapShare, maxBarSize);
  const labelled = pointsOnly ? block.series.map((_, index) => index).filter((index) => !hidden.has(index)) : visibleBars;

  // Along the value axis: the plot's length before any label takes room, and
  // where a value sits on it (0 at the axis floor, 1 at its top).
  const baseHigh = horizontal ? PLOT_MARGIN_RIGHT : PLOT_MARGIN_TOP;
  const valueLength = horizontal
    ? Math.max(0, width - readingAxisWidth - PLOT_MARGIN_LEFT - PLOT_MARGIN_RIGHT)
    : Math.max(0, height - CATEGORY_AXIS_HEIGHT - PLOT_MARGIN_TOP - PLOT_MARGIN_BOTTOM);
  const span = axis.max - axis.min || 1;
  const ratio = (value: number): number => (value - axis.min) / span;
  // A series on the right-hand axis sits on that axis' scale.
  const ratioOf = (series: number, value: number): number => {
    if (!secondaryAxis || seriesAxis(block.series[series]) !== "secondary") return ratio(value);
    return (value - secondaryAxis.min) / (secondaryAxis.max - secondaryAxis.min || 1);
  };

  // The values written past the end of their bar or above their point: a
  // stack's visible totals, one per direction (a category holding both signs
  // is two stacks with two totals); every visible value of a line; every
  // non-zero visible bar of the others (Recharts draws no rectangle for a
  // zero). On a bar chart each is written past a bar the drawing draws
  // (`drawnBars`): the bars are what its value must keep clear of, with the
  // other values - a bar whose series writes no value (its own `show_values`)
  // included. A combo's lines are planned apart, once the bars' values are.
  const asked = anyValuesAsked(block);
  const uprightBars = !horizontal && !pointsOnly;
  const marks: OuterMark[] = [];
  const drawnBars: DrawnBar[] = [];
  const drawBar = (at: number, from: number, to: number, category: number, lane = 0): void => {
    if (!pointsOnly) drawnBars.push({ at, low: Math.min(from, to), high: Math.max(from, to), category, lane });
  };
  const pastBar = (mark: OuterMark, at: number, from: number, to: number, category: number, lane = 0): OuterMark => {
    if (pointsOnly) return mark;
    drawBar(at, from, to, category, lane);
    return { ...mark, bar: drawnBars.length - 1 };
  };
  const insideCandidates: { series: number; category: number; value: number; text: string }[] = [];
  // Every direction of every stacked bar, for the values written past a stack.
  const stackRuns: StackRun[] = [];
  if (asked && steps) {
    // A waterfall writes each bar's change (a total, its level) over its
    // top, a fall too: its sign and tone say which way it went, and a fall
    // to the bottom of the axis would write its value over the category ticks.
    // A step of zero has no bar: its `€0` is what stands over its band.
    if (!hidden.has(0)) {
      steps.forEach((step, position) => {
        const text = waterfallLabel(step, labels[0]);
        const top = Math.max(step.start, step.end);
        const mark: OuterMark = { value: top, text, toward: 1, ratio: ratio(top) };
        marks.push(pastBar(mark, position * band + slot.first, ratio(step.start), ratio(step.end), position));
      });
    }
  } else if (asked) {
    block.categories.forEach((_, category) => {
      if (stacked) {
        let up = 0;
        let down = 0;
        const upSegments: number[] = [];
        const downSegments: number[] = [];
        for (const series of labelled) {
          const value = block.series[series].values[category] ?? 0;
          if (value > 0) {
            up += value;
            upSegments.push(series);
          } else if (value < 0) {
            down += value;
            downSegments.push(series);
          }
        }
        const at = category * band + slot.first;
        // A stacked area's total sits on its point; the one over it is placed like a line's value.
        const point = pointsOnly ? { category } : {};
        // A stack's totals follow the chart's `show_values`, its segments
        // their series' (a series' own word reaches a stacked bar).
        const totals = valuesAsked(block);
        const totalMarks: Record<1 | -1, number | null> = { 1: null, [-1]: null };
        if (up > 0 && totals) {
          const mark: OuterMark = { value: up, text: label(up), toward: 1, ratio: ratio(up), ...point };
          const keyed = pointsOnly ? { ...mark, key: totalKey(category) } : { ...mark, pastKey: pastBarKey("total", 1, 0, category) };
          marks.push(pastBar(keyed, at, ratio(0), ratio(up), category));
          totalMarks[1] = marks.length - 1;
        }
        if (down < 0 && totals) {
          const mark: OuterMark = { value: down, text: label(down), toward: -1, ratio: ratio(down), ...point };
          const keyed = pointsOnly ? mark : { ...mark, pastKey: pastBarKey("total", -1, 0, category) };
          marks.push(pastBar(keyed, at, ratio(down), ratio(0), category));
          totalMarks[-1] = marks.length - 1;
        }
        if (!pointsOnly) {
          for (const [run, toward] of [
            [upSegments, 1],
            [downSegments, -1],
          ] as const) {
            if (run.length === 0) continue;
            let reached = 0;
            const segments = run.map((series) => {
              const value = block.series[series].values[category] ?? 0;
              const from = reached;
              reached += value;
              return { series, value, text: label(value, series), from, to: reached };
            });
            const total = totalMarks[toward];
            stackRuns.push({ category, toward, total: total === null ? null : label(reached), mark: total, segments });
            // A segment alone in its direction IS the total: it is not written twice.
            if (run.length < 2 && totals) continue;
            for (const { series, value, text } of segments) {
              if (seriesValuesAsked(block, series)) insideCandidates.push({ series, category, value, text });
            }
          }
        }
        return;
      }
      labelled.forEach((series, lane) => {
        const value = block.series[series].values[category] ?? 0;
        const own = seriesValuesAsked(block, series);
        const text = label(value, series);
        const at = ratioOf(series, value);
        if (pointsOnly) {
          if (own) marks.push({ value, text, toward: 1, ratio: at, category, key: segmentKey(series, category) });
        } else if (value !== 0) {
          const centre = category * band + slot.first + lane * slot.pitch;
          if (!own) drawBar(centre, ratioOf(series, 0), at, category, lane);
          else {
            const mark: OuterMark = { value, text, toward: value < 0 ? -1 : 1, ratio: at, pastKey: pastBarKey("value", 1, series, category) };
            marks.push(pastBar(mark, centre, ratioOf(series, 0), at, category, lane));
          }
        }
      });
    });
  }
  const widest = (size: number): number => Math.max(0, ...marks.map((mark) => labelWidth(mark.text, size)));

  // Across the category axis: a label must keep clear of its neighbour's.
  // Every size of the chart's scale is tried upright first, largest first;
  // only a bar's value is then turned on its side - its thickness is what
  // must fit between two bars, its length what the plot gives up above.
  // On a horizontal chart the run of a value is what reaches the right edge
  // of the canvas past the longest bar - a stack's `100%` total past a bar at
  // the top of its axis - so the room it claims there is `labelRun`, the
  // estimate with the wide glyph's excess, never the bare glyph count.
  // A line's or an area's values with no room in one row at a size are tried
  // at that size alternating over and under their points, before the
  // next size down: the even categories' over, the odd ones' under, so two
  // neighbours in one row stand two points apart.
  const pitch = pointsOnly ? band : slot.pitch;
  const writtenUnder = (mark: OuterMark): boolean => mark.key !== undefined && (mark.category ?? 0) % 2 === 1;
  const candidates: Candidate[] = [];
  for (const size of scale.values) {
    if (horizontal) {
      candidates.push({ style: { size, rotate: false }, across: size + LABEL_HALO, along: (mark) => labelRun(mark.text, size) });
    } else {
      candidates.push({ style: { size, rotate: false }, across: widest(size) + (pointsOnly ? LABEL_GAP : LABEL_HALO), along: () => size });
      if (pointsOnly) candidates.push({ style: { size, rotate: false }, across: widest(size) + LABEL_GAP, along: () => size, alternate: true });
    }
  }
  if (uprightBars) {
    for (const size of scale.values) {
      candidates.push({ style: { size, rotate: true }, across: size * INK_EM + LABEL_HALO, along: (mark) => labelWidth(mark.text, size) });
    }
  }

  // Along the value axis: the room past the plot's high end that keeps every
  // label inside the canvas. A label of length L past an end at ratio r needs
  // r * plot + L <= plot + high + baseHigh - EDGE_PAD, with plot = length -
  // high: solved for `high` directly. A label past a NEGATIVE end runs toward
  // the category axis and is drawn inside its bar when the plot has no room
  // for it there (`outerLabelPlacement`), so it claims no margin; nor does a
  // value written under its point by an alternating candidate.
  const highRoom = (candidate: Candidate): number => {
    let high = 0;
    for (const mark of marks) {
      if (mark.toward !== 1 || (candidate.alternate && writtenUnder(mark))) continue;
      const r = mark.ratio;
      if (r <= 0) continue;
      const reach = labelOffset + candidate.along(mark);
      high = Math.max(high, (reach - (1 - r) * valueLength - (baseHigh - EDGE_PAD)) / r);
    }
    return Math.ceil(high);
  };
  const minPlot = Math.min(valueLength, Math.max(MIN_PLOT_PX, MIN_PLOT_SHARE * valueLength));
  const lineTooShort = pointsOnly && height < LABELLED_MIN_HEIGHT;
  const fitsAlong = (candidate: Candidate): boolean => {
    const claimed = highRoom(candidate);
    return claimed === 0 || valueLength - claimed >= minPlot;
  };

  // On an upright chart a value is placed where the drawing will put it -
  // past its bar, on the plot the candidate leaves - and must keep its halo
  // clear of every other value and of every bar but its own, inside the plot
  // across (the value axes' ticks stand outside it). Two values at different
  // heights do not touch however near their bars stand: the ARR and the net
  // new ARR of one quarter, a 20M bar beside a 2M one. Elsewhere a size is
  // held to the room between two neighbours (`across`).
  const plotLeft = PLOT_MARGIN_LEFT + readingAxisWidth;
  const plotRight = width - marginRight - secondaryAxisWidth;
  const plotBottom = height - PLOT_MARGIN_BOTTOM - CATEGORY_AXIS_HEIGHT;
  const rightmost = twoAxes ? plotRight : width - EDGE_PAD;
  const halo = LABEL_HALO / 2;
  const haloed = (ink: Rect): Rect => ({ left: ink.left - halo, top: ink.top - halo, right: ink.right + halo, bottom: ink.bottom + halo });
  // The bars as drawn on the plot `claimed` leaves - a horizontal chart's
  // run from the left-hand axis, one row each - and each value past its bar
  // as painted there, halo included (`null` for a value with no bar).
  const barsOn = (claimed: number): Rect[] => {
    const side = slot.size / 2;
    if (horizontal) {
      const length = Math.max(0, valueLength - claimed);
      return drawnBars.map((bar) => ({
        left: plotLeft + bar.low * length,
        right: plotLeft + bar.high * length,
        top: PLOT_MARGIN_TOP + bar.at - side,
        bottom: PLOT_MARGIN_TOP + bar.at + side,
      }));
    }
    const length = Math.max(0, plotBottom - (PLOT_MARGIN_TOP + claimed));
    const y = (r: number): number => plotBottom - r * length;
    return drawnBars.map((bar) => ({
      left: plotLeft + bar.at - side,
      right: plotLeft + bar.at + side,
      top: y(bar.high),
      bottom: y(bar.low),
    }));
  };
  const paintedPastBars = (style: LabelStyle, claimed: number): { bars: Rect[]; values: (Rect | null)[] } => {
    const top = PLOT_MARGIN_TOP + claimed;
    const bars = barsOn(claimed);
    const box: PlotBox = { left: plotLeft, top, right: plotRight, bottom: plotBottom };
    const values = marks.map((mark) => {
      const own = bars[mark.bar ?? -1];
      if (!own) return null;
      const bar = { x: own.left, y: own.top, width: slot.size, height: own.bottom - own.top };
      const placement = outerLabelPlacement(bar, mark.toward, style, mark.text, false, box, labelOffset);
      return haloed(labelInk(placement, mark.text, style.size));
    });
    return { bars, values };
  };
  const clearOfNeighbours = (style: LabelStyle, claimed: number): boolean => {
    const { bars, values } = paintedPastBars(style, claimed);
    const written: Rect[] = [];
    for (const [index, painted] of values.entries()) {
      if (!painted) return false;
      if (painted.left < plotLeft || painted.right > rightmost) return false;
      if (written.some((other) => overlap(painted, other))) return false;
      const own = marks[index].bar;
      if (bars.some((rect, bar) => bar !== own && rect.bottom > rect.top && overlap(painted, rect))) return false;
      written.push(painted);
    }
    return true;
  };
  // An alternating candidate has twice a point's room across, and
  // every value it writes under its point - hanging `POINT_LABEL_OFFSET`
  // below it, halo included (`pointLabelPlacement`) - must stay above the
  // category axis on the plot the candidate leaves.
  const underReach = (size: number): number => POINT_LABEL_OFFSET + size * INK_EM + halo;
  const underStaysInPlot = (candidate: Candidate): boolean => {
    const drawnLength = valueLength - highRoom(candidate);
    return marks.every((mark) => !writtenUnder(mark) || mark.ratio * drawnLength >= underReach(candidate.style.size));
  };
  const clears = (candidate: Candidate): boolean => {
    if (uprightBars) return clearOfNeighbours(candidate.style, highRoom(candidate));
    if (candidate.alternate) return candidate.across <= 2 * pitch && underStaysInPlot(candidate);
    return candidate.across <= pitch;
  };

  // The value axes' ticks as Recharts writes them on the plot `claimed`
  // leaves: right-aligned against the left-hand axis, left-aligned against
  // the right-hand one, centred on their level.
  const tickInk = tickSize * INK_EM;
  const valueTicksOn = (claimed: number): Rect[] => {
    const drawnLength = Math.max(0, plotBottom - (PLOT_MARGIN_TOP + claimed));
    const tickBoxes = (scaleOf: ValueAxisScale, write: (value: number) => string, side: "left" | "right"): Rect[] =>
      scaleOf.ticks.map((value) => {
        const y = plotBottom - ((value - scaleOf.min) / (scaleOf.max - scaleOf.min || 1)) * drawnLength;
        const run = textWidth(write(value), tickSize);
        const left = side === "left" ? plotLeft - TICK_TEXT_GAP - run : plotRight + TICK_TEXT_GAP;
        return { left, right: left + run, top: y - tickInk / 2, bottom: y + tickInk / 2 };
      });
    return [...tickBoxes(axis, tick, "left"), ...(secondaryAxis && secondaryTick ? tickBoxes(secondaryAxis, secondaryTick, "right") : [])];
  };

  // A segment of a stack holds its value inside at `size` when the value is
  // no longer than the segment on a plot `plotLength` long (`insideOn`).
  const segmentHolds = (entry: (typeof insideCandidates)[number], size: number, plotLength: number): boolean => {
    const length = (Math.abs(entry.value) / span) * plotLength;
    const needed = horizontal ? labelWidth(entry.text, size) + 2 * SEGMENT_PAD_X : size + 2 * SEGMENT_PAD_Y;
    return needed <= length;
  };

  // `value_label_fit`: values that fit upright at the size asked, in
  // one row, are written so whatever the word (`auto`'s first candidate) -
  // on a horizontal stack, with every segment's value inside its
  // segment at that size too. Otherwise `stagger` and `upright` keep that
  // size, upright, and the bars give way to the values first, so a value
  // clears the bars beside it and the value of the next group's first bar
  // clears the one of this group's last - two neighbours that never touch
  // need no lift and leave nothing out. A group's bars narrow and stand just
  // far enough apart for both, never under `GIVE_WAY_MIN_SHARE` of their
  // width nor further apart than an even share of the band; when the groups
  // cannot be parted that way, the bars only clear the values beside them.
  // Recharts is then given the width and the gap outright (`fixedBarSlot`).
  // What a value must clear across the bars is its width upright, the
  // height of its figures on a horizontal chart, halo included.
  // A group of upright bars gives way to nothing: it keeps the room
  // `auto` gives it, so each category still reads as one group, and its
  // values clear only one another.
  const fitAsked = !pointsOnly && (marks.length > 0 || (horizontal && insideCandidates.length > 0)) ? valueLabelFit(block) : "auto";
  const fitStyle: LabelStyle = { size: scale.values[0], rotate: false };
  const outerAsAsked = marks.length === 0 || (fitsAlong(candidates[0]) && clears(candidates[0]));
  const segmentsAsAsked = (): boolean => {
    if (!horizontal || insideCandidates.length === 0) return true;
    if (fitStyle.size + 2 * SEGMENT_PAD_Y > slot.size) return false;
    const plotLength = valueLength - (marks.length > 0 ? highRoom(candidates[0]) : 0);
    return insideCandidates.every((entry) => segmentHolds(entry, fitStyle.size, plotLength));
  };
  const fit = fitAsked !== "auto" && !(outerAsAsked && segmentsAsAsked()) ? fitAsked : "auto";
  const lanes = stacked ? 1 : Math.max(1, visibleBars.length);
  const groupedUpright = block.kind === "bar" && !stacked && !horizontal && lanes > 1;
  const fitAcross = (horizontal ? fitStyle.size * INK_EM : widest(fitStyle.size)) + LABEL_HALO;
  let gaveWay: AxisChartLayout["gaveWay"] = null;
  if (fit !== "auto" && marks.length > 0 && !groupedUpright) {
    const half = fitAcross / 2;
    const share = band / lanes;
    const across = (spacing: number): number => band - (lanes - 1) * spacing;
    const clearOfBars = (size: number, spacing: number): boolean =>
      (lanes === 1 || spacing - size / 2 >= half) && across(spacing) - size / 2 >= half;
    const groupsApart = (spacing: number): boolean => lanes === 1 || across(spacing) >= 2 * half;
    if (!clearOfBars(slot.size, slot.pitch) || !groupsApart(slot.pitch)) {
      const floor = Math.ceil(slot.size * GIVE_WAY_MIN_SHARE);
      const widthFor = (spread: number): number => Math.min(slot.size, Math.floor(2 * (spread - half)));
      const parted = lanes > 1 ? widthFor(Math.min(share, (band - 2 * half) / (lanes - 1))) : widthFor(share);
      const barWidth = parted >= floor ? parted : Math.max(floor, widthFor(share));
      const barPitch = lanes > 1 ? Math.min(share, Math.max(barWidth + BAR_GAP, half + barWidth / 2)) : band;
      slot = fixedBarSlot(band, lanes, barWidth, lanes > 1 ? barPitch - barWidth : 0);
      const within = lanes > 1 ? slot.pitch - slot.size : 0;
      gaveWay = { width: slot.size, gap: within, categoryGap: band - lanes * slot.size - (lanes - 1) * within };
      for (const bar of drawnBars) bar.at = bar.category * band + slot.first + bar.lane * slot.pitch;
    }
  }

  // `value_label_fit` stagger / upright, on the plot `claimed`
  // leaves: each value past its bar keeps its halo clear of every value
  // written before it, of every bar but its own, of the value axes' ticks
  // and of a combo's lines, inside the canvas - a group of upright bars'
  // values of no bar: one overhanging a taller neighbour is written
  // over its fill in its halo, as a value on a point is. The near row is written
  // first, along the category axis: each value in its place when nothing
  // there is in its way - of two neighbours that would touch, the
  // second waits. Under `stagger` the values that wait are then written on
  // the far row, along the axis, a row out (`rowStep`) and further, just
  // clear of what is still in their way; a value lifted so never moves a
  // neighbour off the near row. Under `upright` a value that waits
  // is tried a row out - just past the values it would touch in its place,
  // as `stagger` lifts it there - and no further: written there when nothing
  // is in its way and the room the plot already gives the values above the
  // bars holds it (`fitClaim`) - the bars never move for it - else left out.
  // `wanted` is how far each value is lifted where it clears what is beside
  // it, written or not for want of height above the plot.
  // On a horizontal chart a lift runs along the value's row, to the
  // right of a rise: a value that waits has no far row to start on, and moves
  // just past what is in its way - under `stagger`; `upright` leaves it out.
  // Its ticks stand outside the plot, under it and left of it, so only
  // values and bars are in the way; past the right-hand edge it is short of
  // room, which the plot gives up as it does for the totals. A value there
  // claims its run (`labelRun`, the wide glyph's excess kept at the end it
  // reads toward): the next value along its row, or a thin segment's after a
  // total, starts past its `%`.
  const rowStep = horizontal ? 0 : Math.ceil(fitStyle.size * INK_EM + LABEL_HALO + LABEL_CLEARANCE);
  const runExcess = horizontal ? (WIDE_GLYPH_EM - LABEL_GLYPH_EM) * fitStyle.size : 0;
  const fitOrder = marks
    .map((_, index) => index)
    .sort((a, b) => (drawnBars[marks[a].bar ?? 0]?.at ?? 0) - (drawnBars[marks[b].bar ?? 0]?.at ?? 0) || a - b);
  // A combo's lines as drawn on the plot `claimed` leaves, through each
  // point at the centre of its band: their stroke, kept `LABEL_CLEARANCE`
  // from, is in the way of the values past the bars as a bar is.
  const comboLines =
    block.kind === "combo"
      ? block.series.flatMap((entry, index) => (drawnKind(block, entry.kind) === "line" && !hidden.has(index) ? [index] : []))
      : [];
  const lineStrokesOn = (claimed: number): Rect[] => {
    const drawnLength = Math.max(0, plotBottom - (PLOT_MARGIN_TOP + claimed));
    return comboLines.flatMap((series) => {
      const points = block.categories.map((_, category): Point => [
        plotLeft + (category + 0.5) * band,
        plotBottom - ratioOf(series, block.series[series].values[category] ?? 0) * drawnLength,
      ]);
      return strokeBoxes(monotoneCurve(points), LINE_WIDTH, LABEL_CLEARANCE);
    });
  };
  interface FitPlan {
    wanted: (number | null)[];
    painted: (Rect | null)[];
    crowded: number;
    tall: number;
  }
  const fitPlans = new Map<number, FitPlan>();
  const fitPlanAt = (claimed: number): FitPlan => {
    const known = fitPlans.get(claimed);
    if (known) return known;
    const bars = barsOn(claimed);
    const ticks = horizontal ? [] : valueTicksOn(claimed);
    const strokes = lineStrokesOn(claimed);
    const box: PlotBox = horizontal
      ? { left: plotLeft, top: PLOT_MARGIN_TOP, right: plotLeft + Math.max(0, valueLength - claimed), bottom: plotBottom }
      : { left: plotLeft, top: PLOT_MARGIN_TOP + claimed, right: plotRight, bottom: plotBottom };
    const paint = (index: number, lift: number): Rect => {
      const mark = marks[index];
      const own = bars[mark.bar ?? 0];
      const bar = { x: own.left, y: own.top, width: horizontal ? own.right - own.left : slot.size, height: own.bottom - own.top };
      const placement = outerLabelPlacement(bar, mark.toward, fitStyle, mark.text, horizontal, box, labelOffset + lift);
      const painted = haloed(labelInk(placement, mark.text, fitStyle.size));
      if (!horizontal) return painted;
      return placement.anchor === "end" ? { ...painted, left: painted.left - runExcess } : { ...painted, right: painted.right + runExcess };
    };
    const plan: FitPlan = { wanted: marks.map(() => null), painted: marks.map(() => null), crowded: 0, tall: 0 };
    const written: Rect[] = [];
    // What stands in a value's column (its row on a horizontal chart) - a lift
    // moves it along the value axis only - and what of it, or of the values
    // written, a box would touch.
    const obstacles = (index: number) => {
      const column = paint(index, 0);
      const across = (rect: Rect): boolean =>
        horizontal ? rect.top < column.bottom && column.top < rect.bottom : rect.left < column.right && column.left < rect.right;
      const drawn = (rect: Rect): boolean => (horizontal ? rect.right > rect.left : rect.bottom > rect.top);
      const standing = [
        ...(groupedUpright ? [] : bars.filter((rect, bar) => bar !== marks[index].bar && drawn(rect) && across(rect))),
        ...ticks.filter(across),
        ...strokes.filter(across),
      ];
      const inTheWay = (rect: Rect): Rect | undefined =>
        written.find((other) => overlap(rect, other)) ?? standing.find((other) => overlap(rect, other));
      return { standing, inTheWay };
    };
    const offCanvas = (painted: Rect): boolean =>
      horizontal
        ? painted.top < EDGE_PAD - halo || painted.bottom > height - EDGE_PAD + halo
        : painted.left < EDGE_PAD - halo || painted.right > width - EDGE_PAD + halo;
    const offCanvasAlong = (painted: Rect): boolean =>
      horizontal
        ? painted.left + halo < EDGE_PAD || painted.right - halo > width - EDGE_PAD
        : painted.top + halo < EDGE_PAD || painted.bottom - halo > height - EDGE_PAD;
    const write = (index: number, lift: number, painted: Rect): void => {
      plan.wanted[index] = lift;
      if (offCanvasAlong(painted)) {
        plan.tall += 1;
        return;
      }
      plan.painted[index] = painted;
      written.push(painted);
    };
    // How much further a value painted at `painted` moves along its lift to stand clear of `blocking`.
    const pastIt = (painted: Rect, blocking: Rect, toward: 1 | -1): number =>
      horizontal
        ? toward === 1
          ? blocking.right + LABEL_CLEARANCE - painted.left
          : painted.right + LABEL_CLEARANCE - blocking.left
        : toward === 1
          ? painted.bottom + LABEL_CLEARANCE - blocking.top
          : blocking.bottom + LABEL_CLEARANCE - painted.top;
    const oneRow = fit === "upright" && !horizontal;
    const farRow: number[] = [];
    for (const index of fitOrder) {
      const painted = paint(index, 0);
      if (offCanvas(painted)) plan.crowded += 1;
      else if (!obstacles(index).inTheWay(painted)) write(index, 0, painted);
      else if (fit === "stagger" || oneRow) farRow.push(index);
      else plan.crowded += 1;
    }
    for (const index of farRow) {
      const { standing, inTheWay } = obstacles(index);
      const toward = marks[index].toward;
      let lift = rowStep;
      if (oneRow) {
        // A row out, past the values it would touch in its place, and no further.
        const place = paint(index, 0);
        for (const other of written) if (overlap(place, other)) lift = Math.max(lift, pastIt(place, other, toward));
        const painted = paint(index, lift);
        if (inTheWay(painted) || offCanvasAlong(painted) || fitClaim(index, lift) > claimed) plan.crowded += 1;
        else write(index, lift, painted);
        continue;
      }
      let painted = paint(index, lift);
      // Each step lifts it past one thing in its way, which it never meets again.
      let steps = written.length + standing.length;
      for (let blocking = inTheWay(painted); blocking && steps > 0; blocking = inTheWay(painted)) {
        steps -= 1;
        lift += Math.max(pastIt(painted, blocking, toward), MIN_RISE_STEP);
        painted = paint(index, lift);
      }
      if (inTheWay(painted)) plan.crowded += 1;
      else write(index, lift, painted);
    }
    fitPlans.set(claimed, plan);
    return plan;
  };
  // The room past the plot's high end a value lifted by `lift` needs
  // (`highRoom`, its reach its offset, its lift and its size - its run on a
  // horizontal chart), the room the lifted values need, and the most the
  // plot gives up.
  const fitClaim = (index: number, lift: number): number => {
    const mark = marks[index];
    if (mark.toward !== 1 || mark.ratio <= 0) return 0;
    const reach = labelOffset + lift + (horizontal ? labelRun(mark.text, fitStyle.size) : fitStyle.size);
    return (reach - (1 - mark.ratio) * valueLength - (baseHigh - EDGE_PAD)) / mark.ratio;
  };
  const fitHigh = (wanted: readonly (number | null)[]): number => {
    let claim = 0;
    wanted.forEach((lift, index) => {
      if (lift !== null) claim = Math.max(claim, fitClaim(index, lift));
    });
    return Math.ceil(claim);
  };
  const maxClaim = Math.max(0, Math.floor(valueLength - minPlot));

  let outer: LabelStyle | null = null;
  let alternate = false;
  let high = 0;
  let reason: HiddenValuesReason | null = null;
  let room = 0;
  let need = 0;
  let fitPlan: FitPlan | null = null;
  if (marks.length > 0 && fit !== "auto") {
    // From the room every value needs in its place, the plot gives up what
    // the values lifted on it need, re-planned until the room settles.
    high = Math.min(maxClaim, fitHigh(marks.map(() => 0)));
    fitPlan = fitPlanAt(high);
    for (let step = 0; step < MAX_ROOM_STEPS; step += 1) {
      const claim = Math.min(maxClaim, fitHigh(fitPlan.wanted));
      if (claim <= high) break;
      high = claim;
      fitPlan = fitPlanAt(high);
    }
    outer = fitStyle;
  } else if (marks.length > 0) {
    const fitting = lineTooShort ? undefined : candidates.find((candidate) => fitsAlong(candidate) && clears(candidate));
    if (fitting) {
      outer = fitting.style;
      alternate = fitting.alternate === true;
      high = highRoom(fitting);
    } else if (lineTooShort) {
      reason = "short";
      room = height;
      need = LABELLED_MIN_HEIGHT;
    } else {
      const clear = candidates.filter(clears);
      if (clear.length === 0) {
        reason = "narrow";
        room = Math.floor(pitch);
        need = Math.ceil(Math.min(...candidates.map((candidate) => candidate.across)));
      } else {
        reason = "short";
        room = Math.floor(valueLength);
        need = Math.ceil(Math.min(...clear.map(highRoom)) + minPlot);
      }
    }
  }

  // A combo's lines: every point's value, upright, in the largest
  // size that clears the next point's value and reads no larger than the
  // bars' - over its point, the plot giving up the room it needs there.
  const lineSeries =
    asked && block.kind === "combo"
      ? block.series.flatMap((entry, index) =>
          drawnKind(block, entry.kind) === "line" && !hidden.has(index) && seriesValuesAsked(block, index) ? [index] : [],
        )
      : [];
  const points = lineSeries.flatMap((series) =>
    block.categories.map((_, category) => {
      const value = block.series[series].values[category] ?? 0;
      return { series, category, text: label(value, series), ratio: ratioOf(series, value) };
    }),
  );
  let pointStyle: LabelStyle | null = null;
  if (points.length > 0) {
    const widestPoint = (size: number): number => Math.max(0, ...points.map((point) => labelWidth(point.text, size)));
    const pointHigh = (size: number): number =>
      Math.ceil(
        Math.max(
          0,
          ...points.map(({ ratio: r }) =>
            r > 0 ? (POINT_LABEL_OFFSET + size - (1 - r) * valueLength - (baseHigh - EDGE_PAD)) / r : 0,
          ),
        ),
      );
    const largest = outer?.size ?? scale.values[0];
    const across = scale.values.filter((size) => size <= largest && widestPoint(size) + LABEL_GAP <= band);
    const fitting = across.find((size) => pointHigh(size) <= high || valueLength - pointHigh(size) >= minPlot);
    if (fitting !== undefined) {
      pointStyle = { size: fitting, rotate: false };
      high = Math.max(high, pointHigh(fitting));
    } else if (reason === null && across.length === 0) {
      reason = "narrow";
      room = Math.floor(band);
      need = Math.ceil(widestPoint(scale.values[scale.values.length - 1]) + LABEL_GAP);
    } else if (reason === null) {
      reason = "short";
      room = Math.floor(valueLength);
      need = Math.ceil(Math.min(...across.map(pointHigh)) + minPlot);
    }
  }
  // Values turned on their side are said so (advisory): the smallest size
  // they were tried upright in, and the room the widest of them lacked there.
  const smallest = scale.values[scale.values.length - 1];
  const turned = outer?.rotate
    ? { size: smallest, room: Math.floor(pitch), need: Math.ceil(widest(smallest) + LABEL_HALO) }
    : null;

  // Where each value that sits on a point goes: a line's or an area's (a
  // stacked area's total over its point), and a combo line's, planned once
  // the bars' values are. Over its point; under it when something holds the
  // room over it; else raised straight up in its point's column, clear of
  // what is in its way. Each keeps `LABEL_CLEARANCE` past its halo from every
  // other value and the value axes' ticks - never from a bar: it stays next
  // to its point, on the bar's fill with its halo where the point falls in a
  // bar - and one that would run into the ticks is slid along the
  // category axis, back into the plot. One that touches nothing over its
  // point stays exactly there. A value an alternating plan writes
  // under its point tries under first, then over, then the raise.
  const placeOnPoints = (claimed: number) => {
    const drawnLength = Math.max(0, plotBottom - (PLOT_MARGIN_TOP + claimed));
    const ticks = valueTicksOn(claimed);
    // How much higher the canvas would have to reach for the values it has no room for.
    let deficit = 0;
    const placePoint = (dot: LabelBox, text: string, size: number, taken: readonly Rect[], underFirst = false) => {
      const painted = (place: PointPlacement): Rect => haloed(labelInk(pointLabelPlacement(dot, place), text, size));
      // Only text is in a value's way: a bar is not - its halo keeps it
      // legible on a bar's fill and across its edge, next to its point.
      const inTheWay = (rect: Rect): Rect | undefined => {
        const room = grown(rect, LABEL_CLEARANCE);
        return taken.find((other) => overlap(room, other)) ?? ticks.find((other) => overlap(room, other));
      };
      const slid = (place: PointPlacement): PointPlacement => {
        const room = grown(painted(place), LABEL_CLEARANCE);
        const reached = ticks.find((other) => overlap(room, other));
        if (!reached) return place;
        const shift = reached.right <= dot.x ? reached.right - room.left : reached.left - room.right;
        return { ...place, shift: place.shift + shift };
      };
      // A value under its point also stays inside the plot, above the category axis.
      const clearAt = (place: PointPlacement): boolean =>
        (!place.under || painted(place).bottom <= plotBottom) && !inTheWay(painted(place));
      const over = slid({ under: false, rise: 0, shift: 0 });
      const under = slid({ under: true, rise: 0, shift: 0 });
      const [first, second] = underFirst ? [under, over] : [over, under];
      if (clearAt(first)) return first;
      if (clearAt(second)) return second;
      // Each step raises it over one thing in its way, which it never meets
      // again, so it ends over all of them in at most one step per thing.
      let place = over;
      let steps = taken.length + ticks.length;
      for (let blocking = inTheWay(painted(place)); blocking; blocking = inTheWay(painted(place))) {
        if (steps === 0) return null;
        steps -= 1;
        const step = painted(place).bottom + LABEL_CLEARANCE - blocking.top;
        place = { ...place, rise: place.rise + Math.max(step, MIN_RISE_STEP) };
      }
      const top = painted(place).top;
      if (top >= EDGE_PAD) return place;
      deficit = Math.max(deficit, EDGE_PAD - top);
      return null;
    };

    const placed = new Map<string, PointPlacement>();
    let unplaced = 0;
    let linesUnplaced = 0;
    const pointsStyle = pointsOnly ? outer : null;
    if (pointsStyle) {
      const box: PlotBox = { left: plotLeft, top: PLOT_MARGIN_TOP + claimed, right: plotRight, bottom: plotBottom };
      const pointAt = (mark: OuterMark): LabelBox => ({
        x: plotLeft + pointPadding + (count > 1 ? (mark.category ?? 0) * band : band / 2),
        y: plotBottom - mark.ratio * drawnLength,
        width: 0,
        height: 0,
      });
      const paint = (placement: LabelPlacement, text: string): Rect => haloed(labelInk(placement, text, pointsStyle.size));
      // A negative stack total keeps its place under its point, in the way of the others.
      const taken: Rect[] = marks
        .filter((mark) => !mark.key)
        .map((mark) =>
          paint(outerLabelPlacement(pointAt(mark), mark.toward, pointsStyle, mark.text, false, box, labelOffset), mark.text),
        );
      for (const mark of marks) {
        if (!mark.key) continue;
        const dot = pointAt(mark);
        const place = placePoint(dot, mark.text, pointsStyle.size, taken, alternate && writtenUnder(mark));
        if (!place) {
          unplaced += 1;
          continue;
        }
        taken.push(paint(pointLabelPlacement(dot, place), mark.text));
        placed.set(mark.key, place);
      }
    }
    if (pointStyle) {
      const size = pointStyle.size;
      const pastBars = fitPlan ? fitPlanAt(claimed).painted : outer ? paintedPastBars(outer, claimed).values : [];
      const taken: Rect[] = pastBars.filter((rect): rect is Rect => rect !== null);
      for (const point of points) {
        const dot = { x: plotLeft + (point.category + 0.5) * band, y: plotBottom - point.ratio * drawnLength, width: 0, height: 0 };
        const place = placePoint(dot, point.text, size, taken);
        if (!place) {
          linesUnplaced += 1;
          continue;
        }
        taken.push(haloed(labelInk(pointLabelPlacement(dot, place), point.text, size)));
        placed.set(segmentKey(point.series, point.category), place);
      }
    }
    return { placed, unplaced, linesUnplaced, deficit };
  };

  // A value raised past the top of the canvas has the plot give up the room
  // it lacks there, as the values past the bars do - while the plot keeps its
  // `minPlot` and the bars' values still clear each other on the shorter
  // plot. What still finds no room is counted.
  // The bars shrink with the plot and carry the values over them down by a
  // share of the room given up, so the search may take a few steps. Fitted
  // values past the bars are planned anew on each plot tried, and
  // the plot gives up no room that leaves more of them out.
  const fitHidden = (plan: FitPlan): number => plan.crowded + plan.tall;
  const barsStillClear = (claimed: number): boolean => {
    if (fitPlan) return fitHidden(fitPlanAt(claimed)) <= fitHidden(fitPlanAt(high));
    return !(outer && uprightBars && !clearOfNeighbours(outer, claimed));
  };
  let onPoints = placeOnPoints(high);
  const missing = (plan: typeof onPoints): number => plan.unplaced + plan.linesUnplaced;
  let trial = onPoints;
  let claimed = high;
  for (let step = 0; trial.deficit > 0 && step < MAX_ROOM_STEPS; step += 1) {
    claimed += Math.ceil(trial.deficit);
    if (valueLength - claimed < minPlot || !barsStillClear(claimed)) break;
    trial = placeOnPoints(claimed);
    if (missing(trial) < missing(onPoints)) {
      high = claimed;
      onPoints = trial;
    }
  }
  const { placed, unplaced, linesUnplaced } = onPoints;
  // What the fitted values past the bars left out is said first, as a chart's
  // own values are: the reason they had, else the line's (`overlap`).
  if (fitPlan) {
    fitPlan = fitPlanAt(high);
    if (fitPlan.crowded > 0) {
      reason = "upright";
      room = Math.floor(lanes > 1 ? slot.pitch : band);
      need = Math.ceil(fitAcross);
    } else if (fitPlan.tall > 0) {
      reason = "short";
      room = Math.floor(valueLength);
      need = Math.ceil(fitHigh(fitPlan.wanted) + minPlot);
    }
  }
  if (unplaced + linesUnplaced > 0 && reason === null) reason = "overlap";

  // Inside the segments of a stacked bar: one size for every segment of the
  // chart. A size must fit the widest value across the bar; of those, the one
  // that writes the most segments (each must also be long enough along the
  // value axis), the larger on a tie - never under the scale's `segmentFloor`
  // while a size at or above it fits across. A horizontal stack fitted by
  // `value_label_fit` has one size, the one asked: a segment too short
  // for its value there is written past its stack (`stagger`) or left out
  // (`upright`), never set smaller.
  const segmentFit: DeckValueLabelFit = horizontal ? fit : "auto";
  const thickness = slot.size;
  const widestInside = (size: number): number => Math.max(0, ...insideCandidates.map((entry) => labelWidth(entry.text, size)));
  // A segment never reads larger than the total past its stack.
  const largest = outer?.size ?? scale.values[0];
  const acrossFit = (segmentFit === "auto" ? scale.values : [fitStyle.size]).filter(
    (size) =>
      size <= largest &&
      (horizontal ? size + 2 * SEGMENT_PAD_Y <= thickness : widestInside(size) + 2 * SEGMENT_PAD_X <= thickness),
  );
  const readable = acrossFit.some((size) => size >= scale.segmentFloor)
    ? acrossFit.filter((size) => size >= scale.segmentFloor)
    : acrossFit;
  const insideOn = (plotLength: number) => {
    let inner: LabelStyle | null = null;
    let segments = new Set<string>();
    for (const size of readable) {
      const held = new Set<string>();
      for (const entry of insideCandidates) {
        if (segmentHolds(entry, size, plotLength)) held.add(segmentKey(entry.series, entry.category));
      }
      if (held.size > segments.size) {
        inner = { size, rotate: false };
        segments = held;
      }
    }
    return { inner, segments };
  };

  // Past the stack: a segment with no room inside writes its value
  // after its stack's total - above an upright stack, right of a horizontal
  // one, under or left of a negative one - in its series' colour, a leader
  // running from the segment's side out to a lane beside the stack and on to
  // the text. The values of one stack are stacked outward, the segment
  // nearest the end first, each lane a step further out than the one before:
  // a leader that starts further from the end reaches a value further out,
  // so no two leaders of a stack cross. Each value and each leg keeps clear
  // of every bar, every value written and every other stack's leaders, and
  // stays inside the plot across; one that runs past the canvas past the
  // bars has the plot give up the room it lacks, as the totals do. One with
  // no room is tried no further out: the next segment takes its place.
  const candidateKeys = new Set(insideCandidates.map((entry) => segmentKey(entry.series, entry.category)));
  const annotate = (size: number, claimed: number, held: ReadonlySet<string>) => {
    const length = Math.max(0, valueLength - claimed);
    const along = (value: number): number => (horizontal ? plotLeft + ratio(value) * length : plotBottom - ratio(value) * length);
    const box: PlotBox = horizontal
      ? { left: plotLeft, top: PLOT_MARGIN_TOP, right: plotLeft + length, bottom: plotBottom }
      : { left: plotLeft, top: PLOT_MARGIN_TOP + claimed, right: plotRight, bottom: plotBottom };
    const centreOf = (run: StackRun): number => (horizontal ? PLOT_MARGIN_TOP : plotLeft) + run.category * band + slot.first;
    const spanned = (from: number, to: number, centre: number): Rect => {
      const [a, b] = [along(from), along(to)];
      const half = thickness / 2;
      return horizontal
        ? { left: Math.min(a, b), right: Math.max(a, b), top: centre - half, bottom: centre + half }
        : { left: centre - half, right: centre + half, top: Math.min(a, b), bottom: Math.max(a, b) };
    };
    const stackBars = stackRuns.map((run) => spanned(0, run.segments[run.segments.length - 1].to, centreOf(run)));
    // Each total where it is drawn: lifted or left out by a fit, else past its bar.
    const fitted = fitPlan ? fitPlanAt(claimed).painted : null;
    const totalInks = stackRuns.map((run, index) => {
      if (!outer || run.total === null) return null;
      if (fitted) {
        const painted = run.mark === null ? null : fitted[run.mark];
        return painted ? grown(painted, -halo) : null;
      }
      const bar = stackBars[index];
      const where = { x: bar.left, y: bar.top, width: bar.right - bar.left, height: bar.bottom - bar.top };
      return labelInk(outerLabelPlacement(where, run.toward, outer, run.total, horizontal, box, labelOffset), run.total, outer.size);
    });
    const taken: Rect[] = totalInks.flatMap((ink) => (ink ? [haloed(ink)] : []));
    const legs: { run: number; rect: Rect }[] = [];
    const annotations = new Map<string, SegmentAnnotation>();
    let deficit = 0;
    const inkHeight = size * INK_EM;
    const waits = stackRuns.map((run) =>
      run.segments
        .filter((segment) => {
          const key = segmentKey(segment.series, run.category);
          return candidateKeys.has(key) && !held.has(key);
        })
        .reverse(),
    );
    const waitingAt = new Set(stackRuns.flatMap((run, index) => (waits[index].length > 0 ? [`${run.category}:${run.toward}`] : [])));
    stackRuns.forEach((run, index) => {
      const waiting = waits[index];
      if (waiting.length === 0) return;
      const bar = stackBars[index];
      const total = totalInks[index];
      const centre = centreOf(run);
      const out = run.toward === 1;
      const end = run.segments[run.segments.length - 1].to;
      let next: number;
      if (horizontal) {
        const totalPast = total !== null && (out ? total.left >= bar.right : total.right <= bar.left);
        if (out) next = totalPast && total ? total.right + 2 * halo + LABEL_CLEARANCE : bar.right + labelOffset;
        else next = totalPast && total ? total.left - 2 * halo - LABEL_CLEARANCE : bar.left - labelOffset;
      } else {
        const totalPast = total !== null && (out ? total.bottom <= bar.top : total.top >= bar.bottom);
        if (out) next = totalPast && total ? total.top - 2 * halo - LABEL_CLEARANCE : bar.top - labelOffset;
        else next = totalPast && total ? total.bottom + 2 * halo + LABEL_CLEARANCE : bar.bottom + labelOffset;
      }
      // The lanes run beside the stack, past its side, its total and its
      // values, in the gap it owns - over a horizontal stack (the first
      // row's is the plot's top margin), right of an upright one (the last
      // stack's is the plot's right margin). The gap on its other side is its
      // neighbour's own: taken only when that neighbour writes nothing past
      // its stack, so no stack's values crowd out the next one's.
      const widestHere = Math.max(...waiting.map((segment) => labelWidth(segment.text, size)));
      const owner = `${run.category + (horizontal ? 1 : -1)}:${run.toward}`;
      const sides = waitingAt.has(owner) ? ([horizontal ? -1 : 1] as const) : ([horizontal ? -1 : 1, horizontal ? 1 : -1] as const);
      const tracks = sides.map((side) => {
        const reach = horizontal ? inkHeight / 2 : widestHere / 2;
        const [near, far] = horizontal ? [bar.top, bar.bottom] : [bar.left, bar.right];
        const [totalNear, totalFar] = total ? (horizontal ? [total.top, total.bottom] : [total.left, total.right]) : [near, far];
        const base =
          side === 1
            ? Math.max(far, totalFar + halo, centre + reach + halo) + LEADER_INSET
            : Math.min(near, totalNear - halo, centre - reach - halo) - LEADER_INSET;
        return { side, base, used: 0 };
      });
      for (const segment of waiting) {
        const piece = spanned(segment.from, segment.to, centre);
        const placement: LabelPlacement = horizontal
          ? { x: next, y: centre, anchor: out ? "start" : "end", baseline: "central", rotate: false }
          : { x: centre, y: next, anchor: "middle", baseline: out ? "auto" : "hanging", rotate: false };
        const ink = labelInk(placement, segment.text, size);
        const painted = haloed(ink);
        const inside = horizontal
          ? painted.top >= EDGE_PAD && painted.bottom <= plotBottom && (out || painted.left >= plotLeft)
          : painted.left >= plotLeft && painted.right <= rightmost && (out || painted.bottom <= plotBottom);
        if (
          !inside ||
          stackBars.some((rect) => overlap(painted, rect)) ||
          taken.some((rect) => overlap(painted, rect)) ||
          legs.some((leg) => overlap(painted, leg.rect))
        ) {
          continue;
        }
        const routed = tracks
          .map((track) => {
            const lane = track.base + track.side * track.used * LEADER_STEP;
            let leader: [number, number][];
            if (horizontal) {
              const from = (piece.left + piece.right) / 2;
              const rise = (ink.left + ink.right) / 2;
              const [edge, entry] = track.side === 1 ? [piece.bottom, painted.bottom] : [piece.top, painted.top];
              leader = [
                [from, edge],
                [from, lane],
                [rise, lane],
                [rise, entry],
              ];
            } else {
              const from = (piece.top + piece.bottom) / 2;
              const middle = (ink.top + ink.bottom) / 2;
              const [edge, entry] = track.side === 1 ? [piece.right, painted.right] : [piece.left, painted.left];
              leader = [
                [edge, from],
                [lane, from],
                [lane, middle],
              ];
              if ((entry - lane) * track.side < 0) leader.push([entry, middle]);
            }
            return { track, lane, leader, strokes: leaderLegs(leader) };
          })
          .find(({ lane, strokes }) => {
            const laneInside = horizontal
              ? lane - LEADER_WIDTH / 2 >= EDGE_PAD && lane + LEADER_WIDTH / 2 <= plotBottom
              : lane - LEADER_WIDTH / 2 >= plotLeft && lane + LEADER_WIDTH / 2 <= rightmost;
            return (
              laneInside &&
              strokes.every(
                (stroke) =>
                  !stackBars.some((rect) => overlap(stroke, rect)) &&
                  !taken.some((rect) => overlap(stroke, rect)) &&
                  !legs.some((leg) => leg.run !== index && overlap(stroke, leg.rect)),
              )
            );
          });
        if (!routed) continue;
        // How far it runs past the canvas, past the bars (where the plot can give room up).
        const past = !out ? 0 : horizontal ? ink.left + labelRun(segment.text, size) - (width - EDGE_PAD) : EDGE_PAD - ink.top;
        if (past > 0) {
          if (ratio(end) > 0) deficit = Math.max(deficit, past / ratio(end));
          continue;
        }
        annotations.set(segmentKey(segment.series, run.category), { placement, leader: routed.leader });
        taken.push(painted);
        for (const stroke of routed.strokes) legs.push({ run: index, rect: stroke });
        routed.track.used += 1;
        if (horizontal) next = out ? ink.right + 2 * halo + LABEL_CLEARANCE : ink.left - 2 * halo - LABEL_CLEARANCE;
        else next = out ? ink.top - 2 * halo - LABEL_CLEARANCE : ink.bottom + 2 * halo + LABEL_CLEARANCE;
      }
    });
    return { annotations, deficit };
  };
  // The segments' values on the plot `claimed` leaves: inside where they
  // fit, past the stack in the largest size - never larger than the values
  // inside the segments, the totals or the scale's `segmentFloor` - that
  // writes the most of the rest. A horizontal stack fitted by `stagger`
  // writes them past the stack at the size asked, every value of the
  // chart at one size; one fitted `upright` writes none there.
  const segmentsOn = (claimed: number) => {
    const { inner, segments } = insideOn(valueLength - claimed);
    let annotation: LabelStyle | null = null;
    let annotations: ReadonlyMap<string, SegmentAnnotation> = new Map();
    let deficit = 0;
    if (segments.size < insideCandidates.length && segmentFit !== "upright") {
      const cap = segmentFit === "stagger" ? fitStyle.size : Math.min(inner?.size ?? largest, largest, scale.segmentFloor);
      let best: ReturnType<typeof annotate> | null = null;
      for (const size of segmentFit === "stagger" ? [fitStyle.size] : scale.values) {
        if (size > cap) continue;
        const trial = annotate(size, claimed, segments);
        if (best && trial.annotations.size <= best.annotations.size) continue;
        best = trial;
        annotation = { size, rotate: false };
      }
      if (best) {
        annotations = best.annotations;
        deficit = best.deficit;
        if (annotations.size === 0) annotation = null;
      }
    }
    return { inner, segments, annotation, annotations, deficit, written: segments.size + annotations.size };
  };
  // A value past a stack that runs past the canvas has the plot give up the
  // room it lacks, as the values on points do: while the plot keeps its
  // `minPlot` and the totals still clear each other (`barsStillClear`, the
  // fitted ones never fewer), never for fewer values.
  let segmentPlan = segmentsOn(high);
  for (let step = 0, given = high, attempt = segmentPlan; attempt.deficit > 0 && step < MAX_ROOM_STEPS; step += 1) {
    given += Math.ceil(attempt.deficit);
    if (valueLength - given < minPlot || !barsStillClear(given)) break;
    attempt = segmentsOn(given);
    if (attempt.written > segmentPlan.written) {
      high = given;
      segmentPlan = attempt;
    }
  }
  // Fitted totals are written on the plot the room left them.
  if (fitPlan) fitPlan = fitPlanAt(high);
  const { inner, segments, annotation, annotations } = segmentPlan;
  const plotLength = valueLength - high;

  // What still has no room is counted, with what more room would do:
  // the thinnest such segment, its length and what its value needs inside
  // it, and whether the same chart at the tallest canvas a slide gives it,
  // or across a full row, writes every value - its own plan at that size.
  let thin: HiddenSegments | null = null;
  if (segmentPlan.written < insideCandidates.length && reason === null) {
    reason = "segments";
    const waiting = insideCandidates.filter((entry) => {
      const key = segmentKey(entry.series, entry.category);
      return !segments.has(key) && !annotations.has(key);
    });
    const thinnest = waiting.reduce((a, b) => (Math.abs(b.value) < Math.abs(a.value) ? b : a));
    const share = Math.abs(thinnest.value) / span;
    const insideSize = segmentFit !== "auto" ? fitStyle.size : (inner?.size ?? readable[readable.length - 1] ?? smallest);
    room = Math.floor(share * plotLength);
    need = Math.ceil(horizontal ? labelWidth(thinnest.text, insideSize) + 2 * SEGMENT_PAD_X : insideSize + 2 * SEGMENT_PAD_Y);
    const probe = (w: number, h: number): AxisChartLayout | null =>
      probing ? null : planAxisChart(block, w, h, hidden, locale, steps, true);
    const taller = height < TALLEST_CHART_CANVAS ? probe(width, TALLEST_CHART_CANVAS) : null;
    const wider = width < FULL_ROW_CANVAS ? probe(FULL_ROW_CANVAS, height) : null;
    const writesAll = (layout: AxisChartLayout | null): boolean => layout !== null && layout.values.written === layout.values.asked;
    const roomiest = horizontal ? wider : taller;
    const reachOn = roomiest
      ? horizontal
        ? roomiest.plot.right - roomiest.plot.left
        : roomiest.plot.bottom - roomiest.plot.top
      : plotLength;
    thin = {
      share,
      reach: Math.floor(share * reachOn),
      taller: writesAll(taller),
      wider: writesAll(wider),
      series: [...new Set(waiting.map((entry) => entry.series))].sort((a, b) => a - b),
    };
  }

  // A line's or an area's values are marks, placed or not; a combo line's are
  // counted apart; fitted values past the bars as the plan wrote them.
  let lifts: Map<string, number> | null = null;
  if (fitPlan) {
    lifts = new Map();
    for (const [index, painted] of fitPlan.painted.entries()) {
      const key = marks[index].pastKey;
      const lift = fitPlan.wanted[index];
      if (painted && key !== undefined && lift !== null) lifts.set(key, lift);
    }
  }
  const pointsWritten = pointsOnly ? 0 : placed.size;
  const outerWritten = lifts ? lifts.size : outer ? marks.length - unplaced : 0;
  const written = outerWritten + segments.size + annotations.size + pointsWritten;
  const askedCount = marks.length + insideCandidates.length + points.length;
  const margin = {
    top: PLOT_MARGIN_TOP + (horizontal ? 0 : high),
    right: marginRight + (horizontal ? high : 0),
    bottom: PLOT_MARGIN_BOTTOM,
    left: PLOT_MARGIN_LEFT,
  };
  const plot: PlotBox = {
    left: margin.left + readingAxisWidth,
    top: margin.top,
    right: width - margin.right - secondaryAxisWidth,
    bottom: height - margin.bottom - CATEGORY_AXIS_HEIGHT,
  };
  // The bars the fitted values were placed around: those that gave
  // way, else a group of upright bars as Recharts spaces it (`barSlot`).
  const innerGap = slot.pitch - slot.size;
  const barSpacing: AxisChartLayout["barSpacing"] =
    gaveWay ??
    (fit !== "auto" && groupedUpright
      ? { width: slot.size, gap: innerGap, categoryGap: band - lanes * slot.size - (lanes - 1) * innerGap }
      : null);

  return {
    horizontal,
    stacked,
    pointsOnly,
    tickSize,
    labelOffset,
    tickCount,
    axis,
    readingAxisWidth,
    ...(secondaryAxis ? { secondaryAxis } : {}),
    secondaryAxisWidth,
    pointPadding,
    margin,
    plot,
    categoryAxisInterval: categoriesFit ? 0 : "preserveStartEnd",
    barCategoryGap: `${Math.round(gapShare * 100)}%`,
    maxBarSize,
    barSize: slot.size,
    gaveWay,
    barSpacing,
    values: {
      outer,
      fit,
      lifts,
      alternate,
      inner,
      segments,
      annotation,
      annotations,
      thin,
      points: pointStyle,
      placed,
      asked: askedCount,
      written,
      reason: written < askedCount ? reason : null,
      room,
      need,
      turned,
    },
  };
}

/** The box of a bar (or the point of a line: no size) as Recharts hands it to a label, in canvas px. */
export interface LabelBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Where one value is drawn: the anchor point, how the text hangs on it, and whether it reads along the bar. */
export interface LabelPlacement {
  x: number;
  y: number;
  anchor: "start" | "middle" | "end";
  baseline: "auto" | "central" | "hanging";
  rotate: boolean;
}

/**
 * Where a value written past the end of its bar (or above its point) sits.
 * Recharts hands a negative bar a negative height (or width); its end is the
 * far side from the baseline. A value past a negative end runs toward the
 * category axis, and is drawn just inside the bar when the plot has no room
 * for it there. `offset` is the chart's `labelOffset`: just past a bar, clear
 * of a point's dot.
 */
export function outerLabelPlacement(
  box: LabelBox,
  toward: 1 | -1,
  style: LabelStyle,
  text: string,
  horizontal: boolean,
  plot: PlotBox,
  offset: number = LABEL_OFFSET,
): LabelPlacement {
  const length = labelWidth(text, style.size);
  if (horizontal) {
    const left = Math.min(box.x, box.x + box.width);
    const right = Math.max(box.x, box.x + box.width);
    const y = box.y + box.height / 2;
    if (toward === 1) return { x: right + offset, y, anchor: "start", baseline: "central", rotate: false };
    const outside = left - offset - length >= plot.left;
    return outside
      ? { x: left - offset, y, anchor: "end", baseline: "central", rotate: false }
      : { x: left + offset, y, anchor: "start", baseline: "central", rotate: false };
  }
  const x = box.x + box.width / 2;
  const top = Math.min(box.y, box.y + box.height);
  const bottom = Math.max(box.y, box.y + box.height);
  if (toward === 1) {
    return style.rotate
      ? { x, y: top - offset, anchor: "start", baseline: "central", rotate: true }
      : { x, y: top - offset, anchor: "middle", baseline: "auto", rotate: false };
  }
  const extent = style.rotate ? length : style.size;
  const outside = bottom + offset + extent <= plot.bottom;
  if (style.rotate) {
    return outside
      ? { x, y: bottom + offset, anchor: "end", baseline: "central", rotate: true }
      : { x, y: bottom - offset, anchor: "start", baseline: "central", rotate: true };
  }
  return outside
    ? { x, y: bottom + offset, anchor: "middle", baseline: "hanging", rotate: false }
    : { x, y: bottom - offset, anchor: "middle", baseline: "auto", rotate: false };
}

/** Where a value on a point sits: under its point, or over it raised and slid as planned (`ValuePlan.placed`). */
export function pointLabelPlacement(box: LabelBox, { under, rise, shift }: PointPlacement): LabelPlacement {
  const x = box.x + box.width / 2 + shift;
  return under
    ? { x, y: box.y + POINT_LABEL_OFFSET, anchor: "middle", baseline: "hanging", rotate: false }
    : { x, y: box.y - POINT_LABEL_OFFSET - rise, anchor: "middle", baseline: "auto", rotate: false };
}

/** Where a value written inside its segment sits: the segment's centre. */
export function segmentLabelPlacement(box: LabelBox): LabelPlacement {
  return { x: box.x + box.width / 2, y: box.y + box.height / 2, anchor: "middle", baseline: "central", rotate: false };
}
