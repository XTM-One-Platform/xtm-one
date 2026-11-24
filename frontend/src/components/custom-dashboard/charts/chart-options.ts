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
 * A visualization's options as the renderers read them, and the data shaping they share.
 *
 * Options come from a stored definition (`definition.ts` merges the defaults
 * under them), so they are read here defensively: every enum against its
 * list, every number clamped, every string bounded. A value the backend would
 * refuse (`dashboard_visualization_options.py`) falls back to its default
 * rather than failing the widget. Options that may be dynamic (an expression
 * or a colour) are kept as written and evaluated by `dynamic-options.ts`.
 *
 * Pure, relative imports only: runs on `node --test`.
 */
import { resolveColor } from "./colors.ts";
import { toNumber, type Cell, type Field } from "./dynamic-options.ts";

type Raw = Record<string, unknown>;
const isRecord = (value: unknown): value is Raw => typeof value === "object" && value !== null && !Array.isArray(value);

function oneOf<T extends string | number>(value: unknown, allowed: readonly T[], fallback: T): T {
  return (allowed as readonly unknown[]).includes(value) ? (value as T) : fallback;
}
function num(value: unknown, min: number, max: number): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : undefined;
}
function str(value: unknown, max: number): string | undefined {
  if (typeof value !== "string") return undefined;
  const out = value.trim();
  return out ? out.slice(0, max) : undefined;
}
function strings(value: unknown, max: number): string[] {
  const list = typeof value === "string" ? [value] : Array.isArray(value) ? value : [];
  return list.filter((v): v is string => typeof v === "string" && v.length > 0).slice(0, max).map((v) => v.slice(0, 120));
}
const bool = (value: unknown, fallback: boolean): boolean => (typeof value === "boolean" ? value : fallback);

export const LEGEND_DISPLAYS = ["right", "left", "top", "bottom", "off"] as const;
export type LegendDisplay = (typeof LEGEND_DISPLAYS)[number];
export const LEGEND_TRUNCATIONS = ["ellipsisEnd", "ellipsisMiddle", "ellipsisStart", "ellipsisOff"] as const;
export type LegendTruncation = (typeof LEGEND_TRUNCATIONS)[number];
type Scale = "linear" | "log";

/** The colours of a palette option: a list of named colours or hex values (`seriesColors`). */
function colorList(value: unknown): (string | undefined)[] {
  return Array.isArray(value) ? value.slice(0, 20).map(resolveColor) : [];
}
/** Colours by series name (`seriesColorsByField`). */
function colorMap(value: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (!isRecord(value)) return out;
  for (const [key, color] of Object.entries(value).slice(0, 50)) {
    const resolved = resolveColor(color);
    if (resolved) out[key] = resolved;
  }
  return out;
}

interface AxisOptions {
  xAxisTitleText?: string;
  yAxisTitleText?: string;
  xAxisTitleVisibility: "show" | "hide";
  yAxisTitleVisibility: "show" | "hide";
  yAxisScale: Scale;
  yAxisMin?: number;
  yAxisMax?: number;
  xAxisLabelRotation: -90 | -45 | 0 | 45 | 90;
}

function axisOptions(o: Raw): AxisOptions {
  return {
    xAxisTitleText: str(o.xAxisTitleText, 80),
    yAxisTitleText: str(o.yAxisTitleText, 80),
    xAxisTitleVisibility: oneOf(o.xAxisTitleVisibility, ["show", "hide"] as const, "show"),
    yAxisTitleVisibility: oneOf(o.yAxisTitleVisibility, ["show", "hide"] as const, "show"),
    yAxisScale: oneOf(o.yAxisScale, ["linear", "log"] as const, "linear"),
    yAxisMin: num(o.yAxisMin, -1e15, 1e15),
    yAxisMax: num(o.yAxisMax, -1e15, 1e15),
    xAxisLabelRotation: oneOf(o.xAxisLabelRotation, [-90, -45, 0, 45, 90] as const, 0),
  };
}

