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
 * Pure helpers of the native `chart` block: how a number is WRITTEN
 * (`format` / `unit` / `decimals` / `unit_scale`), which colour a series TAKES, the bounds a chart reads
 * and the round ticks its value axis stands on. No React, no Recharts — the
 * PPTX writer rebuilds the chart from what the figure PUBLISHES
 * (`chartExportPayload`, the `.deck-chart-facts` element) and mirrors the
 * number rules in Excel format codes (`backend/app/services/chart_export.py`),
 * so they live apart from the renderer.
 *
 * A chart that plots two units reads each one against its own value
 * axis: `axisView` is the chart as one axis reads it (its series, its target,
 * its format), and `dualAxisScales` cuts both axes into the same number of
 * round steps so they share one set of gridlines.
 */
import type {
  DeckButterflySpine,
  DeckChartBlock,
  DeckChartSeries,
  DeckLegendPosition,
  DeckMapLegendStyle,
  DeckSeriesAxis,
  DeckTone,
  DeckValueFormat,
  DeckValueLabelFit,
  DeckValueLabelSize,
} from "@/types";

// The explicit extension lets Node's runner load this module through
// `field-applicability.ts` (the bundler resolves it either way).
import { CHART_SERIES_VARIABLES } from "../theme.ts";

/** ISO codes written as their sign: `EUR 16.5M` reads as `€16.5M`. */
const CURRENCY_SIGNS: Record<string, string> = {
  EUR: "€",
  USD: "$",
  GBP: "£",
  JPY: "¥",
  CNY: "¥",
  INR: "₹",
  KRW: "₩",
};

/** The default currency: the backend documents `unit` as "EUR by default". */
const DEFAULT_CURRENCY = "EUR";

/** Compact steps of a currency amount (and of a large axis tick). */
const COMPACT_STEPS: ReadonlyArray<readonly [number, string]> = [
  [1e9, "B"],
  [1e6, "M"],
  [1e3, "k"],
];

/** Axis ticks of a `number` chart compact from this magnitude on. */
export const AXIS_COMPACT_FROM = 10_000;

/** Above this many slices a pie or a donut writes no value labels (they would overlap). */
export const MAX_LABELLED_VALUES = 24;

/**
 * Where a value is written; an axis is terser than a label or a tooltip, and
 * an axis whose top reaches `AXIS_COMPACT_FROM` writes EVERY tick compact
 * (`13k / 6.5k / 0`, never `13k / 6,500 / 0`) - the caller decides once for
 * the axis, from its ceiling, not per tick. The values written on such a
 * figure are compact too (`label_compact`: `2.2M users`): a label is
 * read against the ticks beside it and has a bar's width to fit in. The
 * tooltip keeps every digit (`label`).
 */
type ValueSurface = "label" | "label_compact" | "axis" | "axis_compact";

/**
 * The most decimals a chart writes on its own: past them two values
 * differ by float noise, not by a figure. Mirrored by `chart_export.py`.
 */
export const MAX_AUTO_DECIMALS = 6;

/** How many decimals a written amount shows: at least `min`, at most `max`, trailing zeros dropped. */
interface Digits {
  min: number;
  max: number;
}

const NUMBER_FORMATS = new Map<string, Intl.NumberFormat>();

function plainNumber(value: number, locale: string, { min, max }: Digits): string {
  const key = `${locale}|${min}|${max}`;
  let format = NUMBER_FORMATS.get(key);
  if (!format) {
    const options = { minimumFractionDigits: min, maximumFractionDigits: max };
    try {
      format = new Intl.NumberFormat(locale, options);
    } catch {
      format = new Intl.NumberFormat("en", options);
    }
    NUMBER_FORMATS.set(key, format);
  }
  return format.format(value);
}

/** The divisor and letter an amount of this magnitude is written compact in (`[1e6, "M"]`), `[1, ""]` below a thousand. */
function compactTier(value: number): readonly [number, string] {
  const magnitude = Math.abs(value);
  return COMPACT_STEPS.find(([limit]) => magnitude >= limit) ?? [1, ""];
}

/**
 * The decimals that give an amount two significant digits under 10 (`1.4`,
 * `0.96`, `0.053`), one decimal at most from 10 on (`16.5`, `527.1`).
 */
function ownDecimals(amount: number): number {
  const magnitude = Math.abs(amount);
  if (!(magnitude > 0) || magnitude >= 10 || !Number.isFinite(magnitude)) return 1;
  return Math.min(MAX_AUTO_DECIMALS, 1 - Math.floor(Math.log10(magnitude)));
}

/** `16.5M`, `527.1k`, `850`: two significant digits under 10, one decimal at most above, trailing zero dropped by Intl. */
export function compactNumber(value: number, locale: string): string {
  const [limit, suffix] = compactTier(value);
  return `${plainNumber(value / limit, locale, { min: 0, max: ownDecimals(value / limit) })}${suffix}`;
}

/** Whether a value of `format` is written compact on `surface`: a currency always, a number on a compact axis. */
function isCompact(format: DeckValueFormat, surface: ValueSurface): boolean {
  return format === "currency" || (format === "number" && (surface === "axis_compact" || surface === "label_compact"));
}

/** What writing a value reads off a block: how it is written and, when known, the kind of chart it is written on. */
type ValueReading = Pick<DeckChartBlock, "format" | "unit" | "decimals" | "unit_scale"> &
  Partial<Pick<DeckChartBlock, "kind">>;

/** Whether a reading can pin its amounts to one letter (`unit_scale`): a currency, on an axis chart. */
export function unitScaleApplies(block: Pick<ValueReading, "format" | "kind">): boolean {
  return (
    block.format === "currency" &&
    (block.kind === undefined || (!isSingleSeriesKind(block.kind) && block.kind !== "map"))
  );
}

/**
 * The one tier every amount of a block is written in when it pins one
 * (`unit_scale`) - `null` for `auto`, and wherever the scale does not
 * apply (`unitScaleApplies`): a pie, a donut, a gauge, a number and a percent
 * keep their own rule. Mirrored by `chart_export.pinned_tier`.
 */
export function pinnedTier(block: ValueReading): readonly [number, string] | null {
  if (!unitScaleApplies(block)) return null;
  return COMPACT_STEPS.find(([, letter]) => letter === block.unit_scale) ?? null;
}

/** The divisor and letter `value` is written with on `surface`: the block's pinned tier, else its own when compact. */
function valueTier(value: number, block: ValueReading, surface: ValueSurface): readonly [number, string] {
  return pinnedTier(block) ?? (isCompact(block.format ?? "number", surface) ? compactTier(value) : [1, ""]);
}

/** The sign a currency `unit` is written with: a symbol as is, a known code as its symbol. */
function currencySign(unit: string | undefined): string {
  const code = (unit ?? DEFAULT_CURRENCY).trim();
  return CURRENCY_SIGNS[code.toUpperCase()] ?? code;
}

/** A sign is glued to the amount (`€16.5M`); a code keeps a space (`CHF 16.5M`). */
function isSymbolic(sign: string): boolean {
  return !/[A-Za-z0-9]/.test(sign);
}

