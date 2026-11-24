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
 * The Dashboard Studio's configuration of one visualization, its
 * configuration panel - what `VisualizationConfig` shows
 * and writes.
 *
 * - The types a visualization can switch to: those drawing the same shape of
 *   data (`typesLike`, the backend's `dashboard_platform.SHAPE_VIEWS`), and
 *   the options kept on a switch (`switchType`): those the new type's options
 *   model takes (`OPTION_KEYS`, `dashboard_visualization_options.OPTIONS_BY_TYPE`;
 *   the server refuses any other).
 * - The fields of its "Data display" and "Coloring" sections, by type
 *   (`displayFields`, `colorFields`), labelled from the label tables below.
 * - A colour set by ranges (the dynamic option `> majorValue | rangeValue(config)` with
 *   the config in the visualization's `context`): `readColorRule`,
 *   `colorRuleOptions`.
 * - The writes: `setVisualization`, `setOptions` (a field emptied is removed),
 *   `applyOperations` (the server's `set` and `delete` operations, applied to
 *   a definition), `addChart` (a visualization on sample data written in the
 *   dashboard, as test data) and `addInput`.
 * - Where it sits (`placementOf`) and its place set from the Position fields
 *   (`setPlacement`: cells of the grid, pixels of a canvas).
 *
 * Pure, relative imports only: `viz-config.test.ts` runs it on `node --test`.
 */
import { placeBlocks, setBox } from "./canvas.ts";
import { addInputOperations, addVisualizationOperations, freeId, TYPE_TO_VIEW } from "./definition.ts";

type Raw = Record<string, unknown>;
const isRecord = (value: unknown): value is Raw => typeof value === "object" && value !== null && !Array.isArray(value);
const record = (value: unknown): Raw => (isRecord(value) ? value : {});

// ── Types ────────────────────────────────────────────────────────────────────

/** The views each shape of data can be drawn with (`dashboard_platform.SHAPE_VIEWS`, and the text shape). */
const SHAPE_VIEWS: Record<string, readonly string[]> = {
  number: ["number", "icon", "radial", "filler", "marker"],
  series: ["line", "area", "bar", "scatter", "heatmap", "radar"],
  distribution: ["distribution", "donut", "hbar", "bar", "treemap", "radar", "polar", "wordcloud"],
  rows: ["list", "table", "scatter", "bubble", "punchcard", "timeline", "linkgraph", "networkgraph", "sankey", "parallel"],
  geo: ["map", "choroplethsvg"],
  graph: ["networkgraph", "linkgraph", "sankey"],
  events: ["timeline"],
  tree: ["treemap"],
  outcomes: ["outcomematrix", "pathgraph", "exposureconsole", "scoregauge", "ringgauge", "domaincards"],
  text: ["text"],
};

/** How each data visualization type is called, in the order the studio lists them. @i18n-keys */
export const TYPE_LABELS = {
  "xtm.singlevalue": "Single value",
  "xtm.singlevalueicon": "Single value with icon",
  "xtm.singlevalueradial": "Single value radial",
  "xtm.fillergauge": "Filler gauge",
  "xtm.markergauge": "Marker gauge",
  "xtm.line": "Line chart",
  "xtm.area": "Area chart",
  "xtm.column": "Column chart",
  "xtm.bar": "Bar chart",
  "xtm.pie": "Pie chart",
  "xtm.distribution": "Ranked bars",
  "xtm.radar": "Radar chart",
  "xtm.polararea": "Polar area chart",
  "xtm.wordcloud": "Word cloud",
  "xtm.heatmap": "Heatmap",
  "xtm.outcomematrix": "Technique matrix",
  "xtm.pathgraph": "Path graph",
  "xtm.exposureconsole": "Exposure console",
  "xtm.scoregauge": "Score gauge",
  "xtm.ringgauge": "Ring gauge",
  "xtm.domaincards": "Domain cards",
  "xtm.scatter": "Scatter chart",
  "xtm.bubble": "Bubble chart",
  "xtm.punchcard": "Punchcard",
  "xtm.table": "Table",
  "xtm.list": "List",
  "xtm.map": "Map",
  "xtm.choropleth.svg": "Choropleth SVG",
  "xtm.linkgraph": "Link graph",
  "xtm.networkgraph": "Network graph",
  "xtm.sankey": "Sankey diagram",
  "xtm.parallelcoordinates": "Parallel coordinates",
  "xtm.timeline": "Timeline",
  "xtm.treemap": "Treemap",
  "xtm.answer": "Answer in words",
} as const;
export type VizType = keyof typeof TYPE_LABELS;
const VIZ_TYPES = Object.keys(TYPE_LABELS) as VizType[];

export const isVizType = (type: unknown): type is VizType => typeof type === "string" && type in TYPE_LABELS;

/** The shapes a type's view draws. */
function shapesOf(type: string): string[] {
  const view = TYPE_TO_VIEW[type];
  return view ? Object.keys(SHAPE_VIEWS).filter((shape) => SHAPE_VIEWS[shape].includes(view)) : [];
}

/** The types drawing a shape `type` draws, `type` among them, in the studio's order. */
export function typesLike(type: string): VizType[] {
  const views = new Set(shapesOf(type).flatMap((shape) => SHAPE_VIEWS[shape]));
  return VIZ_TYPES.filter((candidate) => views.has(TYPE_TO_VIEW[candidate] ?? ""));
}

/** The types a source answering in `shape` can be drawn with. */
export function typesForShape(shape: string): VizType[] {
  const views = new Set(SHAPE_VIEWS[shape] ?? []);
  return VIZ_TYPES.filter((candidate) => views.has(TYPE_TO_VIEW[candidate] ?? ""));
}

// ── Options each type takes (`OPTIONS_BY_TYPE`) ──────────────────────────────

const BASE = ["link", "trellis"];
const AXES = ["xAxisTitleText", "yAxisTitleText", "xAxisTitleVisibility", "yAxisTitleVisibility", "yAxisScale", "yAxisMin", "yAxisMax", "xAxisLabelRotation"];
const PALETTE = ["seriesColors", "seriesColorsByField"];
const SINGLE = [
  "majorValue", "trendValue", "sparklineValues", "trendDisplay", "sparklineDisplay", "showSparklineAreaGraph", "unit",
  "unitPosition", "numberPrecision", "shouldUseThousandSeparators", "shouldAbbreviateValue", "shouldAbbreviateTrendValue",
  "underLabel", "majorColor", "trendColor", "backgroundColor", "sparklineStrokeColor",
]; // prettier-ignore
const XY = [
  "stackMode", "overlayFields", "y2Fields", "showOverlayY2Axis", "y2AxisTitleText", "y2AxisScale", "y2AxisMin", "y2AxisMax",
  "dataValuesDisplay", "legendDisplay", "legendTruncation", "nullValueDisplay", "pointColors", "markerDisplay", "lineWidth",
  "areaOpacity", "annotationColor", "showBrush", "resultLimit",
]; // prettier-ignore
const GAUGE = ["value", "minValue", "maxValue", "gaugeRanges", "orientation", "gaugeColor", "labelDisplay", "valueDisplay", "unit", "numberPrecision", "majorTickInterval"];

/** A chart's look (`charts/look.ts`, the backend's `_Look`): a module's preset, or a person's choice. */
const LOOK = [
  "palette", "paletteLead", "paletteNoPale", "numberFormat", "tooltipContent", "separatorWidth", "openLinks", "emptySample",
  "fill", "fillOpacityFrom", "fillOpacityTo", "barRadius", "barSize", "barGap", "colorByPoint", "stackTotals", "dateFormat",
  "xAxisType", "markerSize", "labelMaxLength", "labelCut", "gridLines", "gridOpacity", "xAxisFontSize", "yAxisFontSize",
  "axisLabelColor", "hideAxisLines", "valueTickCount", "dataValuesStrong", "radius", "donutHoleSize", "colorRanges", "valueShades",
]; // prettier-ignore

/** The maps, graphs and flows (`STUDIO_VIZ_TYPES`): their options are kept as written. */
const STUDIO_TYPES = ["xtm.map", "xtm.choropleth.svg", "xtm.linkgraph", "xtm.networkgraph", "xtm.sankey", "xtm.parallelcoordinates", "xtm.timeline", "xtm.treemap"];

type Group = "plain" | "xy" | "pie" | "polar" | "radar" | "wordcloud" | "heatmap" | "scatter" | "single" | "icon" | "radial" | "gauge" | "table" | "outcome" | "studio";

/** The options each model takes; `studio` takes any (`StudioVizOptions`). */
const GROUP_KEYS: Record<Exclude<Group, "studio">, readonly string[]> = {
  plain: BASE,
  xy: [...BASE, ...AXES, ...PALETTE, ...XY, ...LOOK],
  pie: [...BASE, ...PALETTE, "collapseThreshold", "collapseLabel", "showDonutHole", "labelDisplay", "legendDisplay", "legendTruncation", "areaOpacity", ...LOOK],
  // A polar area takes a pie's options (`PieOptions`); it has no hole.
  polar: [...BASE, ...PALETTE, "collapseThreshold", "collapseLabel", "showDonutHole", "labelDisplay", "legendDisplay", "legendTruncation", "areaOpacity", ...LOOK],
  radar: [...BASE, ...PALETTE, "areaOpacity", "lineWidth", "legendDisplay", "legendTruncation", ...LOOK],
  wordcloud: [...BASE, ...PALETTE, "fontSizeMin", "fontSizeMax", ...LOOK],
  heatmap: [...BASE, "colorMin", "colorMax", "dataValuesDisplay", "legendDisplay", ...LOOK],
  scatter: [...BASE, ...AXES, ...PALETTE, "xField", "yField", "sizeField", "categoryField", "legendDisplay", "bubbleSizeMin", "bubbleSizeMax"],
  single: [...BASE, ...SINGLE],
  icon: [...BASE, ...SINGLE, "icon", "iconColor", "iconPosition", "showValue"],
  radial: [...BASE, ...SINGLE, "maxValue", "radialStrokeColor", "radialBackgroundColor"],
  gauge: [...BASE, ...GAUGE],
  table: [...BASE, "count", "headerVisibility", "showRowNumbers", "showTotals", "columnFormat", "tableFormat"],
  outcome: [...BASE, "sampleDisplay"],
};

/** The validation views (`OUTCOME_VIZ_TYPES`). */
const OUTCOME_TYPES = ["xtm.outcomematrix", "xtm.pathgraph", "xtm.exposureconsole", "xtm.scoregauge", "xtm.ringgauge", "xtm.domaincards"];

/** Each type's options model (`OPTIONS_BY_TYPE`). */
function groupOf(type: string): Group {
  switch (type) {
    case "xtm.line":
    case "xtm.area":
    case "xtm.column":
    case "xtm.bar":
      return "xy";
    case "xtm.pie":
      return "pie";
    case "xtm.polararea":
      return "polar";
    case "xtm.radar":
      return "radar";
    case "xtm.wordcloud":
      return "wordcloud";
    case "xtm.heatmap":
      return "heatmap";
    case "xtm.scatter":
    case "xtm.bubble":
    case "xtm.punchcard":
      return "scatter";
    case "xtm.singlevalue":
      return "single";
    case "xtm.singlevalueicon":
      return "icon";
    case "xtm.singlevalueradial":
      return "radial";
    case "xtm.fillergauge":
    case "xtm.markergauge":
      return "gauge";
    case "xtm.table":
      return "table";
    default:
      if (OUTCOME_TYPES.includes(type)) return "outcome";
      return STUDIO_TYPES.includes(type) ? "studio" : "plain";
  }
}

/** The options a type takes, or `null` when it takes any. */
function optionKeys(type: string): readonly string[] | null {
  const group = groupOf(type);
  return group === "studio" ? null : GROUP_KEYS[group];
}

/** The options kept when a visualization drawn as `from` is drawn as `to`: those `to` takes. */
export function switchType(options: Raw, to: string): Raw {
  const keys = optionKeys(to);
  if (keys === null) return { ...options };
  return Object.fromEntries(Object.entries(options).filter(([key]) => keys.includes(key)));
}

// ── Fields ───────────────────────────────────────────────────────────────────

/** Each field's label. @i18n-keys */
export const FIELD_LABELS = {
  stackMode: "Stack mode",
  legendDisplay: "Legend",
  dataValuesDisplay: "Data values",
  nullValueDisplay: "Missing values",
  markerDisplay: "Markers",
  lineWidth: "Line width",
  areaOpacity: "Area opacity",
  fontSizeMin: "Smallest word",
  fontSizeMax: "Largest word",
  colorMin: "Colour of the lowest value",
  colorMax: "Colour of the highest value",
  showBrush: "Brush to select a range",
  xAxisTitleText: "X-axis title",
  yAxisTitleText: "Y-axis title",
  yAxisScale: "Y-axis scale",
  yAxisMin: "Y-axis minimum",
  yAxisMax: "Y-axis maximum",
  showDonutHole: "Donut hole",
  labelDisplay: "Labels",
  collapseThreshold: "Collapse slices under",
  collapseLabel: "Collapsed slices label",
  xField: "X field",
  yField: "Y field",
  sizeField: "Size field",
  categoryField: "Category field",
  bubbleSizeMin: "Smallest bubble",
  bubbleSizeMax: "Largest bubble",
  unit: "Unit",
  unitPosition: "Unit position",
  numberPrecision: "Decimal places",
  shouldUseThousandSeparators: "Thousand separators",
  shouldAbbreviateValue: "Abbreviate the value",
  underLabel: "Label under the value",
  trendDisplay: "Trend",
  sparklineDisplay: "Sparkline",
  showSparklineAreaGraph: "Sparkline as an area",
  icon: "Lucide icon",
  iconPosition: "Icon position",
  showValue: "Show the value",
  maxValue: "Maximum",
  minValue: "Minimum",
  orientation: "Orientation",
  valueDisplay: "Value",
  majorTickInterval: "Tick interval",
  count: "Rows per page",
  headerVisibility: "Header",
  showRowNumbers: "Row numbers",
  showTotals: "Totals row",
  seriesColors: "Series colours",
  annotationColor: "Annotation colour",
  majorColor: "Value colour",
  trendColor: "Trend colour",
  backgroundColor: "Background colour",
  sparklineStrokeColor: "Sparkline colour",
  iconColor: "Icon colour",
  radialStrokeColor: "Radial colour",
  radialBackgroundColor: "Radial track colour",
  gaugeColor: "Gauge colour",
  gaugeRanges: "Gauge ranges",
  splitBy: "Split into panels by",
  columns: "Panels per row",
  sharedScale: "Same scale in every panel",
  sort: "Panel order",
  sampleDisplay: "Sample while there is no data",
} as const;
type FieldLabel = keyof typeof FIELD_LABELS;

/** The choices of the fields that have some. @i18n-keys */
export const CHOICE_LABELS = {
  auto: "Automatic",
  stacked: "Stacked",
  stacked100: "Stacked to 100%",
  right: "Right",
  left: "Left",
  top: "Top",
  bottom: "Bottom",
  off: "Off",
  all: "All",
  minmax: "Minimum and maximum",
  gaps: "Gaps",
  zero: "Zero",
  connect: "Connect",
  filled: "Filled",
  outlined: "Outlined",
  linear: "Linear",
  log: "Logarithmic",
  values: "Values",
  valuesAndPercentage: "Values and percentages",
  before: "Before",
  after: "After",
  above: "Above",
  below: "Below",
  absolute: "Absolute",
  percent: "Percent",
  number: "Number",
  percentage: "Percentage",
  horizontal: "Horizontal",
  vertical: "Vertical",
  inline: "Scrolls with the rows",
  fixed: "Fixed",
  none: "None",
  name: "By name",
  "-name": "By name, descending",
  total: "By total",
  "-total": "By total, descending",
  full: "Greyed out",
  subtle: "Desaturated",
} as const;
type Choice = keyof typeof CHOICE_LABELS;

export type FieldSpec =
  | { kind: "select"; key: FieldLabel; choices: readonly Choice[] }
  | { kind: "number"; key: FieldLabel; min?: number; max?: number; step?: number }
  | { kind: "switch"; key: FieldLabel; fallback: boolean }
  | { kind: "text"; key: FieldLabel; max: number }
  /** A colour, static. */
  | { kind: "color"; key: FieldLabel }
  /** A colour, static or by ranges of the option `value` reads (`readColorRule`). */
  | { kind: "colorRule"; key: FieldLabel; value: string }
  /** A list of colours (`seriesColors`). */
  | { kind: "colors"; key: FieldLabel }
  /** A gauge's ranges: from, to and colour. */
  | { kind: "ranges"; key: FieldLabel };

const LEGEND: readonly Choice[] = ["right", "left", "top", "bottom", "off"];
const AXIS_FIELDS: FieldSpec[] = [
  { kind: "text", key: "xAxisTitleText", max: 80 },
  { kind: "text", key: "yAxisTitleText", max: 80 },
  { kind: "select", key: "yAxisScale", choices: ["linear", "log"] },
  { kind: "number", key: "yAxisMin" },
  { kind: "number", key: "yAxisMax" },
];
const SINGLE_FIELDS: FieldSpec[] = [
  { kind: "text", key: "unit", max: 12 },
  { kind: "select", key: "unitPosition", choices: ["before", "after"] },
  { kind: "number", key: "numberPrecision", min: 0, max: 6, step: 1 },
  { kind: "switch", key: "shouldUseThousandSeparators", fallback: true },
  { kind: "switch", key: "shouldAbbreviateValue", fallback: true },
  { kind: "text", key: "underLabel", max: 80 },
  { kind: "select", key: "trendDisplay", choices: ["absolute", "percent", "off"] },
  { kind: "select", key: "sparklineDisplay", choices: ["below", "before", "after", "off"] },
  { kind: "switch", key: "showSparklineAreaGraph", fallback: false },
];

const DISPLAY: Record<Group, FieldSpec[]> = {
  plain: [],
  studio: [],
  xy: [
    { kind: "select", key: "stackMode", choices: ["auto", "stacked", "stacked100"] },
    { kind: "select", key: "legendDisplay", choices: LEGEND },
    { kind: "select", key: "dataValuesDisplay", choices: ["off", "all", "minmax"] },
    { kind: "select", key: "nullValueDisplay", choices: ["gaps", "zero", "connect"] },
    { kind: "select", key: "markerDisplay", choices: ["off", "filled", "outlined"] },
    { kind: "number", key: "lineWidth", min: 1, max: 6, step: 0.5 },
    { kind: "number", key: "areaOpacity", min: 0, max: 1, step: 0.05 },
    { kind: "switch", key: "showBrush", fallback: false },
    ...AXIS_FIELDS,
  ],
  pie: [
    { kind: "switch", key: "showDonutHole", fallback: true },
    { kind: "select", key: "labelDisplay", choices: ["off", "values", "valuesAndPercentage"] },
    { kind: "select", key: "legendDisplay", choices: LEGEND },
    { kind: "number", key: "collapseThreshold", min: 0, max: 0.5, step: 0.01 },
    { kind: "text", key: "collapseLabel", max: 40 },
  ],
  polar: [
    { kind: "select", key: "labelDisplay", choices: ["off", "values", "valuesAndPercentage"] },
    { kind: "select", key: "legendDisplay", choices: LEGEND },
    { kind: "number", key: "collapseThreshold", min: 0, max: 0.5, step: 0.01 },
    { kind: "text", key: "collapseLabel", max: 40 },
  ],
  radar: [
    { kind: "select", key: "legendDisplay", choices: LEGEND },
    { kind: "number", key: "areaOpacity", min: 0, max: 1, step: 0.05 },
  ],
  wordcloud: [
    { kind: "number", key: "fontSizeMin", min: 8, max: 32, step: 1 },
    { kind: "number", key: "fontSizeMax", min: 12, max: 96, step: 1 },
  ],
  heatmap: [{ kind: "select", key: "dataValuesDisplay", choices: ["off", "all"] }],
  scatter: [
    { kind: "text", key: "xField", max: 100 },
    { kind: "text", key: "yField", max: 100 },
    { kind: "text", key: "sizeField", max: 100 },
    { kind: "text", key: "categoryField", max: 100 },
    { kind: "select", key: "legendDisplay", choices: LEGEND },
    { kind: "number", key: "bubbleSizeMin", min: 2, max: 40, step: 1 },
    { kind: "number", key: "bubbleSizeMax", min: 2, max: 60, step: 1 },
    ...AXIS_FIELDS,
  ],
  single: SINGLE_FIELDS,
  icon: [
    ...SINGLE_FIELDS,
    { kind: "text", key: "icon", max: 60 },
    { kind: "select", key: "iconPosition", choices: ["before", "after", "above"] },
    { kind: "switch", key: "showValue", fallback: true },
  ],
  radial: [...SINGLE_FIELDS, { kind: "number", key: "maxValue" }],
  gauge: [
    { kind: "number", key: "minValue" },
    { kind: "number", key: "maxValue" },
    { kind: "select", key: "orientation", choices: ["horizontal", "vertical"] },
    { kind: "select", key: "labelDisplay", choices: ["number", "percentage", "off"] },
    { kind: "select", key: "valueDisplay", choices: ["number", "percentage", "off"] },
    { kind: "text", key: "unit", max: 12 },
    { kind: "number", key: "numberPrecision", min: 0, max: 6, step: 1 },
    { kind: "number", key: "majorTickInterval", min: 0 },
  ],
  table: [
    { kind: "number", key: "count", min: 1, max: 100, step: 1 },
    { kind: "select", key: "headerVisibility", choices: ["inline", "fixed", "none"] },
    { kind: "switch", key: "showRowNumbers", fallback: false },
    { kind: "switch", key: "showTotals", fallback: false },
  ],
  outcome: [{ kind: "select", key: "sampleDisplay", choices: ["full", "subtle", "off"] }],
};

const SINGLE_COLORS: FieldSpec[] = [
  { kind: "colorRule", key: "majorColor", value: "majorValue" },
  { kind: "colorRule", key: "backgroundColor", value: "majorValue" },
  { kind: "color", key: "trendColor" },
  { kind: "color", key: "sparklineStrokeColor" },
];

const COLORS: Record<Group, FieldSpec[]> = {
  plain: [],
  studio: [],
  table: [],
  outcome: [],
  xy: [{ kind: "colors", key: "seriesColors" }, { kind: "color", key: "annotationColor" }],
  pie: [{ kind: "colors", key: "seriesColors" }],
  polar: [{ kind: "colors", key: "seriesColors" }],
  radar: [{ kind: "colors", key: "seriesColors" }],
  wordcloud: [{ kind: "colors", key: "seriesColors" }],
  heatmap: [{ kind: "color", key: "colorMin" }, { kind: "color", key: "colorMax" }],
  scatter: [{ kind: "colors", key: "seriesColors" }],
  single: SINGLE_COLORS,
  icon: [...SINGLE_COLORS, { kind: "colorRule", key: "iconColor", value: "majorValue" }],
  radial: [...SINGLE_COLORS, { kind: "colorRule", key: "radialStrokeColor", value: "majorValue" }, { kind: "color", key: "radialBackgroundColor" }],
  gauge: [{ kind: "colorRule", key: "gaugeColor", value: "value" }, { kind: "ranges", key: "gaugeRanges" }],
};

/** The "Data display" fields of a type. */
export const displayFields = (type: string): FieldSpec[] => DISPLAY[groupOf(type)];
/** The "Coloring" fields of a type. */
export const colorFields = (type: string): FieldSpec[] => COLORS[groupOf(type)];

/** The types a trellis splits into panels (`viz/route.TRELLIS_VIEWS`). */
const TRELLIS_TYPES = ["xtm.column", "xtm.bar", "xtm.line", "xtm.area", "xtm.pie", "xtm.distribution"];
export const takesTrellis = (type: string): boolean => TRELLIS_TYPES.includes(type);

// ── Colours by ranges ────────────────────────────────────────────────────────

export interface ColorRange {
  from?: number;
  to?: number;
  value: string;
}

export type ColorRule =
  | { mode: "theme" }
  | { mode: "static"; color: string }
  | { mode: "ranges"; ranges: ColorRange[] }
  /** An expression the panel does not write: edited in the source. */
  | { mode: "custom"; expression: string };

const RANGE_RULE = /^>\s*([A-Za-z_][A-Za-z0-9_]*)\s*\|\s*rangeValue\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*$/;

function readRanges(value: unknown): ColorRange[] | null {
  if (!Array.isArray(value)) return null;
  const out: ColorRange[] = [];
  for (const entry of value.slice(0, 20)) {
    const rule = record(entry);
    if (typeof rule.value !== "string") return null;
    out.push({
      ...(typeof rule.from === "number" ? { from: rule.from } : {}),
      ...(typeof rule.to === "number" ? { to: rule.to } : {}),
      value: rule.value,
    });
  }
  return out;
}

/** The context entry holding the ranges of the colour option `key` (`<key>EditorConfig`). */
const rangesKey = (key: string): string => `${key}EditorConfig`;

/** How the colour option `key` is set: not at all (the theme's), one colour, ranges of a value, or another expression. */
export function readColorRule(options: Raw, context: Raw, key: string): ColorRule {
  const value = options[key];
  if (typeof value !== "string" || !value.trim()) return { mode: "theme" };
  if (!value.trimStart().startsWith(">")) return { mode: "static", color: value };
  const match = RANGE_RULE.exec(value);
  const ranges = match ? readRanges(context[match[2]]) : null;
  return ranges ? { mode: "ranges", ranges } : { mode: "custom", expression: value };
}

/** The option and the context entry writing `rule` for the colour option `key` read on the option `of` (`undefined`: removed). */
function colorRuleOptions(key: string, of: string, rule: ColorRule): { option: string | undefined; context: ColorRange[] | undefined } {
  switch (rule.mode) {
    case "theme":
      return { option: undefined, context: undefined };
    case "static":
      return { option: rule.color, context: undefined };
    case "ranges":
      return { option: `> ${of} | rangeValue(${rangesKey(key)})`, context: rule.ranges.slice(0, 20) };
    case "custom":
      return { option: rule.expression, context: undefined };
  }
}

/** Ranges to start from: below, between and above two thresholds of the value. */
export const STARTER_RANGES: ColorRange[] = [
  { to: 50, value: "#4caf50" },
  { from: 50, to: 80, value: "#ff9800" },
  { from: 80, value: "#f44336" },
];

// ── Writes ───────────────────────────────────────────────────────────────────

const withoutUndefined = (value: Raw): Raw => Object.fromEntries(Object.entries(value).filter(([, v]) => v !== undefined));

/** The visualization `vid` with `patch` merged (a key set to `undefined` is removed). */
export function setVisualization(definition: Raw, vid: string, patch: Raw): Raw {
  const visualizations = record(definition.visualizations);
  if (!isRecord(visualizations[vid])) return definition;
  return { ...definition, visualizations: { ...visualizations, [vid]: withoutUndefined({ ...visualizations[vid], ...patch }) } };
}

/** The visualization `vid` with `patch` merged into its options (a key set to `undefined` or `""` is removed). */
export function setOptions(definition: Raw, vid: string, patch: Raw): Raw {
  const viz = record(record(definition.visualizations)[vid]);
  const options = { ...record(viz.options) };
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined || value === "") delete options[key];
    else options[key] = value;
  }
  return setVisualization(definition, vid, { options });
}