export interface XYOptions extends AxisOptions {
  stackMode: "auto" | "stacked" | "stacked100";
  overlayFields: string[];
  y2Fields: string[];
  showOverlayY2Axis: boolean;
  y2AxisTitleText?: string;
  y2AxisScale: Scale;
  y2AxisMin?: number;
  y2AxisMax?: number;
  dataValuesDisplay: "off" | "all" | "minmax";
  legendDisplay: LegendDisplay;
  legendTruncation: LegendTruncation;
  nullValueDisplay: "gaps" | "zero" | "connect";
  seriesColors: (string | undefined)[];
  seriesColorsByField: Record<string, string>;
  /** One colour per point of a single series (an expression, `dynamic-options`). */
  pointColors: unknown;
  markerDisplay: "off" | "filled" | "outlined";
  lineWidth: number;
  areaOpacity: number;
  annotationColor?: string;
  showBrush: boolean;
  /** The most points drawn per series; longer series are downsampled. */
  resultLimit: number;
}

const MAX_RESULT_LIMIT = 1000;

export function readXYOptions(raw: unknown): XYOptions {
  const o = isRecord(raw) ? raw : {};
  return {
    ...axisOptions(o),
    stackMode: oneOf(o.stackMode, ["auto", "stacked", "stacked100"] as const, "auto"),
    overlayFields: strings(o.overlayFields, 10),
    y2Fields: strings(o.y2Fields, 10),
    showOverlayY2Axis: bool(o.showOverlayY2Axis, false),
    y2AxisTitleText: str(o.y2AxisTitleText, 80),
    y2AxisScale: oneOf(o.y2AxisScale, ["linear", "log"] as const, "linear"),
    y2AxisMin: num(o.y2AxisMin, -1e15, 1e15),
    y2AxisMax: num(o.y2AxisMax, -1e15, 1e15),
    dataValuesDisplay: oneOf(o.dataValuesDisplay, ["off", "all", "minmax"] as const, "off"),
    legendDisplay: oneOf(o.legendDisplay, LEGEND_DISPLAYS, "bottom"),
    legendTruncation: oneOf(o.legendTruncation, LEGEND_TRUNCATIONS, "ellipsisEnd"),
    nullValueDisplay: oneOf(o.nullValueDisplay, ["gaps", "zero", "connect"] as const, "gaps"),
    seriesColors: colorList(o.seriesColors),
    seriesColorsByField: colorMap(o.seriesColorsByField),
    pointColors: o.pointColors,
    markerDisplay: oneOf(o.markerDisplay, ["off", "filled", "outlined"] as const, "off"),
    lineWidth: num(o.lineWidth, 1, 6) ?? 2,
    areaOpacity: num(o.areaOpacity, 0, 1) ?? 0.25,
    annotationColor: resolveColor(o.annotationColor),
    showBrush: bool(o.showBrush, false),
    resultLimit: Math.round(num(o.resultLimit, 10, MAX_RESULT_LIMIT) ?? MAX_RESULT_LIMIT),
  };
}

export interface PieOptions {
  collapseThreshold: number;
  /** The collapsed slice's name; unset, the renderer's own ("Other", translated). */
  collapseLabel?: string;
  showDonutHole: boolean;
  labelDisplay: "off" | "values" | "valuesAndPercentage" | "percentage";
  legendDisplay: LegendDisplay;
  legendTruncation: LegendTruncation;
  seriesColors: (string | undefined)[];
  seriesColorsByField: Record<string, string>;
  /** A polar area's slices' opacity. */
  areaOpacity?: number;
}

export function readPieOptions(raw: unknown): PieOptions {
  const o = isRecord(raw) ? raw : {};
  return {
    collapseThreshold: num(o.collapseThreshold, 0, 0.5) ?? 0.01,
    collapseLabel: str(o.collapseLabel, 40),
    showDonutHole: bool(o.showDonutHole, true),
    labelDisplay: oneOf(o.labelDisplay, ["off", "values", "valuesAndPercentage", "percentage"] as const, "off"),
    legendDisplay: oneOf(o.legendDisplay, LEGEND_DISPLAYS, "right"),
    legendTruncation: oneOf(o.legendTruncation, LEGEND_TRUNCATIONS, "ellipsisEnd"),
    seriesColors: colorList(o.seriesColors),
    seriesColorsByField: colorMap(o.seriesColorsByField),
    areaOpacity: num(o.areaOpacity, 0, 1),
  };
}

export interface ScatterOptions extends AxisOptions {
  xField?: string;
  yField?: string;
  sizeField?: string;
  categoryField?: string;
  legendDisplay: LegendDisplay;
  seriesColors: (string | undefined)[];
  seriesColorsByField: Record<string, string>;
  bubbleSizeMin: number;
  bubbleSizeMax: number;
}