/**
 * Write one value the way the block asks — the ONE rule for axis ticks,
 * value labels, tooltips and the gauge dial.
 *
 * - `number`: locale thousands separator, `unit` suffixed to a label
 *   (`1,204 users`, `3.4h`); axis ticks drop the unit, and an `axis_compact`
 *   axis (one reaching `AXIS_COMPACT_FROM`) writes them compact, as a
 *   `label_compact` label does (`2.2M`, `12.5k users`).
 * - `currency`: the raw amount written compact with the sign of `unit`
 *   (`€16.5M`, `€527.1k`, `CHF 850`) - or, on an axis chart that pins a
 *   `unit_scale`, in that one letter (`€0.25M` beside `€2.50M`, `pinnedTier`).
 * - `percent`: the value IS the percentage (`86` -> `86%`); `unit` is ignored.
 *
 * The block's `decimals` writes every value with exactly that many (of the
 * compact amount: `€19.14M`; a pinned amount that rounds to zero: `€0.00M`).
 * Without it a value shows up to `maxDecimals`, the decimals of the run it is
 * written in (`chartValueWriter`), and alone two significant digits under 10
 * (`0.96`, `1.4`).
 *
 * `axisUnit` keeps the unit on an axis tick: the two axes of a chart that
 * plots two units each say which one they read (`0.5x / 1x / 1.5x`).
 */
export function formatChartValue(
  value: number,
  block: ValueReading,
  locale: string,
  surface: ValueSurface = "label",
  { axisUnit = false, maxDecimals }: { axisUnit?: boolean; maxDecimals?: number } = {},
): string {
  if (!Number.isFinite(value)) return "";
  const format: DeckValueFormat = block.format ?? "number";
  const [limit, letter] = valueTier(value, block, surface);
  const amount = value / limit;
  const fixed = typeof block.decimals === "number" ? block.decimals : undefined;
  const digits: Digits = fixed !== undefined ? { min: fixed, max: fixed } : { min: 0, max: maxDecimals ?? ownDecimals(amount) };
  if (format === "percent") return `${plainNumber(amount, locale, digits)}%`;
  if (format === "currency") {
    const sign = currencySign(block.unit);
    const written = `${plainNumber(Math.abs(amount), locale, digits)}${letter}`;
    const negative = value < 0 ? "-" : "";
    return isSymbolic(sign) ? `${negative}${sign}${written}` : `${negative}${sign} ${written}`;
  }
  const number = `${plainNumber(amount, locale, digits)}${letter}`;
  const labelled = surface === "label" || surface === "label_compact";
  if ((!labelled && !axisUnit) || !block.unit) return number;
  const unit = block.unit.trim();
  return unit.length === 1 ? `${number}${unit}` : `${number} ${unit}`;
}

/**
 * How a RUN of values is written - the values one axis writes together: its
 * ticks, or its labels, tooltips and legend. A block's `decimals`
 * writes each one with exactly that many. Without it the run is written with
 * the decimals its smallest amount under 10 needs for two significant digits
 * (one decimal at most otherwise), and one more while two different values
 * would read the same - `1.57x` and `1.56x`, never `1.5x` twice - up to
 * `MAX_AUTO_DECIMALS` - read at the block's pinned tier when it pins one.
 * Mirrored by `chart_export.run_decimals`, the band decimals of the PPTX
 * number formats.
 */
export function chartValueWriter(
  block: ValueReading,
  values: readonly number[],
  locale: string,
  surface: ValueSurface = "label",
  { axisUnit = false }: { axisUnit?: boolean } = {},
): (value: number) => string {
  const write = (value: number, maxDecimals?: number) =>
    formatChartValue(value, block, locale, surface, { axisUnit, maxDecimals });
  if (typeof block.decimals === "number") return (value) => write(value);
  const run = values.filter(Number.isFinite);
  const exact = run.map((value) => write(value, MAX_AUTO_DECIMALS));
  const collides = (digits: number): boolean => {
    const seen = new Map<string, string>();
    return run.some((value, index) => {
      const text = write(value, digits);
      const other = seen.get(text);
      seen.set(text, other ?? exact[index]);
      return other !== undefined && other !== exact[index];
    });
  };
  let maxDecimals = Math.max(1, ...run.map((value) => ownDecimals(value / valueTier(value, block, surface)[0])));
  while (maxDecimals < MAX_AUTO_DECIMALS && collides(maxDecimals)) maxDecimals += 1;
  return (value) => write(value, maxDecimals);
}

/**
 * Every value a chart - or one axis view of it (`axisView`) - writes on its
 * figure: its series' values, a stack's totals each way, a waterfall's
 * changes and the levels they bring the bridge to, a donut's total, its
 * target (a gauge's maximum), a butterfly's side totals under its legend.
 * The run its labels, tooltips and legend take their decimals from;
 * `chart_export.view_values` reads the same off the payload for the labels
 * the PPTX writes.
 */
export function writtenValues(view: DeckChartBlock): number[] {
  const target = view.target !== undefined && view.target !== null ? [view.target] : [];
  if (view.kind === "waterfall") {
    return [...waterfallSteps(view, "").flatMap((step) => [step.value, step.end]), ...target];
  }
  const values = view.series.flatMap((entry) => entry.values);
  if (isMirroredKind(view.kind)) return [...values, ...(butterflyTotals(view) ?? [])];
  if (isStacked(view)) {
    view.categories.forEach((_, index) => {
      let up = 0;
      let down = 0;
      for (const entry of view.series) {
        const value = entry.values[index] ?? 0;
        if (value > 0) up += value;
        else if (value < 0) down += value;
      }
      values.push(up, down);
    });
  }
  if (view.kind === "donut") values.push(values.reduce((sum, value) => sum + Math.max(value, 0), 0));
  if (view.kind === "gauge") return [...values, gaugeReading(view).max];
  return [...values, ...target];
}

/** The semantic tones a series may be drawn in, as the deck's CSS tokens. */
const TONE_VARIABLES: Record<DeckTone, string> = {
  accent: "var(--deck-accent)",
  info: "var(--deck-info)",
  success: "var(--deck-success)",
  warning: "var(--deck-warning)",
  error: "var(--deck-error)",
  muted: "var(--deck-muted)",
};

/**
 * One colour per series, in series order: a TONED series takes its semantic
 * colour, the untoned ones take the template's chart palette IN ORDER —
 * `chart_1` goes to the first untoned series, whatever its position, so a
 * "Plan (muted) vs Actual" chart still draws Actual in the accent.
 */
export function seriesColors(series: readonly DeckChartSeries[]): string[] {
  let paletteIndex = 0;
  return series.map((entry) => {
    if (entry.tone) return TONE_VARIABLES[entry.tone];
    const color = paletteColor(paletteIndex);
    paletteIndex += 1;
    return color;
  });
}

/** The n-th palette colour, cycling past the six the template defines. */
export function paletteColor(index: number): string {
  const variable = CHART_SERIES_VARIABLES[index % CHART_SERIES_VARIABLES.length];
  return `var(${variable})`;
}

/* ── Waterfall ─────────────────────────────────────────────────────── */

/** Mirrors `DEFAULT_WATERFALL_TOLERANCE` in `presentation_spec.py`, in percent. */
export const DEFAULT_WATERFALL_TOLERANCE = 0.5;
/** Mirrors `_BRIDGE_FLOAT_SLACK`: `16.5 + 1.8 + 1.3 - 0.4` is `19.200000000000003`. */
const BRIDGE_FLOAT_SLACK = 1e-9;

/** What a waterfall bar is: a change up or down, a level drawn from zero, or a miss `allow_gap` draws. */
type WaterfallRole = "increase" | "decrease" | "total" | "gap";

/** One bar of a waterfall: its label, its value (the change, or a total's level) and the span it covers. */
export interface WaterfallStep {
  category: string;
  value: number;
  start: number;
  end: number;
  role: WaterfallRole;
}

