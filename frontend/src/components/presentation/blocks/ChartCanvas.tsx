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
 * The Recharts drawing of the native `chart` block. Loaded lazily by
 * `chart.tsx`, which owns the measured box, the legend and the hidden-series
 * state; this file turns one block into SVG at one explicit size.
 *
 * Every colour is a deck token (`var(--deck-chart-N)`, `var(--deck-<tone>)`)
 * so the figure follows the template and the theme live, and the export's
 * computed styles resolve them. Every glyph is a `<text>` the layout audit
 * reads. Nothing here is a class name taken from the spec. The room of an
 * axis chart - its axes, its plot, how its values are written - is decided
 * in `chart-layout.ts`; this file draws what that decided.
 */
import { Fragment, useCallback, useEffect, useMemo, type CSSProperties, type ReactNode, type SVGProps } from "react";
import {
  Area,
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  LabelList,
  Line,
  Pie,
  PieChart,
  ReferenceLine,
  Tooltip,
  XAxis,
  YAxis,
  useXAxisScale,
  useYAxisScale,
  type LabelProps,
  type PieLabelRenderProps,
  type PieSectorShapeProps,
  type TooltipContentProps,
} from "recharts";

import type { DeckChartBlock, DeckSeriesKind } from "@/types";

import { butterflyLayout, type ButterflyLayout } from "./chart-butterfly";
import {
  axisInk,
  chartValueWriter,
  gaugeReading,
  hasSecondaryTargetLine,
  paletteColor,
  seriesAxis,
  seriesColors,
  seriesValuesAsked,
  waterfallColor,
  waterfallLabel,
  waterfallSteps,
  writtenValues,
  type ValueAxisScale,
  type WaterfallStep,
} from "./chart-format";
import {
  axisChartLayout,
  axisTick,
  BAR_GAP,
  drawnKind,
  LABEL_OFFSET,
  LEADER_WIDTH,
  LINE_WIDTH,
  outerLabelPlacement,
  pastBarKey,
  pointLabelPlacement,
  pointPlacementName,
  segmentKey,
  seriesLabels,
  segmentLabelPlacement,
  totalKey,
  type AxisChartLayout,
  type LabelBox,
  type LabelPlacement,
  type LabelStyle,
  type SegmentAnnotation,
  type ValuePlan,
} from "./chart-layout";
import { gaugeArcPath, gaugeLayout } from "./chart-gauge";
import { createSettleClock, growsIn, type GrowthHandlers } from "./chart-settle";
import {
  END_ANGLE,
  LEADER_STROKE,
  sliceChartLayout,
  START_ANGLE,
  type PieSlice,
  type SliceChartLayout,
  type SliceValue,
} from "./chart-slices";

interface ChartCanvasProps {
  block: DeckChartBlock;
  width: number;
  height: number;
  /** Series (axis charts) or slices (pie / donut) the legend switched off. */
  hidden: ReadonlySet<number>;
  /** False on the static export page and under `prefers-reduced-motion`. */
  animate: boolean;
  locale: string;
  /**
   * Called once the figure is complete - painted, and grown in when it
   * animates - so the host can drop `data-deck-pending`.
   */
  onSettled: () => void;
  /** A waterfall's bar for a total its steps miss (`allow_gap`), in the app's language. */
  gapLabel: string;
  /** A waterfall tooltip's line for the level a step brings the bridge to, in the app's language. */
  runningLabel: string;
}

const TICK_FILL = "var(--deck-muted)";
/**
 * Each axis' tick labels carry a class naming the axis: Recharts draws them
 * in a layer apart from the axis itself, and the PPTX extractor reads each
 * value axis' ink off its own labels. Their size is the chart's
 * (`layout.tickSize`, `label_size`), which the stylesheet sets too.
 */
const CATEGORY_TICK = { className: "deck-chart-tick-category" } as const;
const PRIMARY_TICK = { className: "deck-chart-tick-primary" } as const;
const SECONDARY_TICK = { className: "deck-chart-tick-secondary" } as const;
const GRID_STROKE = "var(--deck-line)";
const TARGET_STROKE = "var(--deck-warning)";
const CONNECTOR_STROKE = "var(--deck-muted)";
/** A waterfall bar's corners: it floats, so all four are rounded alike. */
const WATERFALL_RADIUS = 6;
/** A change too small for the axis still shows as a bar (a 0.06M gap on a 20M axis is one pixel). */
const WATERFALL_MIN_BAR_PX = 3;
const ANIMATION_MS = 700;

/**
 * The target line is drawn by hand so it owns a class and the axis it runs
 * along: the stylesheet's enter animation draws a line across the plot along
 * its own axis (`data-axis="y"` is a horizontal line at a y value, the
 * common case; `"x"` is the vertical line of a horizontal bar chart).
 */
const horizontalTargetLine = (props: SVGProps<SVGLineElement>) => (
  <line {...props} className="recharts-reference-line-line deck-chart-target" data-axis="y" />
);
const verticalTargetLine = (props: SVGProps<SVGLineElement>) => (
  <line {...props} className="recharts-reference-line-line deck-chart-target" data-axis="x" />
);
/** The right-hand axis' target: dotted, so it never reads as the left-hand one's. */
const secondaryTargetLine = (props: SVGProps<SVGLineElement>) => (
  <line
    {...props}
    className="recharts-reference-line-line deck-chart-target"
    data-axis="y"
    data-value-axis="secondary"
  />
);
const STACK_ID = "stack";
/**
 * The Recharts id of the right-hand value axis of a chart that plots two
 * units. The left-hand axis keeps Recharts' default id, the one the
 * grid and every series read unless told otherwise.
 */
const SECONDARY_AXIS_ID = "secondary";