export function readScatterOptions(raw: unknown): ScatterOptions {
  const o = isRecord(raw) ? raw : {};
  const min = num(o.bubbleSizeMin, 2, 40) ?? 4;
  return {
    ...axisOptions(o),
    xField: str(o.xField, 120),
    yField: str(o.yField, 120),
    sizeField: str(o.sizeField, 120),
    categoryField: str(o.categoryField, 120),
    legendDisplay: oneOf(o.legendDisplay, LEGEND_DISPLAYS, "bottom"),
    seriesColors: colorList(o.seriesColors),
    seriesColorsByField: colorMap(o.seriesColorsByField),
    bubbleSizeMin: min,
    bubbleSizeMax: Math.max(min, num(o.bubbleSizeMax, 2, 60) ?? 24),
  };
}

export interface SingleValueOptions {
  majorValue: unknown;
  trendValue: unknown;
  sparklineValues: unknown;
  trendDisplay: "absolute" | "percent" | "off";
  sparklineDisplay: "below" | "before" | "after" | "off";
  showSparklineAreaGraph: boolean;
  unit?: string;
  unitPosition: "before" | "after";
  numberPrecision: number;
  shouldUseThousandSeparators: boolean;
  shouldAbbreviateValue: boolean;
  shouldAbbreviateTrendValue: boolean;
  underLabel?: string;
  majorColor: unknown;
  trendColor: unknown;
  backgroundColor: unknown;
  sparklineStrokeColor: unknown;
  // single value icon
  icon?: string;
  iconColor: unknown;
  iconPosition: "before" | "after" | "above";
  showValue: boolean;
  // single value radial
  maxValue: number;
  radialStrokeColor: unknown;
  radialBackgroundColor: unknown;
}

/** The single value's defaults: the last point of the first series, its delta, its sparkline. */
const SINGLE_VALUE_DEFAULTS = {
  sparklineValues: "> primary | seriesByIndex(1)",
  majorValue: "> sparklineValues | lastPoint()",
  trendValue: "> sparklineValues | delta(-2)",
} as const;

/** A `number` source's: its value, its change over the period, no sparkline. */
const NUMBER_DEFAULTS = {
  majorValue: "> primary | seriesByName('value') | lastPoint()",
  trendValue: "> primary | seriesByName('change') | lastPoint()",
  value: "> primary | seriesByName('value') | lastPoint()",
} as const;

/** The options of a single value or a gauge with the defaults its data's shape calls for set
 *  under them, so an expression may name a default (`> sparklineValues | lastPoint()`). */
export function withValueDefaults(shape: string, options: Raw): Raw {
  const defaults: Raw =
    shape === "number" ? NUMBER_DEFAULTS : { ...SINGLE_VALUE_DEFAULTS, value: "> primary | seriesByIndex(1) | lastPoint()" };
  return { ...defaults, ...options };
}

export function readSingleValueOptions(raw: unknown): SingleValueOptions {
  const o = isRecord(raw) ? raw : {};
  return {
    majorValue: o.majorValue,
    trendValue: o.trendValue,
    sparklineValues: o.sparklineValues,
    trendDisplay: oneOf(o.trendDisplay, ["absolute", "percent", "off"] as const, "absolute"),
    sparklineDisplay: oneOf(o.sparklineDisplay, ["below", "before", "after", "off"] as const, "below"),
    showSparklineAreaGraph: bool(o.showSparklineAreaGraph, false),
    unit: str(o.unit, 12),
    unitPosition: oneOf(o.unitPosition, ["before", "after"] as const, "after"),
    numberPrecision: Math.trunc(num(o.numberPrecision, 0, 6) ?? 0),
    shouldUseThousandSeparators: bool(o.shouldUseThousandSeparators, true),
    // Abbreviated unless told otherwise: a figure fits a small panel (1.2M).
    shouldAbbreviateValue: bool(o.shouldAbbreviateValue, true),
    shouldAbbreviateTrendValue: bool(o.shouldAbbreviateTrendValue, false),
    underLabel: str(o.underLabel, 80),
    majorColor: o.majorColor,
    trendColor: o.trendColor,
    backgroundColor: o.backgroundColor,
    sparklineStrokeColor: o.sparklineStrokeColor,
    icon: str(o.icon, 60),
    iconColor: o.iconColor,
    iconPosition: oneOf(o.iconPosition, ["before", "after", "above"] as const, "before"),
    showValue: bool(o.showValue, true),
    maxValue: num(o.maxValue, -1e15, 1e15) ?? 100,
    radialStrokeColor: o.radialStrokeColor,
    radialBackgroundColor: o.radialBackgroundColor,
  };
}