/** The tone of each role; a toned series gives its tone to every bar but a gap. */
const WATERFALL_TONES: Record<WaterfallRole, DeckTone> = {
  increase: "success",
  decrease: "error",
  total: "accent",
  gap: "warning",
};

/**
 * Whether a total misses the running sum of the steps before it: by more than
 * `tolerance` percent of the movement since the previous total (the steps'
 * sizes added up), float noise aside. The rule of `waterfall_gaps` in
 * `presentation_spec.py`, which refuses a saved bridge that misses.
 */
export function bridgeMisses(total: number, running: number, moved: number, tolerance: number): boolean {
  const slack = BRIDGE_FLOAT_SLACK * Math.max(1, Math.abs(total), Math.abs(running));
  return Math.abs(total - running) > (tolerance / 100) * moved + slack;
}

/**
 * The bars of a waterfall, in order. A step floats from the running total to
 * where it brings it; a total stands on zero and is the level from there on;
 * the first category starts from zero. With `allow_gap`, a total the steps
 * miss is preceded by a `gap` bar named `gapLabel`, from the running sum to the
 * total, so the bridge shows what it does not explain instead of burying it in
 * the total. Without it a saved bridge has no miss (the validator refuses one)
 * and a total within the tolerance simply stands at its own level.
 */
export function waterfallSteps(
  block: Pick<DeckChartBlock, "series" | "categories" | "totals" | "tolerance" | "allow_gap">,
  gapLabel: string,
): WaterfallStep[] {
  const values = block.series[0]?.values ?? [];
  const totals = new Set(block.totals ?? []);
  const tolerance = block.tolerance ?? DEFAULT_WATERFALL_TOLERANCE;
  const steps: WaterfallStep[] = [];
  let running = 0;
  let moved = 0;
  block.categories.forEach((category, index) => {
    const value = values[index] ?? 0;
    if (!totals.has(index)) {
      steps.push({ category, value, start: running, end: running + value, role: value < 0 ? "decrease" : "increase" });
      running += value;
      moved += Math.abs(value);
      return;
    }
    if (index > 0 && block.allow_gap === true && bridgeMisses(value, running, moved, tolerance)) {
      steps.push({ category: gapLabel, value: value - running, start: running, end: value, role: "gap" });
    }
    steps.push({ category, value, start: 0, end: value, role: "total" });
    running = value;
    moved = 0;
  });
  return steps;
}

/** The colour of a waterfall bar: its role's tone, or the series' own tone (never on a gap). */
export function waterfallColor(role: WaterfallRole, tone: DeckTone | undefined): string {
  return TONE_VARIABLES[tone && role !== "gap" ? tone : WATERFALL_TONES[role]];
}

/**
 * What a waterfall bar is labelled with: a total's level, a change with its
 * sign (`+€1.8M`, `-€400k`), written by the bridge's writer (`chartValueWriter`
 * over its `writtenValues`).
 */
export function waterfallLabel(step: WaterfallStep, write: (value: number) => string): string {
  const text = write(step.value);
  return step.role !== "total" && step.value > 0 ? `+${text}` : text;
}

/* ── Butterfly ─────────────────────────────────────────────────────── */

/**
 * Whether a chart mirrors its two series about a central spine: the first
 * drawn leftwards, the second rightwards, both read as positive sizes. Its
 * geometry is `chart-butterfly.ts`'s; how it reads is here, as for any chart.
 */
export function isMirroredKind(kind: DeckChartBlock["kind"]): boolean {
  return kind === "butterfly";
}

/** Where a butterfly writes its category labels: the block's word, else on its spine. */
export function butterflySpine(block: Pick<DeckChartBlock, "spine">): DeckButterflySpine {
  return block.spine === "left" ? "left" : "center";
}

/** Whether a butterfly draws both sides to one extent: unless the block says `symmetric: false`. */
export function butterflySymmetric(block: Pick<DeckChartBlock, "symmetric">): boolean {
  return block.symmetric !== false;
}

/** Each side's total, left then right, when a butterfly writes them under its legend (`show_totals`); `null` otherwise. */
export function butterflyTotals(block: Pick<DeckChartBlock, "kind" | "series" | "show_totals">): readonly number[] | null {
  if (!isMirroredKind(block.kind) || block.show_totals !== true) return null;
  return block.series.map((entry) => entry.values.reduce((sum, value) => sum + Math.abs(value), 0));
}

/** Whether an axis chart draws a target line (the gauge reads its target as its maximum). */
export function hasTargetLine(block: Pick<DeckChartBlock, "kind" | "target">): boolean {
  return !isSingleSeriesKind(block.kind) && block.target !== undefined && block.target !== null;
}

/**
 * What the legend calls the target line: its label, else its value, as
 * precise as the values it is read against. The legend is where the target
 * is NAMED - a label written on the line itself lands on whichever value
 * sits nearest it.
 */
export function targetLegendName(block: DeckChartBlock, locale: string): string | null {
  if (!hasTargetLine(block)) return null;
  if (block.goal_label) return block.goal_label;
  const view = axisView(block, "primary");
  return chartValueWriter(view, writtenValues(view), locale)(block.target as number);
}

/** The value axis a series reads against: unset is the left-hand one. */
export function seriesAxis(entry: Pick<DeckChartSeries, "axis">): DeckSeriesAxis {
  return entry.axis === "secondary" ? "secondary" : "primary";
}

/** Whether a chart reads a second unit against a right-hand value axis. */
export function hasSecondaryAxis(block: Pick<DeckChartBlock, "kind" | "series" | "horizontal">): boolean {
  if (block.kind !== "bar" && block.kind !== "line" && block.kind !== "combo") return false;
  return block.horizontal !== true && block.series.some((entry) => seriesAxis(entry) === "secondary");
}

/** How one value axis writes a number: `format` / `unit` / `decimals` / `unit_scale`, the `secondary_` ones for the right-hand axis. */
export function axisReading(
  block: Pick<
    DeckChartBlock,
    | "format"
    | "unit"
    | "decimals"
    | "unit_scale"
    | "secondary_format"
    | "secondary_unit"
    | "secondary_decimals"
    | "secondary_unit_scale"
  >,
  axis: DeckSeriesAxis,
): Pick<DeckChartBlock, "format" | "unit" | "decimals" | "unit_scale"> {
  if (axis === "secondary" && block.secondary_format) {
    return {
      format: block.secondary_format,
      unit: block.secondary_unit,
      decimals: block.secondary_decimals,
      unit_scale: block.secondary_unit_scale,
    };
  }
  return { format: block.format, unit: block.unit, decimals: block.decimals, unit_scale: block.unit_scale };
}

/**
 * The chart as ONE of its value axes reads it: the series against that axis,
 * the axis' own target, format and range. A chart with one axis is its own
 * view, so every rule written for one axis (the ceiling, the ticks, the
 * surface, the range an author pins) reads either axis of a two-unit chart
 * unchanged.
 */
export function axisView(block: DeckChartBlock, axis: DeckSeriesAxis): DeckChartBlock {
  if (!hasSecondaryAxis(block)) return block;
  const secondary = axis === "secondary";
  return {
    ...block,
    ...axisReading(block, axis),
    series: block.series.filter((entry) => seriesAxis(entry) === axis),
    target: secondary ? block.secondary_target : block.target,
    y_min: secondary ? block.secondary_y_min : block.y_min,
    y_max: secondary ? block.secondary_y_max : block.y_max,
  };
}