/**
 * What the drawing PUBLISHES once it has a size: the facts a
 * figure cannot know from its spec alone. Hidden, empty. The PPTX extractor
 * reads the axis it settled on and how its values are set (`data-values`:
 * each bar's, point's or stack's own value is written; the size, and whether
 * it is turned on its side; the size of the values inside segments) next to
 * the `data-deck-chart` payload of the figure. The layout audit reads how
 * many values the block asked for, how many are written, and why the rest
 * are not - and reports "values hidden on chart N" for any it misses - and,
 * for values turned on their side, the room they lacked upright
 * (`data-upright-*`, "values rotated on chart N"). A two-unit chart
 * publishes its right-hand axis too. A line or an area whose values
 * alternate over and under their points says so (`data-values-alternate`):
 * the PPTX writer then labels every such series point by point. A
 * chart whose values past the bars are fitted (`value_label_fit`)
 * says how (`data-values-fit`) and, when its bars gave way to them or stand
 * as a group of upright bars, how they stand (`data-bar-width`,
 * `-gap`, `data-category-gap`, canvas px): the PPTX
 * writer spaces PowerPoint's bars alike and labels them point by point. A
 * stack whose thin segments write their value past it publishes how
 * many and their size (`data-values-annotated`, `data-annotation-size`): the
 * values themselves are on the figure with their leaders, where the PPTX
 * extractor measures them. Segments still hidden say what more room would do
 * (`data-values-share`, `-reach`, `-taller`, `-wider`, `-series`), so the
 * layout check recommends only room that writes them. A pie or a donut
 * says where its values are written (`data-values-placement`:
 * `outside` with a leader, or `inside` the slices - the PPTX writer places
 * PowerPoint's labels alike), the share of the smallest slice left without
 * its value (`data-values-share`), and, for values set under the size asked,
 * that size, the radius the disc would keep with them outside at it and the
 * least it keeps (`data-shrunk-size`, `-room`, `-need`, "values shrunk on
 * chart N").
 */
function ChartFacts({
  axis,
  secondaryAxis,
  values,
  barSpacing = null,
  pie = null,
}: {
  axis?: ValueAxisScale;
  secondaryAxis?: ValueAxisScale;
  values: ValuePlan;
  barSpacing?: AxisChartLayout["barSpacing"];
  pie?: Pick<SliceChartLayout, "placement" | "hiddenShare" | "shrunk"> | null;
}) {
  const { turned, thin } = values;
  const shrunk = pie?.shrunk;
  return (
    <span
      className="deck-chart-facts"
      hidden
      data-values={values.outer ? "true" : "false"}
      data-values-asked={values.asked}
      data-values-written={values.written}
      {...(values.reason
        ? { "data-values-reason": values.reason, "data-values-room": values.room, "data-values-need": values.need }
        : {})}
      {...(values.outer ? { "data-label-size": values.outer.size } : {})}
      {...(values.outer?.rotate ? { "data-label-rotate": "true" } : {})}
      {...(values.alternate ? { "data-values-alternate": "true" } : {})}
      {...(values.fit !== "auto" ? { "data-values-fit": values.fit } : {})}
      {...(barSpacing
        ? {
            "data-bar-width": round2(barSpacing.width),
            "data-bar-gap": round2(barSpacing.gap),
            "data-category-gap": round2(barSpacing.categoryGap),
          }
        : {})}
      {...(turned
        ? { "data-upright-size": turned.size, "data-upright-room": turned.room, "data-upright-need": turned.need }
        : {})}
      {...(values.inner ? { "data-segment-size": values.inner.size } : {})}
      {...(values.annotation
        ? { "data-annotation-size": values.annotation.size, "data-values-annotated": values.annotations.size }
        : {})}
      {...(thin
        ? {
            "data-values-share": Math.round(thin.share * 10_000) / 10_000,
            "data-values-reach": thin.reach,
            "data-values-taller": thin.taller ? "true" : "false",
            "data-values-wider": thin.wider ? "true" : "false",
            "data-values-series": thin.series.join(","),
          }
        : {})}
      {...(pie?.placement ? { "data-values-placement": pie.placement } : {})}
      {...(pie?.hiddenShare != null ? { "data-values-share": Math.round(pie.hiddenShare * 10_000) / 10_000 } : {})}
      {...(shrunk ? { "data-shrunk-size": shrunk.size, "data-shrunk-room": shrunk.room, "data-shrunk-need": shrunk.need } : {})}
      {...(values.points ? { "data-point-size": values.points.size } : {})}
      {...(axis ? { "data-min": axis.min, "data-max": axis.max, "data-step": axis.step } : {})}
      {...(secondaryAxis
        ? {
            "data-secondary-min": secondaryAxis.min,
            "data-secondary-max": secondaryAxis.max,
            "data-secondary-step": secondaryAxis.step,
          }
        : {})}
    />
  );
}

/** A canvas length as the drawing publishes it: two decimals. */
const round2 = (value: number): number => Math.round(value * 100) / 100;

/**
 * Which value of its bar a label writes: the bar's own, its stack's total, its
 * segment's inside it, or a thin segment's past its stack.
 */
type LabelRole = "value" | "total" | "segment" | "annotation";

/**
 * One value on the figure. The size is an inline style - the stylesheet sets
 * every glyph of the canvas at the tick size, and a presentation attribute
 * would lose to it. The role, series and category are what the PPTX writer
 * reads to put PowerPoint's own data labels where the viewer wrote these;
 * `data-placement` says a value on a point is written under it (`below`),
 * raised over what is in its way (`lifted`) or slid clear of a value axis'
 * ticks (`shifted`, `pointPlacementName`). `data-lift` says how far past its
 * place a fitted value past a bar sits (`ValuePlan.lifts`).
 */
function ValueText({
  placement,
  style,
  text,
  role,
  series,
  category,
  segment,
  ink,
  at,
  lift,
}: {
  placement: LabelPlacement;
  style: LabelStyle;
  text: string;
  role: LabelRole;
  series: number;
  category: number;
  /** The colour of the segment a value is written inside, which its ink is read from. */
  segment?: string;
  /** The colour a value past its stack is written in: its series'. */
  ink?: string;
  at?: "below" | "lifted" | "shifted";
  /** Canvas px past its place; unset or 0 where it sits in it. */
  lift?: number;
}) {
  const { x, y, anchor, baseline, rotate } = placement;
  const css = {
    fontSize: style.size,
    ...(segment ? { "--deck-segment": segment } : {}),
    ...(ink ? { "--deck-annotation": ink } : {}),
  } as CSSProperties;
  return (
    <text
      x={x}
      y={y}
      textAnchor={anchor}
      dominantBaseline={baseline}
      transform={rotate ? `rotate(-90 ${x} ${y})` : undefined}
      className="deck-chart-value"
      style={css}
      data-role={role}
      data-series={series}
      data-category={category}
      data-rotate={rotate ? "true" : undefined}
      data-placement={at}
      data-lift={lift ? round2(lift) : undefined}
    >
      {text}
    </text>
  );
}

/**
 * The leader from a thin segment to its value past the stack, one
 * line per leg in its series' colour - the PPTX extractor measures each leg
 * as it measures a waterfall's connectors. Square caps close the corners.
 */
function Leader({ annotation, color, series, category }: { annotation: SegmentAnnotation; color: string; series: number; category: number }) {
  const { leader } = annotation;
  return (
    <>
      {leader.slice(1).map(([x2, y2], index) => {
        const [x1, y1] = leader[index];
        return (
          <line
            key={index}
            className="deck-chart-leader"
            x1={x1}
            y1={y1}
            x2={x2}
            y2={y2}
            stroke={color}
            strokeWidth={LEADER_WIDTH}
            strokeLinecap="square"
            data-series={series}
            data-category={category}
          />
        );
      })}
    </>
  );
}