interface GaugeRange {
  from: number;
  to: number;
  color?: string;
}

export interface GaugeOptions {
  value: unknown;
  minValue: number;
  maxValue: number;
  gaugeRanges: GaugeRange[];
  orientation: "horizontal" | "vertical";
  gaugeColor: unknown;
  labelDisplay: "number" | "percentage" | "off";
  valueDisplay: "number" | "percentage" | "off";
  unit?: string;
  numberPrecision: number;
  majorTickInterval?: number;
}

export function readGaugeOptions(raw: unknown): GaugeOptions {
  const o = isRecord(raw) ? raw : {};
  const ranges: GaugeRange[] = [];
  for (const item of Array.isArray(o.gaugeRanges) ? o.gaugeRanges.slice(0, 10) : []) {
    if (!isRecord(item)) continue;
    const from = num(item.from, -1e15, 1e15);
    const to = num(item.to, -1e15, 1e15);
    if (from === undefined || to === undefined || to <= from) continue;
    ranges.push({ from, to, color: resolveColor(item.value) });
  }
  ranges.sort((a, b) => a.from - b.from);
  // The ranges span the scale when they are given; else minValue..maxValue.
  let minValue = ranges.length ? ranges[0].from : num(o.minValue, -1e15, 1e15) ?? 0;
  let maxValue = ranges.length ? Math.max(...ranges.map((r) => r.to)) : num(o.maxValue, -1e15, 1e15) ?? 100;
  if (maxValue <= minValue) [minValue, maxValue] = [0, 100];
  return {
    value: o.value,
    minValue,
    maxValue,
    gaugeRanges: ranges,
    orientation: oneOf(o.orientation, ["horizontal", "vertical"] as const, "horizontal"),
    gaugeColor: o.gaugeColor,
    labelDisplay: oneOf(o.labelDisplay, ["number", "percentage", "off"] as const, "number"),
    valueDisplay: oneOf(o.valueDisplay, ["number", "percentage", "off"] as const, "number"),
    unit: str(o.unit, 12),
    numberPrecision: Math.trunc(num(o.numberPrecision, 0, 6) ?? 0),
    majorTickInterval: num(o.majorTickInterval, 1e-9, 1e15),
  };
}

export interface ColumnFormat {
  width?: number;
  align: "left" | "center" | "right";
  textOverflow: "ellipsis" | "break-word" | "anywhere";
  cellType: "text" | "array" | "sparkline";
  data: unknown;
  rowColors: unknown;
  rowBackgroundColors: unknown;
}

export interface TableOptions {
  count: number;
  headerVisibility: "inline" | "fixed" | "none";
  showRowNumbers: boolean;
  showTotals: boolean;
  columnFormat: Record<string, ColumnFormat>;
  rowColors: unknown;
  rowBackgroundColors: unknown;
}

export function readTableOptions(raw: unknown): TableOptions {
  const o = isRecord(raw) ? raw : {};
  const columnFormat: Record<string, ColumnFormat> = {};
  for (const [key, value] of Object.entries(isRecord(o.columnFormat) ? o.columnFormat : {}).slice(0, 20)) {
    const f = isRecord(value) ? value : {};
    columnFormat[key] = {
      width: num(f.width, 40, 800),
      align: oneOf(f.align, ["left", "center", "right"] as const, "left"),
      textOverflow: oneOf(f.textOverflow, ["ellipsis", "break-word", "anywhere"] as const, "break-word"),
      cellType: oneOf(f.cellType, ["text", "array", "sparkline"] as const, "text"),
      data: f.data,
      rowColors: f.rowColors,
      rowBackgroundColors: f.rowBackgroundColors,
    };
  }
  const table = isRecord(o.tableFormat) ? o.tableFormat : {};
  return {
    count: Math.round(num(o.count, 1, 100) ?? 10),
    headerVisibility: oneOf(o.headerVisibility, ["inline", "fixed", "none"] as const, "inline"),
    showRowNumbers: bool(o.showRowNumbers, false),
    showTotals: bool(o.showTotals, false),
    columnFormat,
    rowColors: table.rowColors,
    rowBackgroundColors: table.rowBackgroundColors,
  };
}