/** Whether a two-unit chart draws a reference line against its right-hand axis. */
export function hasSecondaryTargetLine(
  block: Pick<DeckChartBlock, "kind" | "series" | "horizontal" | "secondary_target">,
): boolean {
  return hasSecondaryAxis(block) && block.secondary_target !== undefined && block.secondary_target !== null;
}

/** What the legend calls the right-hand axis' target line: its value, read the way that axis reads its values. */
export function secondaryTargetLegendName(block: DeckChartBlock, locale: string): string | null {
  if (!hasSecondaryTargetLine(block)) return null;
  const view = axisView(block, "secondary");
  return chartValueWriter(view, writtenValues(view), locale)(block.secondary_target as number);
}

/**
 * The ink a value axis of a two-unit chart writes its ticks in: the colour
 * of its series when they all wear one - the right-hand axis of a count and
 * a price reads in the price line's colour - the muted ink otherwise.
 */
export function axisInk(block: DeckChartBlock, axis: DeckSeriesAxis): string {
  const colors = seriesColors(block.series);
  const own = new Set(block.series.flatMap((entry, index) => (seriesAxis(entry) === axis ? [colors[index]] : [])));
  return own.size === 1 ? [...own][0] : TONE_VARIABLES.muted;
}

/**
 * What a legend entry is across edits. A deck is edited in place
 * (`edit_presentation` rewrites the row and the figure re-renders with the new
 * block), so an entry's POSITION is not its identity: after an earlier series
 * is removed or the series are reordered, a hidden index would point at
 * whatever series took that position. A series carries no id, so its name is
 * its identity. Names may repeat, and then nothing tells which of two
 * `Revenue` series an edit kept: a repeated name is that occurrence in THIS
 * list of entries, and its toggle holds only while the list is unchanged.
 */
export function legendIdentities(names: readonly string[]): string[] {
  const total = new Map<string, number>();
  for (const name of names) total.set(name, (total.get(name) ?? 0) + 1);
  const seen = new Map<string, number>();
  return names.map((name) => {
    if (total.get(name) === 1) return JSON.stringify([name]);
    const occurrence = seen.get(name) ?? 0;
    seen.set(name, occurrence + 1);
    return JSON.stringify([name, occurrence, names]);
  });
}

/**
 * The positions the legend's hidden set (entry identities, see
 * `legendIdentities`) hides in a block whose entries are `identities`. An
 * entry the block no longer has hides nothing. An edit that leaves every
 * entry hidden shows them all again: the click guard below keeps one visible,
 * and an empty chart says nothing.
 */
export function resolveHidden(hidden: ReadonlySet<string>, identities: readonly string[]): ReadonlySet<number> {
  const positions = identities.flatMap((identity, index) => (hidden.has(identity) ? [index] : []));
  return new Set(positions.length >= identities.length ? [] : positions);
}

/** A legend click on entry `index`: shows it, or hides it unless it is the last one visible. */
export function toggleHidden(
  hidden: ReadonlySet<string>,
  index: number,
  identities: readonly string[],
): ReadonlySet<string> {
  const next = new Set(resolveHidden(hidden, identities));
  if (next.has(index)) next.delete(index);
  // Never hide the last visible entry: an empty chart says nothing.
  else if (next.size < identities.length - 1) next.add(index);
  return new Set([...next].map((position) => identities[position]));
}

/**
 * Whether the legend is drawn: the block's own word, else whenever there is
 * more than one thing to tell apart - a second series, or a target line.
 */
export function legendShown(block: Pick<DeckChartBlock, "kind" | "series" | "show_legend" | "target">): boolean {
  if (block.show_legend !== undefined && block.show_legend !== null) return block.show_legend;
  if (block.kind === "gauge") return false;
  // A map's colours mean nothing without its ramp.
  if (block.kind === "pie" || block.kind === "donut" || block.kind === "map") return true;
  return block.series.length > 1 || hasTargetLine(block);
}

/**
 * Where the legend sits: the block's own word, else `top` - the
 * historical placement (the panel title's row in a dashboard panel, above the
 * plot on a slide) - and `right` for a map's classes, a list of rows
 * read beside the countries. A placement, not a switch: it says nothing about
 * whether a legend is drawn (`legendShown`).
 */
export function legendPosition(
  block: Pick<DeckChartBlock, "kind" | "legend_position" | "legend_style">,
): DeckLegendPosition {
  return block.legend_position ?? (mapLegendStyle(block) === "classes" ? "right" : "top");
}

/** The kinds whose bar values `value_label_fit` fits, upright or horizontal. */
export const VALUE_LABEL_FIT_KINDS: ReadonlySet<DeckChartBlock["kind"]> = new Set(["bar", "stacked_bar", "combo"]);

/**
 * How the values of the bars are fitted: the block's own word on a
 * bar, stacked bar or combo chart - upright or horizontal - else
 * `auto`, today's rule: smaller, then turned on their side. Every other kind
 * writes its values on its points or slices, so the word acts on none of them.
 */
export function valueLabelFit(block: Pick<DeckChartBlock, "kind" | "value_label_fit">): DeckValueLabelFit {
  const fit = block.value_label_fit;
  if (fit !== "stagger" && fit !== "upright") return "auto";
  return VALUE_LABEL_FIT_KINDS.has(block.kind) ? fit : "auto";
}

/** The kinds whose values on the figure `value_label_size` sizes: the axis charts and a butterfly. */
export const VALUE_LABEL_SIZE_KINDS: ReadonlySet<DeckChartBlock["kind"]> = new Set([
  "bar",
  "stacked_bar",
  "line",
  "area",
  "combo",
  "waterfall",
  "butterfly",
]);
/**
 * The canvas px a `value_label_size` number may name: the smallest
 * size any text scale sets a value upright in, up to well past `lg`'s ticks.
 * Mirrored by `VALUE_LABEL_SIZE_MIN_PX` / `_MAX_PX` in `chart_export.py`.
 */
export const VALUE_LABEL_SIZE_MIN = 10;
/** @public */
export const VALUE_LABEL_SIZE_MAX = 32;

/**
 * The size the values on the figure are tried at first, as the block names it:
 * its `value_label_size` on an axis chart - a text scale, or a whole
 * number of canvas px from `VALUE_LABEL_SIZE_MIN` to `VALUE_LABEL_SIZE_MAX` -
 * else `undefined`, the values following `label_size` as the ticks do. A pie,
 * a donut or a gauge writes its values at its tick size.
 */
export function valueLabelSize(block: Pick<DeckChartBlock, "kind" | "value_label_size">): DeckValueLabelSize | undefined {
  const size = block.value_label_size;
  if (!VALUE_LABEL_SIZE_KINDS.has(block.kind)) return undefined;
  if (size === "sm" || size === "md" || size === "lg") return size;
  return typeof size === "number" && Number.isInteger(size) && size >= VALUE_LABEL_SIZE_MIN && size <= VALUE_LABEL_SIZE_MAX
    ? size
    : undefined;
}

/** Whether the series stack: `stacked_bar` always, `bar` / `area` on request. */
export function isStacked(block: Pick<DeckChartBlock, "kind" | "stacked">): boolean {
  return block.kind === "stacked_bar" || (block.stacked === true && (block.kind === "bar" || block.kind === "area"));
}

/** Whether the block asks for its values on the figure (`show_values`, on unless switched off). */
export function valuesAsked(block: Pick<DeckChartBlock, "show_values">): boolean {
  return block.show_values !== false;
}