/**
 * The box `LabelList` hands the label content of a series: its bar's (a
 * point's, sizeless); `null` for a polar box, which no axis chart hands it.
 */
function labelBox(viewBox: LabelProps["viewBox"]): LabelBox | null {
  if (!viewBox || !("width" in viewBox)) return null;
  return { x: viewBox.x ?? 0, y: viewBox.y ?? 0, width: viewBox.width ?? 0, height: viewBox.height ?? 0 };
}

/** Data key of the n-th series in a row (names may repeat; positions do not). */
const seriesKey = (index: number): string => `s${index}`;
/** The series a data key names (`seriesKey` read back). */
const seriesIndexOf = (dataKey: unknown): number =>
  typeof dataKey === "string" && /^s\d+$/.test(dataKey) ? Number(dataKey.slice(1)) : 0;
/**
 * The visible totals of a category, one per direction: positive values stack
 * up from the baseline and negative ones down, so a category holding both is
 * two stacks with two totals - a single net figure would misstate either.
 */
const STACK_UP_KEY = "__up";
const STACK_DOWN_KEY = "__down";
const CATEGORY_KEY = "category";
/**
 * The category's position, carried by its row: Recharts hands a label the
 * position of its bar among the bars it DREW, and it draws none for a zero.
 */
const INDEX_KEY = "__index";

/** A row's values by key; a waterfall bar's is the span `[low, high]` it covers. */
type Row = Record<string, string | number | [number, number]>;

/** One row per category: the series values by position, and the visible totals. */
function buildRows(block: DeckChartBlock, hidden: ReadonlySet<number>): Row[] {
  return block.categories.map((category, index) => {
    const row: Row = { [CATEGORY_KEY]: category, [INDEX_KEY]: index };
    let up = 0;
    let down = 0;
    block.series.forEach((entry, seriesIndex) => {
      const value = entry.values[index] ?? 0;
      row[seriesKey(seriesIndex)] = value;
      if (hidden.has(seriesIndex)) return;
      if (value < 0) down += value;
      else up += value;
    });
    row[STACK_UP_KEY] = up;
    row[STACK_DOWN_KEY] = down;
    return row;
  });
}

/**
 * One row per waterfall bar. The category is the bar's POSITION, written as
 * its label by the axis: two bars may share a name (two gap bars), and a band
 * scale keyed on names would stack them in one band.
 */
function buildWaterfallRows(steps: readonly WaterfallStep[]): Row[] {
  return steps.map((step, index) => ({
    [CATEGORY_KEY]: String(index),
    [INDEX_KEY]: index,
    [seriesKey(0)]: step.start <= step.end ? [step.start, step.end] : [step.end, step.start],
  }));
}

/** The waterfall bar a Recharts entry (a tooltip row) stands for. */
function stepOf(steps: readonly WaterfallStep[], payload: Row | undefined): WaterfallStep | undefined {
  return payload ? steps[Number(payload[INDEX_KEY])] : undefined;
}

/** What a drawing receives: the canvas props and the growth handlers of each series. */
interface DrawingProps extends ChartCanvasProps {
  /** The handlers of the n-th series (the one animated pie of a slice chart or a gauge is series 0). */
  growth: (series: number) => GrowthHandlers;
}

export default function ChartCanvas(props: ChartCanvasProps) {
  const { block, hidden, animate, onSettled } = props;
  const clock = useMemo(() => createSettleClock(onSettled), [onSettled]);
  const grows = animate && growsIn(block, hidden);
  useEffect(() => {
    clock.start(grows);
    return () => clock.stop();
  }, [clock, grows]);

  const drawing: DrawingProps = { ...props, growth: clock.growth };
  switch (block.kind) {
    case "pie":
    case "donut":
      return <SliceChart {...drawing} />;
    case "gauge":
      return <GaugeChart {...drawing} />;
    case "butterfly":
      return <ButterflyChart {...drawing} />;
    default:
      return <AxisChart {...drawing} />;
  }
}

/* ── Axis charts: bar, stacked_bar, line, area, combo, waterfall ──────────── */