// ── Series ───────────────────────────────────────────────────────────────────

/** One row of a cartesian chart: its x, then `s<index>` per series. */
export type ChartRow = Record<string, string | number | null>;

/** The series fields of a frame (`frameOf("series")`) as chart rows, nulls drawn as asked;
 *  the series of `stacked100` (their indexes) as shares of their own total on the row. */
export function chartRows(fields: Field[], nullValueDisplay: XYOptions["nullValueDisplay"], stacked100?: ReadonlySet<number>): ChartRow[] {
  const [x, ...series] = fields;
  if (!x) return [];
  return x.values.map((label, row) => {
    const out: ChartRow = { x: label === null ? "" : String(label) };
    const values = series.map((s) => toNumber(s.values[row]));
    const total = values.reduce<number>((sum, v, i) => (stacked100?.has(i) ? sum + Math.abs(v ?? 0) : sum), 0);
    values.forEach((v, i) => {
      const value = v === null && nullValueDisplay === "zero" ? 0 : v;
      out[`s${i}`] = stacked100?.has(i) && value !== null ? (total ? (Math.abs(value) / total) * 100 : 0) : value;
    });
    return out;
  });
}

/** At most `max` rows, chosen by largest-triangle-three-buckets on the rows' totals: the first
 *  and last rows kept, each bucket represented by the row that keeps the curve's shape. */
export function downsample<T extends ChartRow>(rows: T[], max: number): T[] {
  const limit = Math.max(3, Math.trunc(max));
  if (rows.length <= limit) return rows;
  const value = (row: ChartRow) =>
    Object.entries(row).reduce((sum, [key, v]) => (key !== "x" && typeof v === "number" ? sum + v : sum), 0);
  const out: T[] = [rows[0]];
  const bucket = (rows.length - 2) / (limit - 2);
  let previous = 0;
  for (let i = 0; i < limit - 2; i++) {
    const start = Math.floor(i * bucket) + 1;
    const end = Math.min(rows.length - 1, Math.floor((i + 1) * bucket) + 1);
    // The next bucket's average, the third corner of the triangle.
    const nextStart = end;
    const nextEnd = Math.min(rows.length, Math.floor((i + 2) * bucket) + 1);
    let avgX = 0;
    let avgY = 0;
    for (let j = nextStart; j < nextEnd; j++) {
      avgX += j;
      avgY += value(rows[j]);
    }
    const span = Math.max(1, nextEnd - nextStart);
    avgX /= span;
    avgY /= span;
    let best = start;
    let bestArea = -1;
    const px = previous;
    const py = value(rows[previous]);
    for (let j = start; j < end; j++) {
      const area = Math.abs((px - avgX) * (value(rows[j]) - py) - (px - j) * (avgY - py));
      if (area > bestArea) {
        bestArea = area;
        best = j;
      }
    }
    out.push(rows[best]);
    previous = best;
  }
  out.push(rows[rows.length - 1]);
  return out;
}

/** The indexes of the smallest and largest values (the first of each), for `dataValuesDisplay: minmax`. */
export function minMaxIndexes(values: readonly (number | string | null | undefined)[]): Set<number> {
  let min = -1;
  let max = -1;
  values.forEach((v, i) => {
    if (typeof v !== "number") return;
    if (min < 0 || v < (values[min] as number)) min = i;
    if (max < 0 || v > (values[max] as number)) max = i;
  });
  return new Set([min, max].filter((i) => i >= 0));
}

/** Which axis each series is drawn on and whether it is an overlay line. */
export function seriesRoles(names: string[], options: Pick<XYOptions, "overlayFields" | "y2Fields" | "showOverlayY2Axis">) {
  return names.map((name) => {
    const overlay = options.overlayFields.includes(name);
    const y2 = options.y2Fields.includes(name) || (overlay && options.showOverlayY2Axis);
    return { name, overlay, axis: y2 ? ("right" as const) : ("left" as const) };
  });
}

/** A series' colour: by its name, then by its place in `seriesColors`, then the palette. */
export function seriesColor(
  name: string,
  index: number,
  options: { seriesColors: (string | undefined)[]; seriesColorsByField: Record<string, string> },
  palette: (index: number) => string,
): string {
  return (
    (Object.prototype.hasOwnProperty.call(options.seriesColorsByField, name) ? options.seriesColorsByField[name] : undefined) ??
    options.seriesColors[index] ??
    palette(index)
  );
}