/** The visualization `vid` with its `context` entry `key` set, or removed with `undefined`. */
function setContext(definition: Raw, vid: string, key: string, value: unknown): Raw {
  const viz = record(record(definition.visualizations)[vid]);
  const context = { ...record(viz.context) };
  if (value === undefined) delete context[key];
  else context[key] = value;
  return setVisualization(definition, vid, { context });
}

/** The visualization `vid` drawn as `type`, keeping the options `type` takes. */
export function setType(definition: Raw, vid: string, type: string): Raw {
  const viz = record(record(definition.visualizations)[vid]);
  return setVisualization(definition, vid, { type, options: switchType(record(viz.options), type) });
}

/** The colour option `key` (read on `of`) of `vid` set by `rule`: the option and its ranges in `context`. */
export function setColorRule(definition: Raw, vid: string, key: string, of: string, rule: ColorRule): Raw {
  const written = colorRuleOptions(key, of, rule);
  return setContext(setOptions(definition, vid, { [key]: written.option }), vid, rangesKey(key), written.context);
}

/** A whole-element path of the server's `set` and `delete` operations (`dashboard_definition._SET_PATH_RE`). */
const PATH_RE = /^(?:(visualizations|dataSources|inputs)\.([A-Za-z0-9][A-Za-z0-9_.-]{0,59})|(layout|defaults|expressions|applicationProperties|title|description|theme))$/;