function AxisChart({ block, width, height, hidden, animate, locale, gapLabel, runningLabel, growth }: DrawingProps) {
  // A waterfall draws the bars of its bridge, gap bars included; its
  // axis reads their labels, its values their signed changes.
  const steps = useMemo(() => (block.kind === "waterfall" ? waterfallSteps(block, gapLabel) : null), [block, gapLabel]);
  const rows = useMemo(() => (steps ? buildWaterfallRows(steps) : buildRows(block, hidden)), [steps, block, hidden]);
  const colors = useMemo(() => seriesColors(block.series), [block.series]);
  // The room of the figure - axis widths, plot margins, how each value is
  // written - is decided in `chart-layout.ts`. The value axis is decided
  // there too, not by Recharts (see `valueAxisScale`): round ticks from the
  // floor (zero, unless a value is negative) to a rounded top; a percent
  // chart that stays under 100 keeps 100 as its top so a share reads against
  // the whole. The target is part of the ceiling, so the line always falls
  // inside the domain.
  const layout = useMemo(
    () => axisChartLayout(block, width, height, hidden, locale, steps),
    [block, width, height, hidden, locale, steps],
  );
  const { horizontal, stacked, axis, secondaryAxis, values, plot, labelOffset } = layout;
  const tickText = { fontSize: layout.tickSize, fill: TICK_FILL };
  const target = block.target ?? undefined;
  // A chart that plots two units reads each against a value axis of
  // its own: the right-hand one takes the series on `axis: "secondary"`, its
  // format, its target and its ink, and both axes write their unit on their
  // ticks so each says what it reads (`axisTick`, `seriesLabels`).
  const secondaryTarget = hasSecondaryTargetLine(block) ? (block.secondary_target as number) : undefined;
  const labels = useMemo(() => seriesLabels(block, locale), [block, locale]);
  const label = (value: number, series = 0): string => (labels[series] ?? labels[0])(value);
  // The waterfall's tooltip keeps every digit; its bars are labelled compact with their axis.
  const tooltipWrite = useMemo(() => chartValueWriter(block, writtenValues(block), locale), [block, locale]);
  const primaryTick = useMemo(() => axisTick(block, "primary", locale, axis.ticks), [block, locale, axis]);
  const secondaryTick = useMemo(
    () => axisTick(block, "secondary", locale, secondaryAxis?.ticks ?? []),
    [block, locale, secondaryAxis],
  );
  const tick = (value: unknown): string => primaryTick(Number(value));

  // Bars first so a combo's lines paint over them.
  const order = block.series
    .map((entry, index) => ({ entry, index, drawn: drawnKind(block, entry.kind) }))
    .sort((a, b) => Number(a.drawn === "line") - Number(b.drawn === "line"));

  // Bars sit in bands; a line's first and last points would sit ON the axis
  // edges without their own room.
  const categoryAxisPadding = layout.pointsOnly ? { left: layout.pointPadding, right: layout.pointPadding } : undefined;
  const valueAxisDomain: [number, number] = [axis.min, axis.max];

  // A stack writes its visible total past its end, on the outermost visible
  // series that has a value THERE: Recharts draws no rectangle for a zero, so
  // a total hung on a series that is zero in one category would go missing
  // from that category alone. Positive and negative values stack away from the
  // baseline separately, so a category holding both carries two totals - the
  // positive one past the top of its stack, the negative one past the bottom -
  // never a net figure that misstates either. Each segment the layout found
  // room in writes its own value inside it. Every other figure writes
  // each of its values; a combo's line writes each over its point, or under
  // it where the layout found a bar's value over it, and none where both
  // rooms are taken.
  const stackEnd = (row: Row, sign: 1 | -1): number | undefined =>
    [...order].reverse().find(({ index }) => !hidden.has(index) && Math.sign(Number(row[seriesKey(index)])) === sign)?.index;
  const categoryOf = (entry: { payload: Row }) => Number(entry.payload[INDEX_KEY]);
  // A waterfall bar writes its change (a total, its level) over its top, as
  // the layout planned it (`axisChartLayout`); a step of zero has no bar to
  // hang it on and is written by `WaterfallLevels` from the axis scales.
  const waterfallValue = (position: number, box: LabelBox): ReactNode => {
    const step = steps?.[position];
    if (!values.outer || !step) return null;
    const text = waterfallLabel(step, labels[0]);
    return (
      <ValueText
        key={`value-${position}`}
        placement={outerLabelPlacement(box, 1, values.outer, text, horizontal, plot, labelOffset)}
        style={values.outer}
        text={text}
        role="value"
        series={0}
        category={position}
      />
    );
  };
  const pointLabels = (index: number) => {
    const style = values.points;
    if (!style) return null;
    const PointValue = ({ viewBox, value }: LabelProps) => {
      const category = Number(value);
      const row = rows[category];
      const box = labelBox(viewBox);
      const place = values.placed.get(segmentKey(index, category));
      if (!row || !box || !place) return null;
      return (
        <ValueText
          placement={pointLabelPlacement(box, place)}
          style={style}
          text={label(Number(row[seriesKey(index)]), index)}
          role="value"
          series={index}
          category={category}
          at={pointPlacementName(place)}
        />
      );
    };
    return <LabelList valueAccessor={categoryOf} content={PointValue} />;
  };
  const valueLabels = (index: number, drawn: DeckSeriesKind) => {
    if (block.kind === "combo" && drawn === "line") return pointLabels(index);
    if (!values.outer && !values.inner && !values.annotation) return null;
    // A series whose own `show_values` is off writes none (a stack and a bridge
    // follow the chart, and the stack's layout leaves out such a series' segments).
    if (!stacked && !steps && !seriesValuesAsked(block, index)) return null;
    const { outer, inner, segments, annotation, annotations, placed, lifts } = values;
    const SeriesValues = ({ viewBox, value }: LabelProps) => {
      const category = Number(value);
      const row = rows[category];
      const box = labelBox(viewBox);
      if (!row || !box) return null;
      const own = Number(row[seriesKey(index)]);
      const texts: ReactNode[] = [];
      // A value on a point (a line, an area) sits where the layout placed it,
      // and is not written where it found no room (`ValuePlan.placed`). A
      // fitted value past a bar sits its lift past its place, and is
      // not written where the plan left it out (`ValuePlan.lifts`).
      const past = (role: LabelRole, amount: number, toward: 1 | -1, onPoint?: string) => {
        if (!outer) return;
        const place = onPoint && layout.pointsOnly && toward === 1 ? placed.get(onPoint) : undefined;
        if (onPoint && layout.pointsOnly && toward === 1 && !place) return;
        const lift = lifts ? lifts.get(pastBarKey(role === "total" ? "total" : "value", toward, index, category)) : 0;
        if (lift === undefined) return;
        const text = label(amount, index);
        texts.push(
          <ValueText
            key={role + toward}
            placement={
              place
                ? pointLabelPlacement(box, place)
                : outerLabelPlacement(box, toward, outer, text, horizontal, plot, lift ? labelOffset + lift : labelOffset)
            }
            style={outer}
            text={text}
            role={role}
            series={index}
            category={category}
            at={place ? pointPlacementName(place) : undefined}
            lift={lift}
          />,
        );
      };
      if (steps) {
        texts.push(waterfallValue(category, box));
      } else if (stacked) {
        if (inner && segments.has(segmentKey(index, category))) {
          texts.push(
            <ValueText
              key="segment"
              placement={segmentLabelPlacement(box)}
              style={inner}
              text={label(own, index)}
              role="segment"
              series={index}
              category={category}
              segment={colors[index]}
            />,
          );
        }
        if (stackEnd(row, 1) === index) past("total", Number(row[STACK_UP_KEY]), 1, totalKey(category));
        if (stackEnd(row, -1) === index) past("total", Number(row[STACK_DOWN_KEY]), -1);
        // A thin segment's value past its stack, where the layout
        // planned it: its leader under its text, both in its series' colour.
        const outside = annotation ? annotations.get(segmentKey(index, category)) : undefined;
        if (annotation && outside) {
          texts.push(<Leader key="leader" annotation={outside} color={colors[index]} series={index} category={category} />);
          texts.push(
            <ValueText
              key="annotation"
              placement={outside.placement}
              style={annotation}
              text={label(own, index)}
              role="annotation"
              series={index}
              category={category}
              ink={colors[index]}
            />,
          );
        }
      } else {
        past("value", own, layout.pointsOnly || own >= 0 ? 1 : -1, segmentKey(index, category));
      }
      return texts.length > 0 ? <Fragment>{texts}</Fragment> : null;
    };
    return <LabelList valueAccessor={categoryOf} content={SeriesValues} />;
  };

  const barRadius: number | [number, number, number, number] = stacked ? 0 : horizontal ? [0, 8, 8, 0] : [8, 8, 0, 0];

  return (
    <>
      <ComposedChart
        width={width}
        height={height}
        data={rows}
        layout={horizontal ? "vertical" : "horizontal"}
        // The right margin keeps the last category label (centred on its
        // tick) inside the canvas; the top one (the right one on a horizontal
        // chart) also holds the values written past the longest bar.
        margin={layout.margin}
        barCategoryGap={layout.barCategoryGap}
        barGap={layout.gaveWay ? layout.gaveWay.gap : BAR_GAP}
      >
        <CartesianGrid strokeDasharray="4 8" stroke={GRID_STROKE} horizontal={!horizontal} vertical={horizontal} />
        {horizontal ? (
          <>
            <XAxis
              type="number"
              domain={valueAxisDomain}
              ticks={axis.ticks}
              tickFormatter={tick}
              tick={{ ...tickText, ...PRIMARY_TICK }}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              type="category"
              dataKey={CATEGORY_KEY}
              width={layout.readingAxisWidth}
              tick={{ ...tickText, ...CATEGORY_TICK }}
              axisLine={false}
              tickLine={false}
              interval={layout.categoryAxisInterval}
            />
          </>
        ) : (
          <>
            <XAxis
              dataKey={CATEGORY_KEY}
              tick={{ ...tickText, ...CATEGORY_TICK }}
              axisLine={{ stroke: GRID_STROKE }}
              tickLine={false}
              interval={layout.categoryAxisInterval}
              padding={categoryAxisPadding}
              tickFormatter={steps ? (position: string) => steps[Number(position)]?.category ?? "" : undefined}
            />
            <YAxis
              type="number"
              domain={valueAxisDomain}
              ticks={axis.ticks}
              width={layout.readingAxisWidth}
              tickFormatter={tick}
              tick={{ ...tickText, ...PRIMARY_TICK, ...(secondaryAxis ? { fill: axisInk(block, "primary") } : {}) }}
              axisLine={false}
              tickLine={false}
            />
            {secondaryAxis ? (
              <YAxis
                yAxisId={SECONDARY_AXIS_ID}
                orientation="right"
                type="number"
                domain={[secondaryAxis.min, secondaryAxis.max]}
                ticks={secondaryAxis.ticks}
                width={layout.secondaryAxisWidth}
                tickFormatter={(value: unknown) => secondaryTick(Number(value))}
                tick={{ ...tickText, ...SECONDARY_TICK, fill: axisInk(block, "secondary") }}
                axisLine={false}
                tickLine={false}
              />
            ) : null}
          </>
        )}
        <Tooltip
          content={(tooltip) =>
            steps ? (
              <WaterfallTooltip
                tooltip={tooltip}
                block={block}
                steps={steps}
                write={tooltipWrite}
                runningLabel={runningLabel}
              />
            ) : (
              <ChartTooltip tooltip={tooltip} format={(value, dataKey) => label(value, seriesIndexOf(dataKey))} />
            )
          }
          cursor={{ fill: GRID_STROKE, stroke: GRID_STROKE, fillOpacity: 0.35 }}
          isAnimationActive={false}
        />
        {order.map(({ entry, index, drawn }) => {
          const key = seriesKey(index);
          const color = colors[index];
          const shared = {
            dataKey: key,
            name: entry.name,
            hide: hidden.has(index),
            isAnimationActive: animate,
            animationDuration: ANIMATION_MS,
            ...(secondaryAxis && seriesAxis(entry) === "secondary" ? { yAxisId: SECONDARY_AXIS_ID } : {}),
            ...growth(index),
          };
          if (block.kind === "area") {
            return (
              <Area
                key={key}
                {...shared}
                type="monotone"
                stroke={color}
                strokeWidth={3}
                fill={color}
                fillOpacity={stacked ? 0.55 : 0.22}
                stackId={stacked ? STACK_ID : undefined}
                dot={false}
                activeDot={{ r: 8, stroke: "var(--deck-bg)", strokeWidth: 2 }}
              >
                {valueLabels(index, drawn)}
              </Area>
            );
          }
          if (drawn === "line") {
            return (
              <Line
                key={key}
                {...shared}
                type="monotone"
                stroke={color}
                strokeWidth={LINE_WIDTH}
                dot={{ r: 6, fill: color, stroke: "var(--deck-bg)", strokeWidth: 2 }}
                activeDot={{ r: 9, stroke: "var(--deck-bg)", strokeWidth: 2 }}
              >
                {valueLabels(index, drawn)}
              </Line>
            );
          }
          if (steps) {
            return (
              <Bar
                key={key}
                {...shared}
                radius={WATERFALL_RADIUS}
                maxBarSize={layout.maxBarSize}
                minPointSize={(_value, position) => (steps[position]?.value ? WATERFALL_MIN_BAR_PX : 0)}
              >
                {steps.map((step, position) => (
                  <Cell key={position} fill={waterfallColor(step.role, entry.tone)} />
                ))}
                {valueLabels(index, drawn)}
              </Bar>
            );
          }
          return (
            <Bar
              key={key}
              {...shared}
              fill={color}
              stackId={stacked ? STACK_ID : undefined}
              radius={barRadius}
              maxBarSize={layout.maxBarSize}
              {...(layout.gaveWay ? { barSize: layout.gaveWay.width } : {})}
            >
              {valueLabels(index, drawn)}
            </Bar>
          );
        })}
        {steps && !hidden.has(0) ? <WaterfallLevels steps={steps} barSize={layout.barSize} valueAt={waterfallValue} /> : null}
        {target !== undefined ? (
          // Named in the legend (`chart.tsx`), never on the plot: a label on the
          // line lands on whichever value sits nearest the target.
          <ReferenceLine
            {...(horizontal ? { x: target } : { y: target })}
            stroke={TARGET_STROKE}
            strokeDasharray="12 10"
            strokeWidth={3}
            shape={horizontal ? verticalTargetLine : horizontalTargetLine}
          />
        ) : null}
        {secondaryAxis && secondaryTarget !== undefined ? (
          <ReferenceLine
            yAxisId={SECONDARY_AXIS_ID}
            y={secondaryTarget}
            stroke={TARGET_STROKE}
            strokeDasharray="2 10"
            strokeLinecap="round"
            strokeWidth={4}
            shape={secondaryTargetLine}
          />
        ) : null}
      </ComposedChart>
      <ChartFacts axis={axis} secondaryAxis={secondaryAxis} values={values} barSpacing={layout.barSpacing} />
    </>
  );
}