/** A legend label cut to `max` characters the way `legendTruncation` asks. */
export function truncateLabel(label: string, max: number, mode: LegendTruncation): string {
  if (mode === "ellipsisOff" || label.length <= max) return label;
  const keep = Math.max(1, max - 1);
  if (mode === "ellipsisStart") return `…${label.slice(label.length - keep)}`;
  if (mode === "ellipsisMiddle") return `${label.slice(0, Math.ceil(keep / 2))}…${label.slice(label.length - Math.floor(keep / 2))}`;
  return `${label.slice(0, keep)}…`;
}

/** Annotations from the annotation source: an x (matched to the chart's) and a label. */
export function annotationMarks(fields: Field[] | undefined, xs: Set<string>): { x: string; label: string }[] {
  if (!fields?.length) return [];
  const [first, second] = fields;
  const marks: { x: string; label: string }[] = [];
  first.values.forEach((value, row) => {
    const x = value === null ? "" : String(value);
    if (!xs.has(x)) return;
    const label = second?.values[row];
    marks.push({ x, label: label === null || label === undefined ? "" : String(label).slice(0, 80) });
  });
  return marks.slice(0, 50);
}

// ── Pie ──────────────────────────────────────────────────────────────────────

export interface Slice {
  label: string;
  value: number;
  /** Its index in the data (its colour), or -1 for the collapsed slice. */
  index: number;
}

/** The slices in data order; those under `threshold` of the total collapse into one last slice
 *  when there are at least two of them. */
export function collapseSlices(items: { label: string; value: number }[], threshold: number, label: string): Slice[] {
  const slices = items.map((item, index) => ({ label: item.label, value: Math.max(0, item.value), index }));
  const total = slices.reduce((sum, s) => sum + s.value, 0);
  if (!total || threshold <= 0) return slices;
  const small = slices.filter((s) => s.value / total < threshold);
  if (small.length < 2) return slices;
  const kept = slices.filter((s) => s.value / total >= threshold);
  return [...kept, { label, value: small.reduce((sum, s) => sum + s.value, 0), index: -1 }];
}

// ── Scatter, bubble, punchcard ───────────────────────────────────────────────

export interface ScatterPoint {
  x: number;
  y: number;
  size?: number;
  category: string;
}

const fieldNamed = (fields: Field[], name: string | undefined) => (name ? fields.find((f) => f.name === name) : undefined);
const isNumeric = (field: Field) => field.values.some((v) => toNumber(v) !== null) && field.values.every((v) => v === null || v === "" || toNumber(v) !== null);

/** The points of a scatter or bubble chart: from rows (the named fields, else the numeric columns
 *  in order), or from series (each series a category, its x read as a number or its position). */
export function scatterPoints(shape: string, fields: Field[], options: Pick<ScatterOptions, "xField" | "yField" | "sizeField" | "categoryField">, withSize: boolean): ScatterPoint[] {
  if (shape === "series") {
    const [x, ...series] = fields;
    if (!x) return [];
    const numericX = isNumeric(x);
    return series.flatMap((s) =>
      s.values.flatMap((value, row) => {
        const y = toNumber(value);
        if (y === null) return [];
        return [{ x: numericX ? (toNumber(x.values[row]) ?? row) : row, y, category: s.name }];
      }),
    );
  }
  const numeric = fields.filter(isNumeric);
  const x = fieldNamed(fields, options.xField) ?? numeric[0];
  const y = fieldNamed(fields, options.yField) ?? numeric.find((f) => f !== x);
  const size = withSize ? (fieldNamed(fields, options.sizeField) ?? numeric.find((f) => f !== x && f !== y)) : undefined;
  const category = fieldNamed(fields, options.categoryField) ?? fields.find((f) => !isNumeric(f));
  if (!x || !y) return [];
  const points: ScatterPoint[] = [];
  x.values.forEach((value, row) => {
    const px = toNumber(value);
    const py = toNumber(y.values[row]);
    if (px === null || py === null) return;
    const point: ScatterPoint = { x: px, y: py, category: category ? String(category.values[row] ?? "") : "" };
    if (size) point.size = Math.max(0, toNumber(size.values[row]) ?? 0);
    points.push(point);
  });
  return points;
}

/** A bubble's radius: `size` placed between `min` and `max` pixels by its share of the largest. */
export function bubbleRadius(size: number | undefined, largest: number, min: number, max: number): number {
  if (size === undefined || largest <= 0) return min;
  return min + (max - min) * Math.sqrt(Math.max(0, size) / largest);
}