/**
 * Whether the values of the series at `index` are written: the series' own
 * `show_values`, else the chart's. The validator allows the series' word on
 * an unstacked bar, line, area or combo chart and on a stacked bar's segments -
 * its stack's totals follow the chart; everywhere else every series follows
 * the chart.
 */
export function seriesValuesAsked(block: Pick<DeckChartBlock, "show_values" | "series">, index: number): boolean {
  return block.series[index]?.show_values ?? valuesAsked(block);
}

/** Whether any series of the block asks for its values (`seriesValuesAsked`). */
export function anyValuesAsked(block: Pick<DeckChartBlock, "show_values" | "series">): boolean {
  return block.series.some((_, index) => seriesValuesAsked(block, index));
}

/**
 * Whether each value is written on the figure, as far as the block alone
 * tells: asked for, and - on a pie or a donut - few enough slices to read.
 * An axis chart is never capped by a count: its drawing decides from
 * the room it has (`axisChartLayout` in `chart-layout.ts`) and publishes
 * what it wrote.
 */
export function valuesShown(block: Pick<DeckChartBlock, "kind" | "series" | "categories" | "show_values">): boolean {
  if (!anyValuesAsked(block)) return false;
  if (block.kind === "pie" || block.kind === "donut") return block.categories.length <= MAX_LABELLED_VALUES;
  return true;
}

/**
 * How the values written ON an axis chart read: compact once its axis
 * is (`axisSurface`), so a bar's label is `€19.1M` / `2.2M`, never
 * `3,800,000`. Mirrored by `chart_export.label_number_format`.
 */
export function labelSurface(
  block: Pick<DeckChartBlock, "kind" | "series" | "categories" | "stacked" | "target">,
): ValueSurface {
  return axisSurface(axisExtent(block)) === "axis_compact" ? "label_compact" : "label";
}

/** Kinds that plot ONE series: a share of a whole, or one value on a dial. */
export function isSingleSeriesKind(kind: DeckChartBlock["kind"]): boolean {
  return kind === "pie" || kind === "donut" || kind === "gauge";
}

/** How a value axis writes its ticks, decided once from the furthest value it reaches. */
export function axisSurface(extent: number): ValueSurface {
  return Math.abs(extent) >= AXIS_COMPACT_FROM ? "axis_compact" : "axis";
}

/**
 * The largest magnitude a value axis reaches, above OR below zero: what the
 * tick surface is decided from. A chart of losses (`-1,200,000` at its
 * deepest, nothing above zero) has a ceiling of zero, and read from that alone
 * its ticks would never compact while the same figures as gains would.
 */
export function axisExtent(block: AxisBlock): number {
  return Math.max(Math.abs(axisCeiling(block)), Math.abs(axisFloor(block)));
}

/** What a value axis reads off a block: its values, how they stack, a waterfall's levels, the target. */
type AxisBlock = Pick<
  DeckChartBlock,
  "kind" | "series" | "categories" | "stacked" | "target" | "totals" | "tolerance" | "allow_gap"
>;

/** Every level a waterfall's bars reach: both ends of each bar. */
function waterfallLevels(block: Omit<AxisBlock, "target">): number[] {
  return waterfallSteps(block, "").flatMap((step) => [step.start, step.end]);
}

/** The largest value an axis must reach: the tallest stack, point or waterfall level, and the target. */
export function axisCeiling(block: AxisBlock): number {
  const stacked = isStacked(block);
  let ceiling = 0;
  if (block.kind === "waterfall") {
    ceiling = Math.max(0, ...waterfallLevels(block));
  } else {
    block.categories.forEach((_, index) => {
      let stack = 0;
      for (const entry of block.series) {
        const value = entry.values[index] ?? 0;
        if (stacked) stack += Math.max(value, 0);
        else ceiling = Math.max(ceiling, value);
      }
      if (stacked) ceiling = Math.max(ceiling, stack);
    });
  }
  if (block.target !== undefined && block.target !== null) ceiling = Math.max(ceiling, block.target);
  return ceiling;
}

/**
 * The lowest value an axis must reach: the deepest negative point, stack or
 * waterfall level, never above zero — a chart of positive values stands on its baseline.
 */
function axisFloor(block: Omit<AxisBlock, "target">): number {
  if (block.kind === "waterfall") return Math.min(0, ...waterfallLevels(block));
  const stacked = isStacked(block);
  let floor = 0;
  block.categories.forEach((_, index) => {
    let stack = 0;
    for (const entry of block.series) {
      const value = entry.values[index] ?? 0;
      if (stacked) stack += Math.min(value, 0);
      else floor = Math.min(floor, value);
    }
    if (stacked) floor = Math.min(floor, stack);
  });
  return floor;
}

/** The round steps a value axis may take, per decade: `1 / 2 / 2.5 / 5 / 10`. */
const NICE_STEPS = [1, 2, 2.5, 5, 10] as const;

/** A value axis as the chart draws it: round ticks from its floor to a rounded top. */
export interface ValueAxisScale {
  min: number;
  max: number;
  step: number;
  ticks: number[];
}

/**
 * The ticks of a value axis, decided HERE rather than left to the charting
 * library: `tickCount` round values (`0 / 10M / 20M / 30M`, never `0 / 7.5M /
 * 15M / 22.5M`) from the floor to the first round value at or above the
 * ceiling, so the axis the viewer draws is an axis the PPTX writer can set on
 * an editable chart (`minimum_scale` / `maximum_scale` / `major_unit`) and the
 * two show the same bars against the same lines. A percent axis that stays
 * under 100 tops out AT 100, on a step that lands there.
 */
export function valueAxisScale(
  block: AxisBlock & Pick<DeckChartBlock, "format" | "y_min" | "y_max">,
  tickCount: number,
): ValueAxisScale {
  const intervals = Math.max(Math.round(tickCount) - 1, 1);
  const floor = axisFloor(block);
  const ceiling = axisCeiling(block);
  const percentDial = block.format === "percent" && ceiling <= 100;
  const top = percentDial ? 100 : ceiling;
  const pinned = pinnedRange(block);
  if (pinned) {
    const scale = pinnedScale(floor, top, intervals, pinned);
    if (scale) return scale;
  }
  const span = top - floor;
  if (!(span > 0)) {
    return { min: 0, max: intervals, step: 1, ticks: Array.from({ length: intervals + 1 }, (_, i) => i) };
  }
  const raw = span / intervals;
  const exponent = Math.floor(Math.log10(raw));
  const magnitude = 10 ** exponent;
  const nice = NICE_STEPS.find((n) => n * magnitude >= raw - raw * 1e-9) ?? 10;
  const step = nice * magnitude;
  const decimals = Math.max(0, -exponent) + (nice === 2.5 ? 1 : 0);
  const round = (value: number) => Number(value.toFixed(decimals));
  const min = floor < 0 ? round(-Math.ceil(-floor / step - 1e-9) * step) : 0;
  const max = round(Math.ceil(top / step - 1e-9) * step);
  const ticks: number[] = [];
  for (let value = min; value <= max + step * 1e-6; value += step) ticks.push(round(value));
  if (ticks[ticks.length - 1] !== max) ticks.push(max);
  return { min, max, step, ticks };
}

/**
 * The ends of a value axis its author pinned (`y_min` / `y_max`, the
 * `secondary_` pair on the right-hand axis read through `axisView`); `null`
 * when neither is set, and the axis is the chart's own. The validator keeps
 * zero and every value the axis plots inside the range.
 */