/**
 * What a waterfall draws from its axis scales rather than from its bars. The
 * dashed lines that carry its level from one bar to the next - what makes the
 * bars read as one running sum: each runs in the GAP between two bars, at the
 * level the first one leaves the bridge at (along a bar it would lie on the
 * bar's edge). And the value of a step of zero, which Recharts draws no bar
 * for and so hands no label: written over its band at the level it leaves
 * unchanged. A bar is `barSize` wide (`barSlot`), centred in its band.
 */
function WaterfallLevels({
  steps,
  barSize,
  valueAt,
}: {
  steps: readonly WaterfallStep[];
  barSize: number;
  /** The value of the bar at `position`, written as the layout planned it over `box`. */
  valueAt: (position: number, box: LabelBox) => ReactNode;
}) {
  const x = useXAxisScale();
  const y = useYAxisScale();
  if (!x || !y) return null;
  const edges = (position: number) => {
    const middle = x(String(position), { position: "middle" });
    return middle === undefined ? null : { left: middle - barSize / 2, right: middle + barSize / 2 };
  };
  const zeros = steps.flatMap((step, position) => {
    const band = step.value === 0 ? edges(position) : null;
    const level = band ? y(step.end) : undefined;
    return band && level !== undefined ? [valueAt(position, { x: band.left, y: level, width: barSize, height: 0 })] : [];
  });
  return (
    <g>
      {zeros}
      {steps.slice(0, -1).map((step, position) => {
        const from = edges(position);
        const to = edges(position + 1);
        const level = y(step.end);
        if (!from || !to || level === undefined || to.left <= from.right) return null;
        return (
          <line
            key={position}
            className="deck-chart-connector"
            x1={from.right}
            x2={to.left}
            y1={level}
            y2={level}
            stroke={CONNECTOR_STROKE}
            strokeWidth={2}
            strokeDasharray="4 6"
          />
        );
      })}
    </g>
  );
}