export interface PathOperation {
  op: "set" | "delete";
  path: string;
  value?: unknown;
}

/** `operations` applied to `definition` as the server applies them; one it would refuse leaves the definition as it was. */
export function applyOperations(definition: Raw, operations: readonly PathOperation[]): Raw {
  let out: Raw = { ...definition };
  for (const operation of operations) {
    const match = PATH_RE.exec(operation.path);
    if (!match) return definition;
    if (match[1]) {
      const group = { ...record(out[match[1]]) };
      if (operation.op === "set") {
        if (!isRecord(operation.value)) return definition;
        group[match[2]] = operation.value;
      } else delete group[match[2]];
      out = { ...out, [match[1]]: group };
    } else if (operation.op === "set") out = { ...out, [match[3]]: operation.value };
    else {
      out = { ...out };
      delete out[match[3]];
    }
  }
  return out;
}

/** The sample data a new chart of each shape draws until it is pointed at a real source. */
const SAMPLES: Record<string, unknown> = {
  number: { value: 42, change: 6 },
  series: {
    series: [
      {
        name: "Sample",
        points: [
          { x: "2026-10-01", y: 12 },
          { x: "2026-10-02", y: 18 },
          { x: "2026-10-03", y: 9 },
          { x: "2026-10-04", y: 22 },
          { x: "2026-10-05", y: 16 },
        ],
      },
    ],
  },
  distribution: {
    items: [
      { label: "High", value: 14 },
      { label: "Medium", value: 11 },
      { label: "Low", value: 6 },
    ],
  },
  rows: {
    columns: [
      { key: "name", label: "Name" },
      { key: "count", label: "Count" },
    ],
    rows: [
      { name: "Alpha", count: 12 },
      { name: "Beta", count: 7 },
      { name: "Gamma", count: 3 },
    ],
  },
  text: { text: "An answer in words." },
  outcomes: {
    items: [
      { label: "Prevention", success: 41, failed: 13, pending: 4, parts: [] },
      { label: "Detection", success: 28, failed: 17, pending: 6, parts: [] },
      { label: "Response", success: 9, failed: 3, pending: 1, parts: [] },
    ],
  },
};