interface PinnedRange {
  min?: number;
  max?: number;
}

function pinnedRange(view: Pick<DeckChartBlock, "y_min" | "y_max">): PinnedRange | null {
  const min = typeof view.y_min === "number" && Number.isFinite(view.y_min) ? view.y_min : undefined;
  const max = typeof view.y_max === "number" && Number.isFinite(view.y_max) ? view.y_max : undefined;
  return min === undefined && max === undefined ? null : { min, max };
}

/** Rounds away the float noise of a sum of steps (`0.1 + 0.2`). */
const cleanTick = (value: number): number => Number(value.toPrecision(12));

/** Whether a step is one of the round ones (`NICE_STEPS`, any decade). */
function isRoundStep(step: number): boolean {
  if (!(step > 0) || !Number.isFinite(step)) return false;
  const mantissa = step / 10 ** Math.floor(Math.log10(step));
  return NICE_STEPS.some((nice) => Math.abs(mantissa - nice) <= 1e-6);
}

/** The smallest round step at or above `raw`. */
function roundStepAtLeast(raw: number): number {
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const nice = NICE_STEPS.find((n) => n * magnitude >= raw - raw * 1e-9) ?? 10;
  return cleanTick(nice * magnitude);
}

/** `intervals` equal steps from `min` to `max`, both ends exact. */
function evenScale(min: number, max: number, intervals: number): ValueAxisScale {
  const step = (max - min) / intervals;
  const ticks = Array.from({ length: intervals + 1 }, (_, i) => (i === intervals ? max : cleanTick(min + i * step)));
  return { min, max, step: cleanTick(step), ticks };
}

/**
 * The ends of a pinned axis that are known before its step is: the pinned
 * ones, and zero as the floor of an axis with no negative value - the floor
 * the chart's own axis takes.
 */
function pinnedEnds(floor: number, pinned: PinnedRange): { lo?: number; hi?: number } {
  return { lo: pinned.min ?? (floor < 0 ? undefined : 0), hi: pinned.max };
}

/**
 * A single value axis with a pinned end. Both ends known: the number of
 * steps nearest `intervals` that cuts the range into round ones, else
 * `intervals` equal steps (the author asked for those ends). One end known:
 * round steps from it to the first round tick past the values, as the
 * chart's own axis does from zero. `null` when the range holds nothing.
 */
function pinnedScale(floor: number, top: number, intervals: number, pinned: PinnedRange): ValueAxisScale | null {
  const { lo, hi } = pinnedEnds(floor, pinned);
  if (lo !== undefined && hi !== undefined) {
    if (!(hi > lo)) return null;
    const counts = [intervals, intervals + 1, intervals - 1, intervals + 2, intervals - 2].filter((n) => n >= 2);
    const round = counts.find((n) => isRoundStep((hi - lo) / n));
    return evenScale(lo, hi, round ?? Math.max(intervals, 1));
  }
  if (lo !== undefined) {
    if (!(top > lo)) return null;
    const step = roundStepAtLeast((top - lo) / intervals);
    const count = Math.max(1, Math.ceil((top - lo) / step - 1e-9));
    return evenScale(lo, cleanTick(lo + count * step), count);
  }
  if (hi === undefined || !(hi > floor)) return null;
  const step = roundStepAtLeast((hi - floor) / intervals);
  const count = Math.max(1, Math.ceil((hi - floor) / step - 1e-9));
  return evenScale(cleanTick(hi - count * step), hi, count);
}

/**
 * A pinned axis of a two-unit chart, in exactly `intervals` steps so it
 * shares the other axis' gridlines: both ends known, equal steps between
 * them; one end known, the smallest round step that reaches past the values.
 * `null` when the range holds nothing.
 */
function pinnedIn(floor: number, top: number, intervals: number, pinned: PinnedRange): ValueAxisScale | null {
  const { lo, hi } = pinnedEnds(floor, pinned);
  if (lo !== undefined && hi !== undefined) return hi > lo ? evenScale(lo, hi, intervals) : null;
  if (lo !== undefined) {
    return top > lo ? evenScale(lo, cleanTick(lo + intervals * roundStepAtLeast((top - lo) / intervals)), intervals) : null;
  }
  if (hi === undefined || !(hi > floor)) return null;
  return evenScale(cleanTick(hi - intervals * roundStepAtLeast((hi - floor) / intervals)), hi, intervals);
}

/** The floor and the top a value axis must reach: a percent axis that stays under 100 tops out at 100. */
function axisBounds(view: DeckChartBlock): [number, number] {
  const ceiling = axisCeiling(view);
  return [axisFloor(view), view.format === "percent" && ceiling <= 100 ? 100 : ceiling];
}

/** A value axis cut into exactly `intervals` round steps, from the floor to at least the top. */
function scaleIn(floor: number, top: number, intervals: number): ValueAxisScale {
  const span = top - floor;
  if (!(span > 0)) {
    return { min: 0, max: intervals, step: 1, ticks: Array.from({ length: intervals + 1 }, (_, i) => i) };
  }
  const raw = span / intervals;
  const first = Math.floor(Math.log10(raw));
  for (let exponent = first; exponent < first + 24; exponent += 1) {
    const magnitude = 10 ** exponent;
    for (const nice of NICE_STEPS) {
      const step = nice * magnitude;
      if (step < raw - raw * 1e-9) continue;
      const decimals = Math.max(0, -exponent) + (nice === 2.5 ? 1 : 0);
      const round = (value: number) => Number(value.toFixed(decimals));
      const min = floor < 0 ? round(-Math.ceil(-floor / step - 1e-9) * step) : 0;
      const max = round(min + intervals * step);
      if (max < top - Math.abs(top) * 1e-9) continue;
      return { min, max, step, ticks: Array.from({ length: intervals + 1 }, (_, i) => round(min + i * step)) };
    }
  }
  const step = span / intervals;
  return { min: floor, max: top, step, ticks: Array.from({ length: intervals + 1 }, (_, i) => floor + i * step) };
}

/** The two value axes of a chart that plots two units. */
interface DualAxisScales {
  primary: ValueAxisScale;
  secondary: ValueAxisScale;
}

/**
 * The two value axes of a two-unit chart. They share ONE set of gridlines -
 * a right-hand tick floating between two lines reads against neither - so
 * both are cut into the same number of round steps: the count, from
 * `tickCount - 1` down to two, at which the two plots leave the least empty
 * room above their tallest value. An axis whose author pinned its range
 * (`y_min` / `y_max`, `secondary_y_min` / `secondary_y_max`) keeps it, and
 * the count is the one that cuts the pinned ranges into round steps first.
 * The PPTX writer sets the same min / max / step on its two value axes
 * (`.deck-chart-facts`).
 */