/* ── Butterfly ─────────────────────────────────────────────────────── */

/** A butterfly bar's corners: rounded at its outer end only, on either side (Recharts mirrors a bar drawn leftwards). */
const BUTTERFLY_RADIUS: [number, number, number, number] = [0, 8, 8, 0];
/** The invisible spacers either side of zero that hold a centre spine's column. */
const SPINE_SPACER_LEFT = "__spineLeft";
const SPINE_SPACER_RIGHT = "__spineRight";

/**
 * One row per rung: its category's position (two rungs may share a name), the
 * first series stored negative - it runs leftwards from the spine - the
 * second positive, and the spine's spacers.
 */
function buildButterflyRows(block: DeckChartBlock, gap: number): Row[] {
  return block.categories.map((_, index) => ({
    [CATEGORY_KEY]: String(index),
    [INDEX_KEY]: index,
    [SPINE_SPACER_LEFT]: -gap,
    [SPINE_SPACER_RIGHT]: gap,
    [seriesKey(0)]: -Math.abs(block.series[0]?.values[index] ?? 0),
    [seriesKey(1)]: Math.abs(block.series[1]?.values[index] ?? 0),
  }));
}

/**
 * A butterfly: its two series mirrored about a central spine, one row per
 * rung. The room - the spine's column, the sides' scale, where each value is
 * written and at what size - is decided in `chart-butterfly.ts`; this draws
 * it as one horizontal bar chart whose two sides share each row (stacked
 * with Recharts' `sign` offset), every position written as the size it
 * stands for.
 */
function ButterflyChart({ block, width, height, hidden, animate, locale, growth }: DrawingProps) {
  const layout = useMemo(() => butterflyLayout(block, width, height, hidden, locale), [block, width, height, hidden, locale]);
  const rows = useMemo(() => buildButterflyRows(block, layout.gap), [block, layout.gap]);
  const colors = useMemo(() => seriesColors(block.series), [block.series]);
  // The hover keeps every digit, as every chart's does.
  const tooltipWrite = useMemo(() => chartValueWriter(block, writtenValues(block), locale), [block, locale]);
  const tickText = { fontSize: layout.tickSize, fill: TICK_FILL };
  const spacer = { stackId: STACK_ID, fill: "transparent", isAnimationActive: false, activeBar: false } as const;
  return (
    <>
      <ComposedChart
        width={width}
        height={height}
        data={rows}
        layout="vertical"
        stackOffset="sign"
        margin={layout.margin}
        barCategoryGap={layout.barCategoryGap}
      >
        <CartesianGrid strokeDasharray="4 8" stroke={GRID_STROKE} horizontal={false} vertical />
        <XAxis
          type="number"
          domain={[layout.domain[0], layout.domain[1]]}
          allowDataOverflow
          ticks={[...layout.ticks]}
          interval={0}
          tickFormatter={(value: unknown) => layout.tickLabels.get(Number(value)) ?? ""}
          tick={{ ...tickText, ...PRIMARY_TICK }}
          axisLine={false}
          tickLine={false}
        />
        {/* The rungs' band scale; their names are written by `ButterflyMarks`, on the spine or the left axis. */}
        <YAxis type="category" dataKey={CATEGORY_KEY} width={0} hide />
        <Tooltip
          content={(tooltip) => (
            <ButterflyTooltip tooltip={tooltip} block={block} colors={colors} hidden={hidden} write={tooltipWrite} />
          )}
          cursor={{ fill: GRID_STROKE, stroke: GRID_STROKE, fillOpacity: 0.35 }}
          isAnimationActive={false}
        />
        {layout.gap > 0 ? <Bar dataKey={SPINE_SPACER_LEFT} {...spacer} /> : null}
        {layout.gap > 0 ? <Bar dataKey={SPINE_SPACER_RIGHT} {...spacer} /> : null}
        {block.series.slice(0, 2).map((entry, index) => (
          <Bar
            key={seriesKey(index)}
            dataKey={seriesKey(index)}
            name={entry.name}
            stackId={STACK_ID}
            fill={colors[index]}
            radius={BUTTERFLY_RADIUS}
            maxBarSize={layout.maxBarSize}
            hide={hidden.has(index)}
            isAnimationActive={animate}
            animationDuration={ANIMATION_MS}
            {...growth(index)}
          />
        ))}
        <ButterflyMarks block={block} layout={layout} />
      </ComposedChart>
      <ChartFacts axis={layout.axis} values={layout.values} barSpacing={layout.barSpacing} />
    </>
  );
}

/**
 * What a butterfly writes from its axis scales: the rungs' names - in the
 * spine's column, or right-aligned on the left axis with a rule where the
 * sides meet - and the values its layout planned (`marks`), each past the
 * outer end of its bar, leftwards on the left side, rightwards on the right,
 * unsigned. The names carry the category ticks' class, which the PPTX
 * extractor reads the chart's text off.
 */