/** The shape a new chart of `type` draws its sample in: the first of its shapes with a sample. */
function sampleShape(type: string): string | undefined {
  return shapesOf(type).find((shape) => shape in SAMPLES);
}

/** The types the toolbar adds (those a sample can draw). */
export const ADDABLE_TYPES: VizType[] = VIZ_TYPES.filter((type) => sampleShape(type) !== undefined);

/**
 * `definition` with a chart of `type` on sample data written in the dashboard
 * (a static source of its own, as test data), below the others in the
 * layout `layoutId`; or `null` when no sample suits the type.
 */
export function addChart(definition: Raw, type: string, layoutId: string | null, sourceName: string): { definition: Raw; id: string } | null {
  const shape = sampleShape(type);
  if (!shape) return null;
  const sid = freeId(definition, "ds_sample");
  const withSource = applyOperations(definition, [
    { op: "set", path: `dataSources.${sid}`, value: { type: "static", name: sourceName.slice(0, 80), options: { shape, data: SAMPLES[shape] } } },
  ]);
  const added = addVisualizationOperations(withSource, sid, type, "", layoutId);
  return { definition: applyOperations(withSource, added.operations), id: added.id };
}

/** The inputs the toolbar adds. */
export const INPUT_TYPES = ["dropdown", "multiselect", "text", "number", "timerange", "button"] as const;
export type AddableInput = (typeof INPUT_TYPES)[number];