export function dualAxisScales(block: DeckChartBlock, tickCount: number): DualAxisScales {
  const most = Math.max(Math.round(tickCount) - 1, 2);
  const primaryView = axisView(block, "primary");
  const secondaryView = axisView(block, "secondary");
  const [primaryFloor, primaryTop] = axisBounds(primaryView);
  const [secondaryFloor, secondaryTop] = axisBounds(secondaryView);
  const primaryPin = pinnedRange(primaryView);
  const secondaryPin = pinnedRange(secondaryView);
  const filled = (scale: ValueAxisScale, floor: number, top: number) =>
    scale.max > scale.min ? (top - floor) / (scale.max - scale.min) : 0;
  const axisIn = (floor: number, top: number, intervals: number, pin: PinnedRange | null) => {
    const pinned = pin ? pinnedIn(floor, top, intervals, pin) : null;
    // An unpinned axis is always cut in round steps: only a pinned one can miss them.
    return { scale: pinned ?? scaleIn(floor, top, intervals), round: !pinned || isRoundStep(pinned.step) };
  };
  const cut = (intervals: number) => {
    const primary = axisIn(primaryFloor, primaryTop, intervals, primaryPin);
    const secondary = axisIn(secondaryFloor, secondaryTop, intervals, secondaryPin);
    const score =
      filled(primary.scale, primaryFloor, primaryTop) + filled(secondary.scale, secondaryFloor, secondaryTop);
    return { primary: primary.scale, secondary: secondary.scale, round: Number(primary.round) + Number(secondary.round), score };
  };
  // From the most steps down, so a tie keeps the finer grid.
  let best = cut(most);
  for (let intervals = most - 1; intervals >= 2; intervals -= 1) {
    const candidate = cut(intervals);
    if (candidate.round > best.round || (candidate.round === best.round && candidate.score > best.score + 1e-9)) {
      best = candidate;
    }
  }
  return { primary: best.primary, secondary: best.secondary };
}

/**
 * The dial of a gauge: its one value read against `target` (the maximum),
 * `100` for a percent dial with no target named. Clamped to the dial.
 */
export function gaugeReading(block: Pick<DeckChartBlock, "series" | "target" | "format">): {
  value: number;
  max: number;
  ratio: number;
} {
  const value = block.series[0]?.values[0] ?? 0;
  const declared = block.target ?? (block.format === "percent" ? 100 : undefined);
  const max = declared !== undefined && declared > 0 ? declared : Math.max(value, 1);
  const ratio = Math.min(Math.max(value / max, 0), 1);
  return { value, max, ratio };
}

/* ── Map ───────────────────────────────────────────────────────────── */

/**
 * The share of its series' colour the lowest value of a map is drawn in, in
 * percent (the highest takes all of it); the rest is the paper the map is
 * drawn on (`--deck-map-paper`), so the faintest country with a value still
 * reads apart from those without one (`MAP_EMPTY_FILL`).
 */
export const MAP_RAMP_FLOOR = 30;
/** The fill of a country without a value: the muted surface, a step off the paper (`--deck-map-empty`). */
export const MAP_EMPTY_FILL = "var(--deck-map-empty)";

/** The ends of a map's ramp: its lowest and its highest value - from zero when every country has one value. */
export function mapRampDomain(block: Pick<DeckChartBlock, "series">): { min: number; max: number } {
  const values = (block.series[0]?.values ?? []).filter(Number.isFinite);
  if (!values.length) return { min: 0, max: 0 };
  const max = Math.max(...values);
  const lowest = Math.min(...values);
  return { min: lowest === max ? Math.min(0, lowest) : lowest, max };
}

/** Where `value` sits on the ramp: 0 at its lowest end, 1 at its highest - and 1 on a ramp of one value. */
export function mapRampPosition(value: number, { min, max }: { min: number; max: number }): number {
  if (!(max > min)) return 1;
  return Math.min(1, Math.max(0, (value - min) / (max - min)));
}

/** The colour of a ramp position: its share of `end` (the series' colour) mixed into the paper. */
export function mapRampColor(position: number, end: string): string {
  const share = Math.round((MAP_RAMP_FLOOR + (100 - MAP_RAMP_FLOOR) * position) * 10) / 10;
  return `color-mix(in oklab, ${end} ${share}%, var(--deck-map-paper))`;
}

/**
 * How a map writes the values on its countries and its ramp: compact once its
 * largest value reaches `AXIS_COMPACT_FROM` (`2.2M users`), as the labels of
 * an axis chart do; the tooltip keeps every digit.
 */
export function mapValueSurface(block: Pick<DeckChartBlock, "series">): ValueSurface {
  const extent = Math.max(0, ...(block.series[0]?.values ?? []).map(Math.abs));
  return axisSurface(extent) === "axis_compact" ? "label_compact" : "label";
}

/** What a map's legend shows: its series' name, the two ends of its ramp as the chart writes a value, and their colours. */
export function mapRampLegend(
  block: DeckChartBlock,
  locale: string,
): { name: string; low: string; high: string; from: string; to: string } {
  const domain = mapRampDomain(block);
  const write = chartValueWriter(block, writtenValues(block), locale, mapValueSurface(block));
  const end = seriesColors(block.series)[0];
  return {
    name: block.series[0]?.name ?? "",
    low: write(domain.min),
    high: write(domain.max),
    from: mapRampColor(0, end),
    to: mapRampColor(1, end),
  };
}

/**
 * How a map colours its countries and draws its legend: `classes` -
 * flat steps of its ramp, listed beside the plot - when the block says so,
 * the continuous ramp otherwise, and on any other kind.
 */
export function mapLegendStyle(block: Pick<DeckChartBlock, "kind" | "legend_style">): DeckMapLegendStyle {
  return block.kind === "map" && block.legend_style === "classes" ? "classes" : "ramp";
}

/** Whether a map dots the countries drawn too small to see: unless the block says `markers: false`. */
export function mapMarkersShown(block: Pick<DeckChartBlock, "markers">): boolean {
  return block.markers !== false;
}

/**
 * How many classes a classed map draws when it names no count, and the
 * counts it may name. Mirrored by `MAP_DEFAULT_CLASSES` and
 * `MapClassCount` in `presentation_spec.py` (pinned by a test). Never more
 * classes than the map has distinct values.
 * @public
 */
export const MAP_DEFAULT_CLASSES = 5;
/** @public */
export const MAP_MIN_CLASSES = 2;
/** @public */
export const MAP_MAX_CLASSES = 7;

/**
 * Natural breaks (Fisher-Jenks, exact): `sorted` - ascending, distinct - cut
 * into at most `count` runs whose summed squared deviations from their own
 * means is the least. Returns the index each run starts at, the first 0. A
 * dynamic programme over the cut points, `count x n^2` steps on at most the
 * 249 values of a map; the values are centred first, so two close figures in
 * the billions keep their difference. A tie keeps the earliest cut.
 */
export function naturalBreaks(sorted: readonly number[], count: number): number[] {
  const n = sorted.length;
  const runs = Math.max(1, Math.min(Math.floor(count), n));
  if (n === 0) return [];
  const mean = sorted.reduce((total, value) => total + value, 0) / n;
  const sums = [0];
  const squares = [0];
  for (const value of sorted) {
    const centred = value - mean;
    sums.push(sums[sums.length - 1] + centred);
    squares.push(squares[squares.length - 1] + centred * centred);
  }
  // The squared deviations of `sorted[from..to]`, both ends included.
  const spread = (from: number, to: number): number => {
    const size = to - from + 1;
    const total = sums[to + 1] - sums[from];
    return Math.max(0, squares[to + 1] - squares[from] - (total * total) / size);
  };
  // cost[r][to]: the least spread of `sorted[0..to]` in r + 1 runs; start[r][to]: where its last run starts.
  const cost: number[][] = [Array.from({ length: n }, (_, to) => spread(0, to))];
  const start: number[][] = [Array.from({ length: n }, () => 0)];
  for (let run = 1; run < runs; run += 1) {
    cost.push(Array.from({ length: n }, () => Infinity));
    start.push(Array.from({ length: n }, () => run));
    for (let to = run; to < n; to += 1) {
      for (let from = run; from <= to; from += 1) {
        const total = cost[run - 1][from - 1] + spread(from, to);
        if (total < cost[run][to]) {
          cost[run][to] = total;
          start[run][to] = from;
        }
      }
    }
  }
  const starts: number[] = [];
  for (let run = runs - 1, to = n - 1; run >= 0; run -= 1) {
    const from = start[run][to];
    starts.unshift(from);
    to = from - 1;
  }
  return starts;
}