function ButterflyMarks({ block, layout }: { block: DeckChartBlock; layout: ButterflyLayout }) {
  const x = useXAxisScale();
  const y = useYAxisScale();
  if (!x || !y) return null;
  const middle = (category: number) => y(String(category), { position: "middle" });
  const { outer } = layout.values;
  const marks: ReactNode[] = [];
  block.categories.forEach((category, index) => {
    const at = middle(index);
    if (at === undefined || index % layout.categoryStep !== 0) return;
    marks.push(
      <text
        key={`category-${index}`}
        className="deck-chart-tick-category"
        x={layout.categoryX}
        y={at}
        textAnchor={layout.categoryAnchor}
        dominantBaseline="central"
        fill={TICK_FILL}
        style={{ fontSize: layout.categorySize }}
      >
        {category}
      </text>,
    );
  });
  if (layout.spine === "left") {
    const at = x(0);
    if (at !== undefined) {
      marks.push(
        <line
          key="spine"
          className="deck-chart-spine"
          x1={at}
          x2={at}
          y1={layout.plot.top}
          y2={layout.plot.bottom}
          stroke={GRID_STROKE}
          strokeWidth={2}
        />,
      );
    }
  }
  if (outer) {
    for (const mark of layout.marks) {
      const toward = mark.side === 0 ? -1 : 1;
      const end = x(toward * (layout.gap + mark.size));
      const at = middle(mark.category);
      if (end === undefined || at === undefined) continue;
      marks.push(
        <ValueText
          key={`value-${mark.side}-${mark.category}`}
          placement={{ x: end + toward * LABEL_OFFSET, y: at, anchor: mark.side === 0 ? "end" : "start", baseline: "central", rotate: false }}
          style={outer}
          text={mark.text}
          role="value"
          series={mark.side}
          category={mark.category}
        />,
      );
    }
  }
  return <g>{marks}</g>;
}

/* ── Pie / donut ──────────────────────────────────────────────────────────── */

/**
 * A pie or a donut. Its disc and where each value is written - outside at the
 * end of a leader, or inside its slice - are decided in `chart-slices.ts`;
 * Recharts hands each sector's label and leader to the two renderers below,
 * which draw the value that layout planned for the sector's category, or
 * nothing.
 */
function SliceChart({ block, width, height, hidden, animate, locale, growth }: DrawingProps) {
  const layout = useMemo(() => sliceChartLayout(block, width, height, hidden, locale), [block, width, height, hidden, locale]);
  const { slices, labels, values } = layout;
  const donut = block.kind === "donut";
  const total = slices.reduce((sum, slice) => sum + slice.value, 0);
  const label = useMemo(() => chartValueWriter(block, writtenValues(block), locale), [block, locale]);
  const planned = (payload: unknown): { category: number; value: SliceValue } | null => {
    const category = (payload as PieSlice | undefined)?.index;
    const value = category === undefined ? undefined : labels.get(category);
    return category !== undefined && value ? { category, value } : null;
  };

  const renderLabel = (props: PieLabelRenderProps) => {
    const written = planned(props.payload);
    if (!written || !values.outer) return <Fragment />;
    const { category, value } = written;
    return (
      <ValueText
        placement={value.placement}
        style={values.outer}
        text={value.text}
        role={value.inside ? "segment" : "value"}
        series={0}
        category={category}
        segment={value.inside ? paletteColor(category) : undefined}
      />
    );
  };
  const renderLeader = (props: { payload?: unknown }) => {
    const written = planned(props.payload);
    if (!written?.value.leader) return <Fragment />;
    const [[x1, y1], [x2, y2]] = written.value.leader;
    return (
      <line
        className="deck-chart-slice-leader"
        x1={x1}
        y1={y1}
        x2={x2}
        y2={y2}
        stroke={CONNECTOR_STROKE}
        strokeWidth={LEADER_STROKE}
        data-category={written.category}
      />
    );
  };

  return (
    <>
      <PieChart width={width} height={height} margin={{ top: 0, right: 0, bottom: 0, left: 0 }}>
        <Tooltip content={(tooltip) => <ChartTooltip tooltip={tooltip} format={label} />} isAnimationActive={false} />
        <Pie
          data={slices}
          dataKey="value"
          nameKey="name"
          cx={layout.cx}
          cy={layout.cy}
          innerRadius={layout.innerRadius}
          outerRadius={layout.outerRadius}
          paddingAngle={layout.paddingAngle}
          cornerRadius={donut ? 6 : 0}
          startAngle={START_ANGLE}
          endAngle={END_ANGLE}
          stroke="var(--deck-bg)"
          strokeWidth={3}
          isAnimationActive={animate}
          animationDuration={ANIMATION_MS}
          {...growth(0)}
          label={layout.placement ? renderLabel : false}
          labelLine={layout.placement === "outside" ? renderLeader : false}
        >
          {slices.map((slice) => (
            <Cell key={slice.index} fill={slice.color} />
          ))}
        </Pie>
      </PieChart>
      {donut && block.format !== "percent" && slices.length > 1 ? (
        <div className="deck-chart-center" aria-hidden="true">
          <span className="deck-chart-center-value">{label(total)}</span>
        </div>
      ) : null}
      <ChartFacts values={values} pie={layout} />
    </>
  );
}

/* ── Gauge ─────────────────────────────────────────────────────────────────── */

function GaugeChart({ block, width, height, animate, locale, growth }: DrawingProps) {
  const { value, max, ratio } = gaugeReading(block);
  const color = seriesColors(block.series)[0];
  const label = useMemo(() => chartValueWriter(block, writtenValues(block), locale), [block, locale]);
  // The dial's size and its place, the half disc centred in the canvas (`chart-gauge.ts`).
  const { cx, cy, radius, thickness, readingSize } = gaugeLayout(block, width, height);
  const dialProps = {
    dataKey: "value",
    cx,
    cy,
    startAngle: 180,
    endAngle: 0,
    innerRadius: radius - thickness,
    outerRadius: radius,
    stroke: "none",
  } as const;
  // The track is the whole half circle; the value arc is drawn over it, square
  // where the dial starts and rounded at its tip (`gaugeArcPath`), its
  // complement transparent. Kept stable: a new shape is a new chart to
  // Recharts, which would sweep the dial again.
  const track = [{ name: "track", value: 1 }];
  const reading = [
    { name: "value", value: ratio },
    { name: "rest", value: Math.max(1 - ratio, 0) },
  ];
  const readingShape = useCallback(
    (sector: PieSectorShapeProps, index: number) => (
      <path
        className={index === 0 ? "deck-chart-gauge-arc" : undefined}
        d={gaugeArcPath(sector, index === 0 && ratio < 1)}
        fill={sector.fill}
      />
    ),
    [ratio],
  );
  return (
    <>
      <PieChart width={width} height={height} margin={{ top: 0, right: 0, bottom: 0, left: 0 }}>
        <Tooltip
          // The dial's own segments are a ratio and its remainder, which mean
          // nothing to a reader: the hover reads the value itself, like every
          // other chart's does.
          content={(tooltip) =>
            tooltip.active ? <GaugeTooltip block={block} value={value} max={max} color={color} label={label} /> : null
          }
          isAnimationActive={false}
        />
        <Pie {...dialProps} data={track} isAnimationActive={false} fill={GRID_STROKE} />
        <Pie
          {...dialProps}
          data={reading}
          shape={readingShape}
          isAnimationActive={animate}
          animationDuration={ANIMATION_MS}
          {...growth(0)}
        >
          <Cell fill={color} />
          <Cell fill="transparent" />
        </Pie>
      </PieChart>
      <div className="deck-chart-gauge" style={{ bottom: height - cy, fontSize: readingSize }}>
        <span className="deck-chart-gauge-value">{label(value)}</span>
        <span className="deck-chart-gauge-max">
          {block.goal_label ? `${block.goal_label} ${label(max)}` : `/ ${label(max)}`}
        </span>
      </div>
    </>
  );
}