/** `definition` with a new input of `kind` above the tabs, setting a token of its own. */
export function addInput(definition: Raw, kind: AddableInput, title: string): { definition: Raw; id: string } {
  const id = freeId(definition, `input_${kind}`);
  const input: Raw = { type: `input.${kind}`, title: title.slice(0, 120) };
  const token = id.replace(/[^A-Za-z0-9_]/g, "_").slice(0, 60);
  // A button runs one action at least: it sets a token of its own until it is given others.
  if (kind === "button") input.options = { label: title.slice(0, 60), actions: [{ type: "setToken", token, value: "clicked" }] };
  else {
    input.token = token;
    if (kind === "dropdown" || kind === "multiselect") input.options = { items: [] };
    if (kind === "timerange") input.defaultValue = "30d";
  }
  return { definition: applyOperations(definition, addInputOperations(definition, id, input)), id };
}

// ── Position ─────────────────────────────────────────────────────────────────

/** The grid's columns and tallest block (`dashboard-grid/layout.ts`). */
const GRID_COLUMNS = 12;
const MAX_GRID_ROWS = 40;

export interface Placement {
  layoutId: string;
  /** A grid places in cells, a canvas in pixels. */
  type: "grid" | "absolute";
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Where the visualization `vid` sits: the first layout holding it as a block, `preferred` first. */
export function placementOf(definition: Raw, vid: string, preferred?: string | null): Placement | null {
  const layouts = record(record(definition.layout).layoutDefinitions);
  const ids = Object.keys(layouts);
  for (const layoutId of preferred && ids.includes(preferred) ? [preferred, ...ids.filter((id) => id !== preferred)] : ids) {
    const layout = record(layouts[layoutId]);
    for (const entry of Array.isArray(layout.structure) ? layout.structure : []) {
      const item = record(entry);
      const position = record(item.position);
      if (item.item !== vid || (item.type ?? "block") !== "block") continue;
      const [x, y, w, h] = [position.x, position.y, position.w, position.h];
      if (typeof x === "number" && typeof y === "number" && typeof w === "number" && typeof h === "number") {
        return { layoutId, type: layout.type === "absolute" ? "absolute" : "grid", x, y, w, h };
      }
    }
  }
  return null;
}

/** The visualization placed at `box`: kept on the grid's columns, or on the canvas. */
export function setPlacement(definition: Raw, vid: string, placement: Placement): Raw {
  const round = (n: number) => Math.round(n);
  if (placement.type === "absolute") {
    return setBox(definition, placement.layoutId, vid, { x: round(placement.x), y: round(placement.y), w: round(placement.w), h: round(placement.h) });
  }
  const w = Math.min(GRID_COLUMNS, Math.max(1, round(placement.w)));
  const x = Math.min(GRID_COLUMNS - w, Math.max(0, round(placement.x)));
  const h = Math.min(MAX_GRID_ROWS, Math.max(1, round(placement.h)));
  return placeBlocks(definition, placement.layoutId, [{ id: vid, x, y: Math.max(0, round(placement.y)), w, h }]);
}