/** One class of a classed map: the values it holds, how its legend row reads, its flat colour. */
interface MapClass {
  min: number;
  max: number;
  /** `120 +` for the top class, `43` for a class of one value, `12 - 39` for the rest. */
  label: string;
  color: string;
}

/**
 * The classes of a classed map, lowest first: its distinct values cut
 * by natural breaks into the block's `classes` (2-7), else up to
 * `MAP_DEFAULT_CLASSES`, never more than it has distinct values. Each class
 * takes one flat step of the map's ramp - the floor's wash for the lowest,
 * the series' colour for the highest - and is written as the chart writes a
 * value. The top class is open-ended (`120 +`) even when it holds one value.
 */
export function mapClasses(block: DeckChartBlock, locale: string): MapClass[] {
  const distinct = [...new Set((block.series[0]?.values ?? []).filter(Number.isFinite))].sort((a, b) => a - b);
  const named = block.classes;
  const asked =
    typeof named === "number" && Number.isInteger(named) && named >= MAP_MIN_CLASSES && named <= MAP_MAX_CLASSES
      ? named
      : MAP_DEFAULT_CLASSES;
  const starts = naturalBreaks(distinct, asked);
  const write = chartValueWriter(block, writtenValues(block), locale, mapValueSurface(block));
  const end = seriesColors(block.series)[0];
  const count = starts.length;
  return starts.map((from, index) => {
    const min = distinct[from];
    const max = distinct[index + 1 < count ? starts[index + 1] - 1 : distinct.length - 1];
    const top = count > 1 && index === count - 1;
    const label = top ? `${write(min)} +` : min === max ? write(min) : `${write(min)} - ${write(max)}`;
    return { min, max, label, color: mapRampColor(count > 1 ? index / (count - 1) : 1, end) };
  });
}

/** The class a value falls in: the highest whose lowest value it reaches (the lowest for anything under it). */
export function mapClassOf(classes: readonly MapClass[], value: number): MapClass | undefined {
  let found = classes[0];
  for (const entry of classes) if (value >= entry.min) found = entry;
  return found;
}

/**
 * What the figure PUBLISHES for the exporter (`data-deck-chart`): the
 * chart as data — kind, categories, series with the CSS colour expression
 * each one is drawn in, how a value is written, what is stacked, shown and
 * targeted — so the PPTX writer rebuilds it as an editable PowerPoint chart
 * from the same numbers the viewer plotted, instead of reading bars off a
 * picture. Colour expressions are resolved to pixels by the extractor, in the
 * theme the page was rendered in; the layout-dependent facts (the axis the
 * drawing settled on, whether values fit) ride the `.deck-chart-facts`
 * element the drawing renders, because they are only known once it has a size.
 * A waterfall publishes the bars it DREW (`steps`, the gap bars of
 * `allow_gap` included, named `gapLabel`), so the writer never sums a bridge.
 * A two-unit chart says which series read against the right-hand axis
 * and how that axis reads, so the writer gives it a second value axis. The
 * text scale (`label_size`) is the writer's fallback where the page
 * published no measured size. A series whose values stay off while the
 * chart writes others (its own `show_values`) says so, and keeps no label.
 * The side the legend sits on (`legend_position`) is published as
 * written - `null` when unset - so the writer puts PowerPoint's legend on the
 * same side and keeps its historical rule for the default. An axis that pins
 * its currency amounts to one letter (`unit_scale`) publishes it, and
 * only then: a chart without one publishes what it always did. So does the
 * size its values are tried at first (`value_label_size`), as written,
 * on the kinds it acts on: the writer's fallback for the data labels, where
 * `label_size` stays the ticks'. A butterfly publishes its sizes as
 * given (the writer stores the left side negative), itself as a horizontal
 * chart and where it wrote its categories (`spine`), and nothing more.
 * A map says which part of the world it draws; the extractor pastes
 * it as a picture, PowerPoint having no map.
 */
export function chartExportPayload(block: DeckChartBlock, locale: string, gapLabel: string): string {
  const single = isSingleSeriesKind(block.kind);
  const mirrored = isMirroredKind(block.kind);
  const colors = seriesColors(block.series);
  const steps = block.kind === "waterfall" ? waterfallSteps(block, gapLabel) : null;
  const tone = block.series[0]?.tone;
  const twoAxes = hasSecondaryAxis(block);
  const secondary = axisReading(block, "secondary");
  const someValues = anyValuesAsked(block);
  const primaryScale = pinnedTier({ ...axisReading(block, "primary"), kind: block.kind })?.[1];
  const secondaryScale = twoAxes ? pinnedTier({ ...secondary, kind: block.kind })?.[1] : undefined;
  const valueSize = valueLabelSize(block);
  const payload = {
    kind: block.kind,
    categories: steps ? steps.map((step) => step.category) : block.categories,
    series: steps
      ? [{ name: block.series[0]?.name ?? "", values: steps.map((step) => step.value), color: waterfallColor("total", tone) }]
      : block.series.map((entry, index) => ({
          name: entry.name,
          values: entry.values,
          color: colors[index],
          ...(entry.kind ? { kind: entry.kind } : {}),
          ...(twoAxes && seriesAxis(entry) === "secondary" ? { axis: "secondary" } : {}),
          ...(someValues && !seriesValuesAsked(block, index) ? { show_values: false } : {}),
        })),
    ...(steps
      ? {
          steps: steps.map((step) => ({
            start: step.start,
            end: step.end,
            value: step.value,
            role: step.role,
            color: waterfallColor(step.role, tone),
          })),
        }
      : {}),
    slice_colors: single ? block.categories.map((_, index) => paletteColor(index)) : [],
    format: block.format ?? "number",
    unit: block.unit ?? null,
    decimals: block.decimals ?? null,
    ...(primaryScale ? { unit_scale: primaryScale } : {}),
    stacked: isStacked(block),
    horizontal: !single && (block.horizontal === true || mirrored),
    ...(mirrored ? { spine: butterflySpine(block) } : {}),
    legend: legendShown(block),
    legend_position: block.legend_position ?? null,
    values: valuesShown(block),
    target: hasTargetLine(block) ? (block.target as number) : null,
    target_name: targetLegendName(block, locale),
    secondary_format: twoAxes ? (secondary.format ?? "number") : null,
    secondary_unit: twoAxes ? (secondary.unit ?? null) : null,
    secondary_decimals: twoAxes ? (secondary.decimals ?? null) : null,
    ...(secondaryScale ? { secondary_unit_scale: secondaryScale } : {}),
    secondary_target: hasSecondaryTargetLine(block) ? (block.secondary_target as number) : null,
    secondary_target_name: secondaryTargetLegendName(block, locale),
    label_size: block.label_size ?? "md",
    ...(valueSize !== undefined ? { value_label_size: valueSize } : {}),
    ...(block.kind === "map" ? { region: block.region ?? "world" } : {}),
    locale,
  };
  return JSON.stringify(payload);
}