/* ── Tooltip ───────────────────────────────────────────────────────────────── */

/** The gauge's hover reading: its one value, and the maximum when the block names it. */
function GaugeTooltip({
  block,
  value,
  max,
  color,
  label,
}: {
  block: DeckChartBlock;
  value: number;
  max: number;
  color: string;
  /** How the dial writes a number (`chartValueWriter`). */
  label: (amount: number) => string;
}) {
  return (
    <div className="deck-chart-tooltip" role="status">
      <ul className="deck-chart-tooltip-rows">
        <li className="deck-chart-tooltip-row">
          <span className="deck-chart-swatch" style={{ background: color }} aria-hidden="true" />
          <span className="deck-chart-tooltip-name">{block.series[0]?.name ?? ""}</span>
          <span className="deck-chart-tooltip-value">{label(value)}</span>
        </li>
        {block.goal_label ? (
          <li className="deck-chart-tooltip-row">
            <span className="deck-chart-swatch deck-chart-swatch-line" aria-hidden="true" />
            <span className="deck-chart-tooltip-name">{block.goal_label}</span>
            <span className="deck-chart-tooltip-value">{label(max)}</span>
          </li>
        ) : null}
      </ul>
    </div>
  );
}

/**
 * A waterfall bar's hover: its label, its change (or a total's level), and
 * for a change the level it brings the bridge to. Recharts' own row would read
 * the bar's span, which is how it is drawn, not what it says.
 */
function WaterfallTooltip({
  tooltip,
  block,
  steps,
  write,
  runningLabel,
}: {
  tooltip: TooltipContentProps;
  block: DeckChartBlock;
  steps: readonly WaterfallStep[];
  /** How the bridge writes a number (`chartValueWriter`). */
  write: (value: number) => string;
  runningLabel: string;
}) {
  if (!tooltip.active || !tooltip.payload?.length) return null;
  const step = stepOf(steps, tooltip.payload[0]?.payload as Row | undefined);
  if (!step) return null;
  return (
    <div className="deck-chart-tooltip" role="status">
      <div className="deck-chart-tooltip-title">{step.category}</div>
      <ul className="deck-chart-tooltip-rows">
        <li className="deck-chart-tooltip-row">
          <span
            className="deck-chart-swatch"
            style={{ background: waterfallColor(step.role, block.series[0]?.tone) }}
            aria-hidden="true"
          />
          <span className="deck-chart-tooltip-name">{block.series[0]?.name ?? ""}</span>
          <span className="deck-chart-tooltip-value">{waterfallLabel(step, write)}</span>
        </li>
        {step.role === "total" ? null : (
          <li className="deck-chart-tooltip-row">
            <span className="deck-chart-swatch deck-chart-swatch-line" aria-hidden="true" />
            <span className="deck-chart-tooltip-name">{runningLabel}</span>
            <span className="deck-chart-tooltip-value">{write(step.end)}</span>
          </li>
        )}
      </ul>
    </div>
  );
}

/** A butterfly rung's hover: its name, and each visible side's size, unsigned. */
function ButterflyTooltip({
  tooltip,
  block,
  colors,
  hidden,
  write,
}: {
  tooltip: TooltipContentProps;
  block: DeckChartBlock;
  colors: readonly string[];
  hidden: ReadonlySet<number>;
  /** How the chart writes a number, every digit kept (`chartValueWriter`). */
  write: (value: number) => string;
}) {
  if (!tooltip.active || !tooltip.payload?.length) return null;
  const category = Number((tooltip.payload[0]?.payload as Row | undefined)?.[INDEX_KEY]);
  if (!Number.isInteger(category)) return null;
  return (
    <div className="deck-chart-tooltip" role="status">
      <div className="deck-chart-tooltip-title">{block.categories[category]}</div>
      <ul className="deck-chart-tooltip-rows">
        {block.series.slice(0, 2).map((entry, index) =>
          hidden.has(index) ? null : (
            <li key={index} className="deck-chart-tooltip-row">
              <span className="deck-chart-swatch" style={{ background: colors[index] }} aria-hidden="true" />
              <span className="deck-chart-tooltip-name">{entry.name}</span>
              <span className="deck-chart-tooltip-value">{write(Math.abs(entry.values[category] ?? 0))}</span>
            </li>
          ),
        )}
      </ul>
    </div>
  );
}

function ChartTooltip({
  tooltip,
  format,
}: {
  tooltip: TooltipContentProps;
  /** How one entry is written: an axis chart's in the format of the axis its series reads against. */
  format: (value: number, dataKey: unknown) => string;
}) {
  if (!tooltip.active || !tooltip.payload?.length) return null;
  const title = tooltip.label ?? tooltip.payload[0]?.name;
  return (
    <div className="deck-chart-tooltip" role="status">
      {title !== undefined ? <div className="deck-chart-tooltip-title">{String(title)}</div> : null}
      <ul className="deck-chart-tooltip-rows">
        {tooltip.payload.map((entry, index) => {
          const payload = entry.payload as { color?: string } | undefined;
          const color = payload?.color ?? entry.color ?? "";
          return (
            <li key={index} className="deck-chart-tooltip-row">
              <span className="deck-chart-swatch" style={{ background: color }} aria-hidden="true" />
              <span className="deck-chart-tooltip-name">{String(entry.name ?? "")}</span>
              <span className="deck-chart-tooltip-value">{format(Number(entry.value ?? 0), entry.dataKey)}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
