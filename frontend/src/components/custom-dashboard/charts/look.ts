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
 * The look of a chart, as generic options: what a module's own widget looks
 * like, said in words any visualization takes (`palette`, `separatorWidth`,
 * `barRadius`, `fill: "gradient"`, `numberFormat`, `dateFormat`...).
 *
 * A module's adapter sends them as its presentation preset; a person may set
 * them too (`dashboard_visualization_options.py` checks them). Every option is
 * optional and, when absent, the renderer keeps XTM One's own look - so a
 * widget drawn with another view than its module's keeps it. Read
 * defensively, like every option (`chart-options.ts`): an enum against its
 * list, a number clamped, a colour resolved.
 *
 * Also here: the data a chart draws besides its values (an item's or a
 * series' colour, link and key), and the sample a widget draws, greyed, while
 * it has nothing to show.
 *
 * Pure, relative imports only: runs on `node --test`.
 */
import { resolveColor } from "./colors.ts";
import { seriesXOrder } from "./dynamic-options.ts";
import { axisDateOptions, formatNamedDate, formatNamedNumber, NUMBER_FORMATS, type NumberFormatName } from "./formats.ts";
import { isPale, paletteColors, themedColor, type ColorMode } from "./palettes.ts";

type Raw = Record<string, unknown>;
const isRecord = (value: unknown): value is Raw => typeof value === "object" && value !== null && !Array.isArray(value);
const oneOf = <T extends string>(value: unknown, allowed: readonly T[]): T | undefined =>
  (allowed as readonly unknown[]).includes(value) ? (value as T) : undefined;
const num = (value: unknown, min: number, max: number): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : undefined;
const bool = (value: unknown): boolean => value === true;

const LOOK_PALETTES = ["material", "materialMixed", "results"] as const;
type LookPalette = (typeof LOOK_PALETTES)[number];
/** How the x values that are dates are written: `1 Jan 2026`, `1 January 2026`, `January 2026`, `2026`. */
const LOOK_DATE_FORMATS = ["date", "longDate", "monthYear", "year"] as const;
type LookDateFormat = (typeof LOOK_DATE_FORMATS)[number];
const SAMPLES = ["status", "platforms", "ttp", "temporal", "radar"] as const;
export type SampleName = (typeof SAMPLES)[number];
const GRID_LINES = ["both", "horizontal", "vertical", "none"] as const;
type GridLines = (typeof GRID_LINES)[number];

export interface Look {
  /** The named palette series, slices and bars take, in the theme's tint (`palettes.paletteColors`). */
  palette?: LookPalette;
  /** A colour drawn before the palette's (the first series, the first bar). */
  paletteLead?: string;
  /** Leave out the palette's colours too close to white. */
  paletteNoPale: boolean;
  /** How values are written (axes, tooltips, labels); XTM One's compact form when unset. */
  numberFormat?: NumberFormatName;
  /** A tooltip with the label alone, or the label and the value. */
  tooltipContent: "labelValue" | "label";
  /** The line, in the surface colour, between slices, cells or bars (px). */
  separatorWidth?: number;
  /** A click on a value that has a link opens it, when the dashboard reacts to nothing. */
  openLinks: boolean;
  /** What to draw, greyed and marked "Sample", when there is nothing to show. */
  emptySample?: SampleName;
  /** Areas and bars: a solid fill, or a gradient from one opacity to another. */
  fill: "solid" | "gradient";
  fillOpacityFrom: number;
  fillOpacityTo: number;
  /** A bar's rounded end (px). */
  barRadius?: number;
  /** A bar's thickness, as a share of its band (%). */
  barSize?: number;
  /** The gap between the bars of one group (px). */
  barGap?: number;
  /** One colour per bar of a single series, as a palette goes. */
  colorByPoint: boolean;
  /** Totals above stacked bars. */
  stackTotals: boolean;
  /** The x values that are dates, written so (in UTC: they are buckets). */
  dateFormat?: LookDateFormat;
  /** A category axis (one tick per value) or a time axis (ticks spaced by time). */
  xAxisType: "category" | "time";
  /** A marker on each point of a line (px radius). */
  markerSize?: number;
  /** Category labels cut to this many characters. */
  labelMaxLength?: number;
  /** Cut on a word (`…` → `...`) or on a character (`…`). */
  labelCut: "words" | "chars";
  /** Category labels that would overlap: `rotate` writes every one at -45° (else some are dropped). */
  crowdedLabels?: "rotate";
  /** Which grid lines are drawn. */
  gridLines?: GridLines;
  xAxisFontSize?: number;
  yAxisFontSize?: number;
  /** The colour of the axes' labels (and of the labels around a radar or a polar area). */
  axisLabelColor?: string;
  /** No axis line: the labels stand alone. */
  hideAxisLines: boolean;
  /** The value axis has this many steps (two labels for one: zero and the top). */
  valueTickCount?: number;
  /** Grid lines, polygons and rings in the text colour at this opacity, rather than the theme's border. */
  gridOpacity?: number;
  /** Data values written large and bold, a little off the bar's end. */
  dataValuesStrong: boolean;
  /** A radar's radius (px). */
  radius?: number;
  /** A donut's hole, as a share of its radius (%). */
  donutHoleSize?: number;
  /** A heatmap's colours by ranges of its values (`palettes.heatRanges`). */
  colorRanges?: "heat";
  /** A treemap of one colour: smaller values in lighter (or darker) shades. */
  valueShades: boolean;
  /** The value axis laid out with round steps from the data's bounds (`nice-scale.ts`). */
  niceTicks: boolean;
  /** The legend's marker: a dot or a square. */
  legendMarker?: "circle" | "square";
  /** The legend's text colour, rather than the series' own. */
  legendLabelColor?: string;
  /** A pie or a donut at the top of its widget, rather than centred. */
  pieAlign: "center" | "top";
}

export function readLook(raw: unknown): Look {
  const o = isRecord(raw) ? raw : {};
  const from = num(o.fillOpacityFrom, 0, 1);
  return {
    palette: oneOf(o.palette, LOOK_PALETTES),
    paletteLead: resolveColor(o.paletteLead),
    paletteNoPale: bool(o.paletteNoPale),
    numberFormat: oneOf(o.numberFormat, NUMBER_FORMATS),
    tooltipContent: oneOf(o.tooltipContent, ["labelValue", "label"] as const) ?? "labelValue",
    separatorWidth: num(o.separatorWidth, 0, 6),
    openLinks: bool(o.openLinks),
    emptySample: oneOf(o.emptySample, SAMPLES),
    fill: oneOf(o.fill, ["solid", "gradient"] as const) ?? "solid",
    fillOpacityFrom: from ?? 0.7,
    fillOpacityTo: num(o.fillOpacityTo, 0, 1) ?? 0.1,
    barRadius: num(o.barRadius, 0, 12),
    barSize: num(o.barSize, 10, 100),
    barGap: num(o.barGap, 0, 12),
    colorByPoint: bool(o.colorByPoint),
    stackTotals: bool(o.stackTotals),
    dateFormat: oneOf(o.dateFormat, LOOK_DATE_FORMATS),
    xAxisType: oneOf(o.xAxisType, ["category", "time"] as const) ?? "category",
    markerSize: num(o.markerSize, 2, 10),
    labelMaxLength: num(o.labelMaxLength, 5, 100),
    labelCut: oneOf(o.labelCut, ["words", "chars"] as const) ?? "chars",
    crowdedLabels: oneOf(o.crowdedLabels, ["rotate"] as const),
    gridLines: oneOf(o.gridLines, GRID_LINES),
    xAxisFontSize: num(o.xAxisFontSize, 8, 16),
    yAxisFontSize: num(o.yAxisFontSize, 8, 16),
    axisLabelColor: resolveColor(o.axisLabelColor),
    hideAxisLines: bool(o.hideAxisLines),
    valueTickCount: num(o.valueTickCount, 1, 10),
    gridOpacity: num(o.gridOpacity, 0, 0.5),
    dataValuesStrong: bool(o.dataValuesStrong),
    radius: num(o.radius, 40, 400),
    donutHoleSize: num(o.donutHoleSize, 10, 90),
    colorRanges: oneOf(o.colorRanges, ["heat"] as const),
    valueShades: bool(o.valueShades),
    niceTicks: bool(o.niceTicks),
    legendMarker: oneOf(o.legendMarker, ["circle", "square"] as const),
    legendLabelColor: resolveColor(o.legendLabelColor),
    pieAlign: oneOf(o.pieAlign, ["center", "top"] as const) ?? "center",
  };
}

export type EmptyMessage = "No data has been found." | "No data to display" | "No data for this period.";

/** What a widget with nothing to show says: the sentence its module writes (`emptyMessage`), else
 *  XTM One's; whether it stands in the middle of the whole widget, as the module's does, under an
 *  inbox, and in the text's own colour (`strong`, OpenCTI's plain paragraph) rather than muted. */
export function emptyNote(options: unknown): { message: EmptyMessage; fill: boolean; inbox: boolean; strong: boolean } {
  const asked = isRecord(options) ? options.emptyMessage : undefined;
  if (asked === "found") return { message: "No data has been found.", fill: true, inbox: false, strong: true };
  if (asked === "display") return { message: "No data to display", fill: true, inbox: true, strong: false };
  return { message: "No data for this period.", fill: false, inbox: false, strong: false };
}

// ── Colours ──────────────────────────────────────────────────────────────────

/** The colours the look draws series or slices with, in order (the lead, then the palette), or
 *  `undefined` when it names none (the renderer's own palette then). */
export function lookColors(look: Pick<Look, "palette" | "paletteLead" | "paletteNoPale">, mode: ColorMode): string[] | undefined {
  if (!look.palette && !look.paletteLead) return undefined;
  const palette = look.palette ? paletteColors(look.palette, mode) : [];
  const colors = [...(look.paletteLead ? [look.paletteLead] : []), ...palette];
  return look.paletteNoPale ? colors.filter((color) => !isPale(color)) : colors;
}

/** The `index`-th colour of a list, cycling; `undefined` when the list is empty or unset. */
export const nthColor = (colors: readonly string[] | undefined, index: number): string | undefined =>
  colors?.length ? colors[((index % colors.length) + colors.length) % colors.length] : undefined;

/** What a distribution item or the point of a series carries besides its value. */
export interface Extras {
  color?: string;
  link?: string;
  key?: string;
  entityType?: string;
}

/** The extras of each value a chart draws from one series: a distribution's items, or the points
 *  of a series frame's first series, in the frame's order (`frameOf`, `seriesXOrder`). Colours resolved for
 *  the theme (`themedColor`), links kept as the server built them. */
export function valueExtras(shape: string, data: unknown, mode: ColorMode): Extras[] {
  const raw = isRecord(data) ? data : {};
  if (shape === "distribution") {
    return (Array.isArray(raw.items) ? raw.items : []).filter(isRecord).map((item) => extrasOf(item, mode));
  }
  if (shape === "series") {
    const first = (Array.isArray(raw.series) ? raw.series : []).find(isRecord);
    const points = (Array.isArray(first?.points) ? first.points : []).filter(isRecord);
    const byX = new Map(points.map((p) => [String(p.x ?? ""), extrasOf(p, mode)]));
    const xs = new Set<string>();
    for (const s of (Array.isArray(raw.series) ? raw.series : []).filter(isRecord)) {
      for (const p of Array.isArray(s.points) ? s.points : []) if (isRecord(p)) xs.add(String(p.x ?? ""));
    }
    return seriesXOrder(xs).map((x) => byX.get(x) ?? {});
  }
  return [];
}

function extrasOf(record: Raw, mode: ColorMode): Extras {
  const color = resolveColor(record.color);
  return {
    color: color ? themedColor(color, mode) : undefined,
    link: typeof record.link === "string" ? record.link : undefined,
    key: typeof record.key === "string" && record.key ? record.key : undefined,
    entityType: typeof record.entityType === "string" ? record.entityType : undefined,
  };
}

/** Each series' own colour (`color`), for the theme, in the data's order. */
export function seriesColorsOf(data: unknown, mode: ColorMode): (string | undefined)[] {
  const raw = isRecord(data) ? data : {};
  return (Array.isArray(raw.series) ? raw.series : []).filter(isRecord).map((s) => {
    const color = resolveColor(s.color);
    return color ? themedColor(color, mode) : undefined;
  });
}

/** The grid's colour: the text colour of the theme at `opacity`, or the theme's border. */
export function gridColor(opacity: number | undefined, mode: ColorMode): string {
  if (opacity === undefined) return "var(--border-elevation-subtle)";
  return mode === "dark" ? `rgba(255, 255, 255, ${opacity})` : `rgba(0, 0, 0, ${opacity})`;
}

// ── Text ─────────────────────────────────────────────────────────────────────

/** A value written as the look asks, else in XTM One's compact form (`compact`). */
export function lookNumber(look: Pick<Look, "numberFormat">, value: unknown, compact: (value: unknown) => string, locale?: string): string {
  const n = typeof value === "number" ? value : Number(value);
  if (!look.numberFormat) return compact(value);
  return formatNamedNumber(Number.isFinite(n) ? n : null, look.numberFormat, locale);
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$/;

/** An x value: a date written as `dateFormat` asks (in UTC), anything else as it is. */
export function lookCategory(look: Pick<Look, "dateFormat">, value: unknown, locale?: string): string {
  const text = value === null || value === undefined ? "" : String(value);
  if (!look.dateFormat || !ISO_DATE.test(text)) return text;
  if (look.dateFormat === "longDate") return formatNamedDate(text, "longDate", locale, "UTC");
  const interval = look.dateFormat === "monthYear" ? "month" : look.dateFormat === "year" ? "year" : "day";
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? text : new Intl.DateTimeFormat(locale, axisDateOptions(interval)).format(date);
}

/**
 * How a category axis whose look asks to `rotate` crowded labels writes them (ApexCharts' `labels.rotate: -45`):
 * every label, tilted -45° once the widest (0.6 em a character) no longer fits the room each category has, and
 * the height the axis then takes; level, the axis keeps its own way.
 */
export function crowdedAxis(labels: string[], plotWidth: number, fontSize: number): { rotate: boolean; height: number } {
  const widest = Math.max(0, ...labels.map((label) => label.length)) * fontSize * 0.6;
  const room = plotWidth / Math.max(1, labels.length);
  if (widest + 8 <= room) return { rotate: false, height: 24 };
  return { rotate: true, height: Math.ceil(widest * Math.SQRT1_2 + fontSize + 8) };
}

/** A label cut to `max` characters: on a character with `…`, or on the last word with `...`. */
export function cutLabel(label: string, max: number | undefined, cut: Look["labelCut"] = "chars"): string {
  if (max === undefined || label.length <= max) return label;
  if (cut === "chars") return `${label.slice(0, Math.max(1, max - 1)).trimEnd()}…`;
  const kept = label.slice(0, max);
  const space = kept.lastIndexOf(" ");
  return `${space > 0 ? kept.slice(0, space) : kept}...`;
}

/** The time ticks of a time axis: at most `count` of the moments, evenly picked, first and last kept. */
export function timeTicks(moments: readonly number[], count: number): number[] {
  const sorted = [...new Set(moments.filter(Number.isFinite))].sort((a, b) => a - b);
  const most = Math.max(2, Math.trunc(count));
  if (sorted.length <= most) return sorted;
  const step = (sorted.length - 1) / (most - 1);
  return [...new Set(Array.from({ length: most }, (_, i) => sorted[Math.round(i * step)]))];
}

// ── Samples ──────────────────────────────────────────────────────────────────

/** Nothing to show: no value, or only zeros. */
export function isAllEmpty(shape: string, data: unknown): boolean {
  const raw = isRecord(data) ? data : {};
  if (shape === "series") {
    const series = (Array.isArray(raw.series) ? raw.series : []).filter(isRecord);
    return series.every((s) => (Array.isArray(s.points) ? s.points : []).every((p) => !isRecord(p) || !Number(p.y)));
  }
  if (shape === "distribution") {
    return (Array.isArray(raw.items) ? raw.items : []).every((i) => !isRecord(i) || !Number(i.value));
  }
  return false;
}

type SeriesSample = { name: string; color?: string; points: { x: string; y: number; color?: string }[] }[];

const points = (labels: readonly string[], values: readonly number[], colors: readonly string[] = []) =>
  labels.map((x, i) => ({ x, y: values[i], ...(colors[i] ? { color: colors[i] } : {}) }));
// The outcome colours of a sample: succeeded, failed, pending.
const OUTCOME_COLORS = ["success", "#f14337", "#9e9e9e"];
const PLATFORMS = ["EDR", "SIEM", "Firewall", "Email Gateway", "Web Proxy", "Cloud"];
const TECHNIQUES = [
  "T1059 Command and Scripting Interpreter",
  "T1055 Process Injection",
  "T1003 OS Credential Dumping",
  "T1021 Remote Services",
  "T1547 Boot or Logon Autostart",
];

/** Eight weekly dates ending `now`, as ISO dates. */
export function weeklyDates(now: Date): string[] {
  return Array.from({ length: 8 }, (_, i) => new Date(now.getTime() - (7 - i) * 7 * 86_400_000).toISOString().slice(0, 10));
}

/** A sample's series: statuses, results by security platform or by technique, two lines over eight
 *  weeks (named as the widget's own series), outcomes by platform. */
export function sampleSeries(name: SampleName, names: readonly string[], now = new Date()): SeriesSample {
  switch (name) {
    case "status":
      return [{ name: "", points: points(["SUCCESS", "FAILED", "PENDING"], [64, 27, 9], OUTCOME_COLORS) }];
    case "platforms":
      return [
        { name: "Not Detected", points: points(PLATFORMS, [14, 22, 9, 17, 12, 19]) },
        { name: "Not Prevented", points: points(PLATFORMS, [9, 16, 6, 11, 8, 13]) },
      ];
    case "ttp":
      return [
        { name: "Detected TTPs", points: points(TECHNIQUES, [32, 26, 21, 17, 12]) },
        { name: "Prevented TTPs", points: points(TECHNIQUES, [24, 19, 15, 11, 8]) },
      ];
    case "temporal": {
      const dates = weeklyDates(now);
      const first = { name: names[0] || "", points: points(dates, [4, 7, 5, 9, 12, 8, 14, 11]) };
      return names.length > 1 ? [first, { name: names[1], points: points(dates, [2, 5, 3, 7, 9, 6, 10, 7]) }] : [first];
    }
    case "radar":
      return [
        { name: "Success", color: OUTCOME_COLORS[0], points: points(PLATFORMS, [42, 31, 47, 26, 35, 22]) },
        { name: "Failed", color: OUTCOME_COLORS[1], points: points(PLATFORMS, [12, 25, 8, 21, 14, 24]) },
      ];
  }
}

/** What a sample of several series in place of one changes in the options: drawn as several are,
 *  with its legend and no value written on its bars. */
const SEVERAL_SERIES = { legendDisplay: "bottom", dataValuesDisplay: "off" } as const;

/** The sample drawn in place of empty data, in the data's own shape (a distribution, when the sample
 *  has one series; a sample of more stays series, as the module draws it, `options` saying how), and
 *  how greyed it is: fully, or a radar's lighter grey. */
export function sampleFor(
  name: SampleName,
  shape: string,
  data: unknown,
): { shape: "series" | "distribution"; data: unknown; variant: "full" | "subtle"; options: Raw } {
  const raw = isRecord(data) ? data : {};
  const names = (Array.isArray(raw.series) ? raw.series : []).filter(isRecord).map((s) => String(s.name ?? ""));
  const series = sampleSeries(name, names);
  const variant = name === "radar" ? "subtle" : "full";
  if (shape === "distribution" && series.length === 1) {
    return { shape: "distribution", data: { items: series[0].points.map((p) => ({ label: p.x, value: p.y, color: p.color })) }, variant, options: {} };
  }
  return { shape: "series", data: { series }, variant, options: series.length > Math.max(1, names.length) ? { ...SEVERAL_SERIES } : {} };
}