export interface Punchcard {
  xs: string[];
  ys: string[];
  cells: { x: string; y: string; size: number }[];
  largest: number;
}

/** A punchcard from rows: two category fields and a size (the named ones, else the first two
 *  text columns and the first numeric one); repeated pairs add up. */
export function punchcard(fields: Field[], options: Pick<ScatterOptions, "xField" | "yField" | "sizeField">): Punchcard {
  const text = fields.filter((f) => !isNumeric(f));
  const numeric = fields.filter(isNumeric);
  const x = fieldNamed(fields, options.xField) ?? text[0];
  const y = fieldNamed(fields, options.yField) ?? text.find((f) => f !== x);
  const size = fieldNamed(fields, options.sizeField) ?? numeric[0];
  if (!x || !y || !size) return { xs: [], ys: [], cells: [], largest: 0 };
  const totals = new Map<string, { x: string; y: string; size: number }>();
  const xs: string[] = [];
  const ys: string[] = [];
  x.values.forEach((value, row) => {
    const cx = String(value ?? "");
    const cy = String(y.values[row] ?? "");
    const n = toNumber(size.values[row]) ?? 0;
    if (!xs.includes(cx)) xs.push(cx);
    if (!ys.includes(cy)) ys.push(cy);
    const key = JSON.stringify([cx, cy]);
    const cell = totals.get(key) ?? { x: cx, y: cy, size: 0 };
    cell.size += Math.max(0, n);
    totals.set(key, cell);
  });
  const cells = [...totals.values()];
  return { xs, ys, cells, largest: Math.max(0, ...cells.map((c) => c.size)) };
}

// ── Single value and gauges ──────────────────────────────────────────────────

/** A trend as a percentage of the value it started from (`major - delta`), or `undefined`. */
export function percentTrend(major: number, delta: number): number | undefined {
  const before = major - delta;
  return before === 0 ? undefined : (delta / Math.abs(before)) * 100;
}

/** Where `value` sits between `min` and `max`, from 0 to 1. */
export function gaugeShare(value: number, min: number, max: number): number {
  if (!(max > min)) return 0;
  return Math.min(1, Math.max(0, (value - min) / (max - min)));
}

/** The ticks of a gauge: `interval` apart from `min`, at most 20; else five even steps. */
export function gaugeTicks(min: number, max: number, interval?: number): number[] {
  if (!(max > min)) return [];
  if (interval && (max - min) / interval <= 20) {
    const ticks: number[] = [];
    for (let t = min; t <= max + interval * 1e-9; t += interval) ticks.push(Number(t.toPrecision(12)));
    return ticks;
  }
  return [0, 1, 2, 3, 4].map((i) => Number((min + ((max - min) * i) / 4).toPrecision(12)));
}

// ── Table ────────────────────────────────────────────────────────────────────

/** The rows of `page` (0-based, clamped) when `count` rows fit on a page, and how many pages there are. */
export function tablePage<T>(rows: T[], page: number, count: number): { rows: T[]; page: number; pages: number } {
  const size = Math.max(1, Math.trunc(count));
  const pages = Math.max(1, Math.ceil(rows.length / size));
  const at = Math.min(pages - 1, Math.max(0, Math.trunc(page)));
  return { rows: rows.slice(at * size, at * size + size), page: at, pages };
}

/** Per column, the sum of its cells when every non-empty one is a number; else `null`. */
export function columnTotals(fields: Field[]): (number | null)[] {
  return fields.map((field) => {
    const filled = field.values.filter((v) => v !== null && v !== "");
    if (filled.length === 0 || !filled.every((v) => toNumber(v) !== null)) return null;
    return filled.reduce<number>((sum, v) => sum + (toNumber(v) ?? 0), 0);
  });
}

/** A cell holding a list ("a, b; c"): its items, at most 20. */
export function cellItems(cell: Cell): string[] {
  if (cell === null) return [];
  return String(cell)
    .split(/[,;\n]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 20);
}

/** A cell holding numbers ("1, 4, 2"): the numbers, at most 100. */
export function cellNumbers(cell: Cell): number[] {
  if (cell === null) return [];
  return String(cell)
    .split(/[\s,;]+/)
    .slice(0, 100)
    .map(toNumber)
    .filter((n): n is number => n !== null);
}
