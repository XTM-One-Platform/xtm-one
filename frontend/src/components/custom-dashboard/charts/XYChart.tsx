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
 * The `lineArea` and `bars` kinds: area, line, column and bar charts, with
 * their options: stack modes, overlay series and a second
 * axis, data labels, legend, axis titles / scale / bounds, null handling,
 * series and point colours, annotations from a secondary data source, a brush
 * to zoom, and long series downsampled to the width they are drawn in.
 *
 * Their look's options (`look.ts`) draw the modules' own line, area and bar
 * widgets (`dev-docs/dashboard-rendering-reference.md`): a named palette led by
 * the accent, the series' and the points' own colours (a status, an entity
 * type), gradient fills, rounded bar ends, a bar's thickness and the gap in a
 * group, one colour per bar, totals over stacks, values in strong labels, SI or
 * locale numbers, dates per interval on a category or a time axis, markers,
 * cut category labels, the grid's lines; a click on a value with a link opens
 * it. Without them, XTM One's own look.
 */
import { useId } from "react";
import {
  Area,
  Bar,
  Brush,
  CartesianGrid,
  Cell,
  ComposedChart,
  LabelList,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { useColorMode } from "@/hooks/useColorMode";
import { useFormatter } from "@/i18n";


import type { PlatformView } from "../spec";
import {
  annotationMarks,
  chartRows,
  downsample,
  minMaxIndexes,
  readXYOptions,
  seriesColor,
  seriesRoles,
  truncateLabel,
  type ChartRow,
} from "./chart-options";
import { paletteColor } from "./colors";
import {
  AXIS_STROKE,
  AXIS_TICK,
  compact,
  GRID_STROKE,
  LABEL_STYLE,
  legendProps,
  useBrushReport,
  useChartEvents,
  useElementWidth,
  type ChartProps,
} from "./common";
import { frameOf, optionColors, toNumber, type Field } from "./dynamic-options";
import { crowdedAxis, cutLabel, gridColor, lookCategory, lookColors, lookNumber, nthColor, readLook, seriesColorsOf, timeTicks, valueExtras } from "./look";
import { niceScale, valueBounds } from "./nice-scale";
import { labelPixels } from "./polar-charts";
import { dataHref } from "./links";

/** One point drawn per this many pixels of width at most, unless the brush zooms. */
const PIXELS_PER_POINT = 4;

/** The fields drawn: a distribution's labels as the x and its values as one series. */
function xyFields(shape: ChartProps["shape"], data: unknown): Field[] {
  const fields = frameOf(shape, data);
  return shape === "distribution" ? [{ ...fields[0], name: "x" }, fields[1]] : fields;
}

function percent(value: unknown) {
  return `${Math.round(Number(value))}%`;
}

export default function XYChart({ shape, data, scope, height, view }: ChartProps & { view: PlatformView }) {
  const { locale } = useFormatter();
  const mode = useColorMode();
  const gradientId = useId().replace(/:/g, "");
  const options = readXYOptions(scope.options);
  const look = readLook(scope.options);
  const { ref, width } = useElementWidth<HTMLDivElement>();
  const fields = xyFields(shape, data);
  const names = fields.slice(1).map((f) => f.name);
  const stacked = options.stackMode !== "auto" && view !== "line";
  const stacked100 = stacked && options.stackMode === "stacked100";
  const roles = seriesRoles(names, options);
  // Stacked: the series of the left axis that are not overlays.
  const inStack = (i: number) => stacked && !roles[i].overlay && roles[i].axis === "left";
  let rows = chartRows(fields, options.nullValueDisplay, stacked100 ? new Set(roles.flatMap((_r, i) => (inStack(i) ? [i] : []))) : undefined);
  if (options.yAxisScale === "log") {
    // A log scale has no place for zero or less: those points are gaps.
    rows = rows.map((row) => Object.fromEntries(Object.entries(row).map(([k, v]) => [k, typeof v === "number" && v <= 0 ? null : v])) as ChartRow);
  }
  // What each value of the first series carries: its colour, its link, its key (one per row, before downsampling).
  const extras = valueExtras(shape, data, mode);
  const extrasByX = new Map(rows.map((row, i) => [String(row.x), extras[i] ?? {}]));
  const limit = options.showBrush ? options.resultLimit : Math.min(options.resultLimit, Math.max(10, Math.floor((width || 600) / PIXELS_PER_POINT)));
  rows = downsample(rows, limit);
  const bars = view === "bar" || view === "hbar";
  // A time axis spaces lines and areas by time; bars keep one band per bucket, as wide as the look asks.
  const time = look.xAxisType === "time" && !bars;
  if (time) rows = rows.map((row) => ({ ...row, t: Date.parse(String(row.x)) })).filter((row) => Number.isFinite(row.t));
  const hasRight = roles.some((r) => r.axis === "right");
  const horizontal = view === "hbar";
  const palette = lookColors(look, mode);
  const own = shape === "series" ? seriesColorsOf(data, mode) : [];
  // One colour per point of a single series: the dynamic option, else the points' own, else one palette colour each.
  const dynamicPoints = names.length === 1 && bars ? optionColors(options.pointColors, scope) : undefined;
  const pointColor = (index: number, fallback: string): string => {
    const row = rows[index];
    return (
      dynamicPoints?.[index] ??
      (names.length === 1 && bars ? extrasByX.get(String(row?.x))?.color : undefined) ??
      (look.colorByPoint && names.length === 1 ? nthColor(palette ?? undefined, index) ?? paletteColor(index) : undefined) ??
      fallback
    );
  };
  const perPoint = names.length === 1 && bars && (dynamicPoints !== undefined || look.colorByPoint || rows.some((row) => extrasByX.get(String(row.x))?.color));
  const marks = horizontal ? [] : annotationMarks(scope.annotation, new Set(rows.map((r) => String(r.x))));
  const legend = names.length > 1 ? legendProps(options.legendDisplay, options.legendTruncation, look) : null;
  const yTitle = options.yAxisTitleVisibility === "show" ? options.yAxisTitleText : undefined;
  const xTitle = options.xAxisTitleVisibility === "show" ? options.xAxisTitleText : undefined;
  // Crowded labels tilted rather than dropped, when the look asks (the person's own rotation wins).
  const crowded =
    look.crowdedLabels === "rotate" && !time && !horizontal && options.xAxisLabelRotation === 0
      ? crowdedAxis(rows.map((row) => cutLabel(lookCategory(look, row.x, locale), look.labelMaxLength, look.labelCut)), (width || 600) - 60, look.xAxisFontSize ?? 12)
      : null;
  const rotation = crowded?.rotate ? -45 : options.xAxisLabelRotation;
  const number = (value: unknown) => lookNumber(look, value, compact, locale);
  const tickFormatter = stacked100 ? percent : number;
  const category = (value: unknown) => cutLabel(lookCategory(look, value, locale), look.labelMaxLength, look.labelCut);
  const xTick = { ...AXIS_TICK, ...(look.xAxisFontSize ? { fontSize: look.xAxisFontSize } : {}), ...(look.axisLabelColor ? { fill: look.axisLabelColor } : {}) };
  const yTick = { ...AXIS_TICK, ...(look.yAxisFontSize ? { fontSize: look.yAxisFontSize } : {}), ...(look.axisLabelColor ? { fill: look.axisLabelColor } : {}) };
  const axisLine = look.hideAxisLines ? { axisLine: false, tickLine: false } : { stroke: AXIS_STROKE };
  // The value axis in round steps from the data's bounds, as many as its room allows (or as the look
  // asks), unless the person bounds it or it is a log or a 100% scale. Bars stand on zero there too.
  const bounds = valueBounds(rows, roles.length, inStack);
  const nice =
    look.niceTicks && !stacked100 && options.yAxisScale !== "log" && options.yAxisMin === undefined && options.yAxisMax === undefined
      ? niceScale({
          min: bars ? Math.min(0, bounds.min) : bounds.min,
          max: bars ? Math.max(0, bounds.max) : bounds.max,
          length: horizontal ? width || 600 : height,
          horizontal,
          tickAmount: look.valueTickCount,
        })
      : undefined;
  const valueTicks = nice ? { ticks: nice, interval: 0 as const } : look.valueTickCount !== undefined ? { tickCount: look.valueTickCount + 1 } : {};
  const { onSelect, onBrush } = useChartEvents();
  const onBrushChange = useBrushReport(rows, onBrush);
  // A click on the chart names the point under the pointer: its x, the first series' value, its key
  // when it has one; with no reaction set, a point with a link opens it when the look says so.
  const openable = !onSelect && look.openLinks && rows.some((row) => extrasByX.get(String(row.x))?.link);
  const onClick =
    onSelect || openable
      ? (state: { activeTooltipIndex?: number | string | null } | null) => {
          const row = rows[Number(state?.activeTooltipIndex)] as Record<string, unknown> | undefined;
          if (!row) return;
          const point = extrasByX.get(String(row.x));
          if (!onSelect) {
            const href = dataHref(point?.link);
            if (href) window.open(href, "_blank", "noopener,noreferrer");
            return;
          }
          const y = row.s0 ?? "";
          onSelect({ name: names[0], x: String(row.x), y: String(y ?? ""), value: point?.key ?? (shape === "distribution" ? String(row.x) : String(y ?? "")) });
        }
      : undefined;
  // Bars stand on zero (or reach below it): a bar cut at its lowest value would read as nothing.
  const floor = bars && options.yAxisScale !== "log" ? (dataMin: number) => Math.min(0, dataMin) : "auto";
  const domain = (min?: number, max?: number) =>
    [min ?? (stacked100 ? 0 : floor), max ?? (stacked100 ? 100 : "auto")] as [number | string | ((dataMin: number) => number), number | string];
  const valueDomain = nice ? ([nice[0], nice[nice.length - 1]] as [number, number]) : domain(options.yAxisMin, options.yAxisMax);
  const ticks = time ? timeTicks(rows.map((r) => Number(r.t)), Math.max(2, Math.floor((width || 600) / 110))) : undefined;
  const gridProps =
    look.gridLines === "none"
      ? null
      : {
          horizontal: look.gridLines !== "vertical",
          vertical: look.gridLines === undefined || look.gridLines === "both" || look.gridLines === "vertical",
          stroke: look.gridOpacity !== undefined ? gridColor(look.gridOpacity, mode) : GRID_STROKE,
          opacity: look.gridOpacity !== undefined ? 1 : 0.4,
          // Laid out in round steps, the lines along the categories part them rather than cross them.
          ...(nice && horizontal ? { horizontalCoordinatesGenerator: bandEdges(rows.length) } : {}),
        };
  const gradient = look.fill === "gradient";
  // The bar of a stack whose end is rounded: the last series stacked (the top of the stack).
  const lastInStack = roles.reduce((last, _r, i) => (inStack(i) ? i : last), -1);
  const radius = (i: number): number | [number, number, number, number] | undefined => {
    if (look.barRadius === undefined || (inStack(i) && i !== lastInStack)) return undefined;
    const r = look.barRadius;
    return horizontal ? [0, r, r, 0] : [r, r, 0, 0];
  };

  const categoryAxis = time ? (
    <XAxis
      dataKey="t"
      type="number"
      scale="time"
      domain={["dataMin", "dataMax"]}
      ticks={ticks}
      tickFormatter={(t: number) => category(new Date(t).toISOString())}
      tick={xTick}
      {...axisLine}
      height={xTitle ? 40 : 24}
      label={xTitle ? { value: xTitle, position: "insideBottom", offset: 0, style: LABEL_STYLE } : undefined}
    />
  ) : (
    <XAxis
      dataKey="x"
      type="category"
      tick={xTick}
      {...axisLine}
      tickFormatter={look.dateFormat || look.labelMaxLength ? category : undefined}
      minTickGap={8}
      interval={crowded?.rotate ? 0 : undefined}
      angle={rotation}
      textAnchor={rotation < 0 ? "end" : rotation > 0 ? "start" : "middle"}
      height={crowded?.rotate ? crowded.height + (xTitle ? 16 : 0) : rotation === 0 ? (xTitle ? 40 : 24) : 56}
      label={xTitle ? { value: xTitle, position: "insideBottom", offset: 0, style: LABEL_STYLE } : undefined}
    />
  );
  const valueAxis = (
    <YAxis
      yAxisId="left"
      type="number"
      scale={options.yAxisScale}
      domain={valueDomain}
      allowDataOverflow={options.yAxisMin !== undefined || options.yAxisMax !== undefined}
      tickFormatter={tickFormatter}
      tick={yTick}
      {...axisLine}
      {...valueTicks}
      width={yTitle ? 56 : 44}
      label={yTitle ? { value: yTitle, angle: -90, position: "insideLeft", style: LABEL_STYLE } : undefined}
    />
  );
  // A bar's thickness: the group's share of its band, split among the bars side by side in it.
  const sideBySide = roles.filter((role, i) => !role.overlay && (!inStack(i) || i === lastInStack)).length;
  // A horizontal chart's category labels take the room their longest needs - at most 160 px (220 px
  // when the look cuts them to a length) - and a label longer than that room ends in an ellipsis.
  const categoryFont = Number(xTick.fontSize) || 11;
  const written = horizontal ? rows.map((row) => (look.dateFormat || look.labelMaxLength ? category(row.x) : String(row.x ?? ""))) : [];
  const categoryMax = look.labelMaxLength ? 220 : 160;
  // The axis keeps about 18 px for its tick and margin.
  const categoryWidth = Math.min(categoryMax, Math.max(40, Math.max(0, ...written.map((label) => labelPixels(label, categoryFont))) + 18));
  const categoryChars = Math.max(4, Math.floor((categoryWidth - 18) / (categoryFont * 0.6)));
  const categoryTick = (value: unknown) =>
    truncateLabel(look.dateFormat || look.labelMaxLength ? category(value) : String(value ?? ""), categoryChars, "ellipsisEnd");
  // The band of one category in pixels: the plot's length (the chart less its axes and margins) over the categories.
  const plotLength = horizontal ? height - 8 - (yTitle ? 40 : 24) - (legend ? 32 : 0) : (width || 600) - (yTitle ? 56 : 44) - 16;
  const band = plotLength / Math.max(1, rows.length);
  const barSize = look.barSize !== undefined && bars ? Math.max(1, Math.floor((band * look.barSize) / 100 / Math.max(1, sideBySide))) : undefined;

  return (
    <div ref={ref} style={{ height }} className="min-w-0">
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart
          data={rows}
          layout={horizontal ? "vertical" : "horizontal"}
          margin={{ top: 8, right: look.dataValuesStrong && horizontal ? 32 : 8, bottom: 0, left: 0 }}
          onClick={onClick}
          className={onClick ? "cursor-pointer" : undefined}
          barSize={barSize}
          barGap={look.barGap}
        >
          {gradient && (
            <defs>
              {roles.map((role, i) => {
                const color = seriesColor(role.name, i, options, (index) => own[index] ?? nthColor(palette, index) ?? paletteColor(index));
                return (
                  <linearGradient key={i} id={`${gradientId}-s${i}`} x1="0" y1="0" x2={horizontal ? "1" : "0"} y2={horizontal ? "0" : "1"}>
                    <stop offset="0%" stopColor={color} stopOpacity={look.fillOpacityFrom} />
                    <stop offset="100%" stopColor={color} stopOpacity={look.fillOpacityTo} />
                  </linearGradient>
                );
              })}
              {perPoint &&
                rows.map((_row, index) => {
                  const color = pointColor(index, seriesColor(roles[0]?.name ?? "", 0, options, (n) => own[n] ?? nthColor(palette, n) ?? paletteColor(n)));
                  return (
                    <linearGradient key={`p${index}`} id={`${gradientId}-p${index}`} x1="0" y1="0" x2={horizontal ? "1" : "0"} y2={horizontal ? "0" : "1"}>
                      <stop offset="0%" stopColor={color} stopOpacity={look.fillOpacityFrom} />
                      <stop offset="100%" stopColor={color} stopOpacity={look.fillOpacityTo} />
                    </linearGradient>
                  );
                })}
            </defs>
          )}
          {gridProps && <CartesianGrid strokeDasharray="3 3" {...gridProps} />}
          {horizontal ? (
            <>
              <XAxis
                type="number"
                xAxisId="value"
                scale={options.yAxisScale}
                domain={valueDomain}
                allowDataOverflow={options.yAxisMin !== undefined || options.yAxisMax !== undefined}
                tickFormatter={tickFormatter}
                tick={yTick}
                {...axisLine}
                {...valueTicks}
                label={yTitle ? { value: yTitle, position: "insideBottom", offset: 0, style: LABEL_STYLE } : undefined}
                height={yTitle ? 40 : 24}
              />
              <YAxis
                type="category"
                dataKey="x"
                // One line each, cut to the room: the library's own tick would wrap a long label.
                tick={(props: CategoryTickProps) => <CategoryTick {...props} style={xTick} format={categoryTick} />}
                interval={0}
                width={categoryWidth}
                {...axisLine}
                label={xTitle ? { value: xTitle, angle: -90, position: "insideLeft", style: LABEL_STYLE } : undefined}
              />
            </>
          ) : (
            <>
              {categoryAxis}
              {valueAxis}
              {hasRight && (
                <YAxis
                  yAxisId="right"
                  orientation="right"
                  type="number"
                  scale={options.y2AxisScale}
                  domain={domain(options.y2AxisMin, options.y2AxisMax)}
                  allowDataOverflow={options.y2AxisMin !== undefined || options.y2AxisMax !== undefined}
                  tickFormatter={number}
                  tick={yTick}
                  {...axisLine}
                  width={options.y2AxisTitleText ? 56 : 44}
                  label={options.y2AxisTitleText ? { value: options.y2AxisTitleText, angle: 90, position: "insideRight", style: LABEL_STYLE } : undefined}
                />
              )}
            </>
          )}
          <Tooltip
            {...CHART_TOOLTIP_PROPS}
            formatter={(v, _name, item) => (stacked100 && inStack(Number(String(item?.dataKey ?? "").slice(1))) ? percent(v) : number(v))}
            labelFormatter={(label) => (time ? lookCategory({ dateFormat: look.dateFormat ?? "date" }, new Date(Number(label)).toISOString(), locale) : look.dateFormat ? lookCategory(look, label, locale) : String(label ?? ""))}
            // The series in the data's order (s0, s1...), not by name.
            itemSorter="dataKey"
          />
          {legend && <Legend {...legend} />}
          {roles.map((role, i) => {
            const key = `s${i}`;
            const color = seriesColor(role.name, i, options, (index) => own[index] ?? nthColor(palette, index) ?? paletteColor(index));
            const fill = gradient ? `url(#${gradientId}-s${i})` : color;
            const axis = horizontal ? { xAxisId: "value" } : { yAxisId: role.axis };
            const labels = options.dataValuesDisplay !== "off" && (
              <LabelList
                dataKey={key}
                position={horizontal ? "right" : "top"}
                content={valueLabel(options.dataValuesDisplay === "minmax" ? minMaxIndexes(rows.map((r) => r[key])) : null, stacked100 && inStack(i), horizontal, look.dataValuesStrong, number)}
              />
            );
            const totals = look.stackTotals && i === lastInStack && <LabelList dataKey={key} position={horizontal ? "right" : "top"} content={totalLabel(rows, roles.map((_r, n) => n).filter(inStack), horizontal, number)} />;
            const common = { dataKey: key, name: role.name, isAnimationActive: false, ...axis };
            const marker = look.markerSize;
            const dot =
              marker !== undefined
                ? { r: marker, fill: color, stroke: "var(--bg-elevation-default)", strokeWidth: 2 }
                : options.markerDisplay === "off"
                  ? false
                  : { r: 3, stroke: color, fill: options.markerDisplay === "filled" ? color : "var(--bg-elevation-default)" };
            const activeDot = marker !== undefined ? { r: marker + 2 } : undefined;
            if ((role.overlay || view === "line") && !(view === "line" && gradient && !role.overlay)) {
              return (
                <Line
                  key={key}
                  {...common}
                  type="monotone"
                  stroke={color}
                  strokeWidth={options.lineWidth}
                  strokeDasharray={role.overlay && view === "line" ? "5 3" : undefined}
                  connectNulls={options.nullValueDisplay === "connect"}
                  dot={dot}
                  activeDot={activeDot}
                >
                  {labels}
                </Line>
              );
            }
            if (view === "area" || view === "line") {
              // A line with a gradient fill is an area under its line.
              return (
                <Area
                  key={key}
                  {...common}
                  type="monotone"
                  stackId={inStack(i) ? "stack" : undefined}
                  stroke={color}
                  strokeWidth={options.lineWidth}
                  fill={fill}
                  fillOpacity={gradient ? 1 : options.areaOpacity}
                  connectNulls={options.nullValueDisplay === "connect"}
                  dot={dot}
                  activeDot={activeDot}
                >
                  {labels}
                </Area>
              );
            }
            return (
              <Bar key={key} {...common} fill={fill} stackId={inStack(i) ? "stack" : undefined} radius={radius(i)}>
                {perPoint && rows.map((_row, index) => <Cell key={index} fill={gradient ? `url(#${gradientId}-p${index})` : pointColor(index, color)} />)}
                {labels}
                {totals}
              </Bar>
            );
          })}
          {marks.map((mark, index) => (
            <ReferenceLine
              key={`${mark.x}-${index}`}
              x={mark.x}
              yAxisId="left"
              stroke={options.annotationColor ?? "var(--color-feedback-alert-primary)"}
              strokeDasharray="4 2"
              label={mark.label ? { value: mark.label, position: "insideTopLeft", style: { ...LABEL_STYLE, fill: "var(--text-default-primary)" } } : undefined}
            />
          ))}
          {(options.showBrush || onBrush) && !horizontal && rows.length > 2 && (
            <Brush
              dataKey="x"
              height={18}
              stroke="var(--color-accent)"
              fill="var(--bg-elevation-default)"
              travellerWidth={8}
              onChange={onBrush ? onBrushChange : undefined}
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

/** A data label: every value, or only those at `only` (the smallest and largest); `strong`, 12 px bold
 *  in the heading font, 8 px off a horizontal bar's end. */
function valueLabel(only: Set<number> | null, asPercent: boolean, horizontal: boolean, strong = false, format: (value: unknown) => string = compact) {
  function ValueLabel(props: { x?: unknown; y?: unknown; width?: unknown; height?: unknown; value?: unknown; index?: number }) {
    const value = toNumber(props.value);
    if (value === null || (only && !only.has(props.index ?? -1))) return null;
    const x = Number(props.x ?? 0) + Number(props.width ?? 0) / (horizontal ? 1 : 2) + (horizontal ? (strong ? 8 : 4) : 0);
    const y = horizontal ? Number(props.y ?? 0) + Number(props.height ?? 0) / 2 + (strong ? 4 : 3) : Number(props.y ?? 0) - 4;
    return (
      <text
        x={x}
        y={y}
        textAnchor={horizontal ? "start" : "middle"}
        fontSize={strong ? 12 : 10}
        fontWeight={strong ? 600 : undefined}
        fontFamily={strong ? "Geologica, sans-serif" : undefined}
        fill="var(--text-default-primary)"
      >
        {asPercent ? percent(value) : format(value)}
      </text>
    );
  }
  return ValueLabel;
}

/** The total of a stack, over its top bar, in 13 px black. */
function totalLabel(rows: ChartRow[], stackedSeries: number[], horizontal: boolean, format: (value: unknown) => string) {
  function TotalLabel(props: { x?: unknown; y?: unknown; width?: unknown; height?: unknown; index?: number }) {
    const row = rows[props.index ?? -1];
    if (!row) return null;
    const total = stackedSeries.reduce((sum, i) => sum + (toNumber(row[`s${i}`]) ?? 0), 0);
    const x = Number(props.x ?? 0) + Number(props.width ?? 0) / (horizontal ? 1 : 2) + (horizontal ? 6 : 0);
    const y = horizontal ? Number(props.y ?? 0) + Number(props.height ?? 0) / 2 + 4 : Number(props.y ?? 0) - 6;
    return (
      <text x={x} y={y} textAnchor={horizontal ? "start" : "middle"} fontSize={13} fontWeight={900} fill="var(--text-default-primary)">
        {format(total)}
      </text>
    );
  }
  return TotalLabel;
}

/** The grid's lines on the edges of `count` bands across the plot, rather than through their middles. */
function bandEdges(count: number) {
  return ({ offset }: { offset: { top?: number; height?: number } }) => {
    const top = offset.top ?? 0;
    const length = offset.height ?? 0;
    return Array.from({ length: Math.max(1, count) + 1 }, (_, i) => top + (length * i) / Math.max(1, count));
  };
}

interface CategoryTickProps {
  x?: number | string;
  y?: number | string;
  payload?: { value?: unknown };
}

/** A category's label on one line, right-aligned against the axis. */
function CategoryTick({ x, y, payload, style, format }: CategoryTickProps & { style: { fill?: string; fontSize?: number }; format: (value: unknown) => string }) {
  return (
    <text x={Number(x ?? 0) - 4} y={Number(y ?? 0)} textAnchor="end" dominantBaseline="central" fill={style.fill} fontSize={style.fontSize}>
      {format(payload?.value)}
    </text>
  );
}
