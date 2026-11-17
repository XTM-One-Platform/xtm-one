// SPDX-License-Identifier: Apache-2.0

import type {
  TraceAnalyticsCostOverTimeResponse,
  TraceAnalyticsPeriod,
  TraceAnalyticsVolumeOverTimeResponse,
} from "@/types";

interface StackedSeriesRow {
  date: string;
  [key: string]: string | number;
}

/** The key under which a row carries its stack total, drawn above the bar. */
export const TOTAL_KEY = "__total";
/** The series the smallest stack keys are folded into. */
export const OTHER_KEY = "__other";

/**
 * Ensure every row carries every stack key (default 0). Recharts `<Area>` reads
 * each `dataKey` per row; a missing key yields `undefined`, which breaks the
 * stack and leaves gaps on sparse days. Zero-filling keeps each series
 * continuous while preserving the `date` field.
 */
function zeroFillStackRows(rows: StackedSeriesRow[]): StackedSeriesRow[] {
  const keys = new Set<string>();
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (key !== "date") keys.add(key);
    }
  }
  return rows.map((row) => {
    const filled: StackedSeriesRow = { ...row };
    for (const key of keys) {
      if (filled[key] === undefined) filled[key] = 0;
    }
    return filled;
  });
}

export function pivotCostSeries(series: TraceAnalyticsCostOverTimeResponse["series"]): StackedSeriesRow[] {
  const dates = new Map<string, Record<string, number>>();
  for (const item of series) {
    const existing = dates.get(item.date) ?? {};
    existing[item.model] = (existing[item.model] ?? 0) + item.cost;
    dates.set(item.date, existing);
  }

  const rows = Array.from(dates.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, values]) => ({ date, ...values }));
  return zeroFillStackRows(rows);
}

export function pivotVolumeSeries(series: TraceAnalyticsVolumeOverTimeResponse["series"]): StackedSeriesRow[] {
  const dates = new Map<string, Record<string, number>>();
  for (const item of series) {
    const existing = dates.get(item.date) ?? {};
    existing[item.source] = (existing[item.source] ?? 0) + item.count;
    dates.set(item.date, existing);
  }

  const rows = Array.from(dates.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, values]) => ({ date, ...values }));
  return zeroFillStackRows(rows);
}

export function getStackKeys(rows: StackedSeriesRow[]): string[] {
  const keys = new Set<string>();
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (key !== "date" && key !== TOTAL_KEY) keys.add(key);
    }
  }
  return Array.from(keys).sort((a, b) => a.localeCompare(b));
}

export type TimeBucket = "day" | "week" | "month";

/** The first day of *date*'s bucket, as `YYYY-MM-DD` (weeks start on Monday, UTC like the rollup). */
export function bucketStart(date: string, bucket: TimeBucket): string {
  if (bucket === "day") return date;
  if (bucket === "month") return `${date.slice(0, 7)}-01`;
  const day = new Date(`${date}T00:00:00Z`);
  day.setUTCDate(day.getUTCDate() - ((day.getUTCDay() + 6) % 7));
  return day.toISOString().slice(0, 10);
}

/** *rows* summed per bucket, every key kept. */
export function bucketRows(rows: StackedSeriesRow[], bucket: TimeBucket): StackedSeriesRow[] {
  if (bucket === "day") return rows;
  const buckets = new Map<string, StackedSeriesRow>();
  for (const row of rows) {
    const key = bucketStart(row.date, bucket);
    const target = buckets.get(key) ?? { date: key };
    for (const [name, value] of Object.entries(row)) {
      if (name === "date" || typeof value !== "number") continue;
      target[name] = (Number(target[name]) || 0) + value;
    }
    buckets.set(key, target);
  }
  return zeroFillStackRows(Array.from(buckets.values()).sort((a, b) => a.date.localeCompare(b.date)));
}

/** Each row with its stack total under `TOTAL_KEY`, the figure written over the top of its stack. */
export function stackTotals(rows: StackedSeriesRow[], keys: string[]): StackedSeriesRow[] {
  return rows.map((row) => ({ ...row, [TOTAL_KEY]: keys.reduce((sum, key) => sum + (Number(row[key]) || 0), 0) }));
}

/** The sum of each stack key over *rows*. */
export function keyTotals(rows: StackedSeriesRow[], keys: string[]): Record<string, number> {
  const totals: Record<string, number> = {};
  for (const key of keys) totals[key] = 0;
  for (const row of rows) for (const key of keys) totals[key] += Number(row[key]) || 0;
  return totals;
}

/**
 * The *keep* largest stack keys, largest first, the others summed into
 * `OTHER_KEY` - a stack of twenty models reads as noise - and each row's
 * total under `TOTAL_KEY`, the figure drawn above its bar.
 */
export function foldStack(rows: StackedSeriesRow[], keep: number): { rows: StackedSeriesRow[]; keys: string[] } {
  const keys = getStackKeys(rows);
  const totals = keyTotals(rows, keys);
  const ranked = [...keys].sort((a, b) => totals[b] - totals[a] || a.localeCompare(b));
  const kept = ranked.slice(0, Math.max(1, keep));
  const folded = ranked.slice(kept.length);
  const out = rows.map((row) => {
    const next: StackedSeriesRow = { date: row.date };
    let total = 0;
    for (const key of kept) {
      const value = Number(row[key]) || 0;
      next[key] = value;
      total += value;
    }
    if (folded.length > 0) {
      const other = folded.reduce((sum, key) => sum + (Number(row[key]) || 0), 0);
      next[OTHER_KEY] = other;
      total += other;
    }
    next[TOTAL_KEY] = total;
    return next;
  });
  return { rows: out, keys: folded.length > 0 ? [...kept, OTHER_KEY] : kept };
}

const PERIOD_DAYS: Record<Exclude<TraceAnalyticsPeriod, "custom">, number> = {
  "24h": 1,
  "7d": 7,
  "30d": 30,
  "90d": 90,
  "180d": 180,
  "1y": 365,
};

/** How many days a window spans: a custom one from its bounds. */
export function windowDays(period: TraceAnalyticsPeriod, dateFrom?: string, dateTo?: string): number {
  if (period !== "custom") return PERIOD_DAYS[period];
  const start = Date.parse(dateFrom ?? "");
  const end = Date.parse(dateTo ?? "");
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return 30;
  return Math.max(1, Math.round((end - start) / 86_400_000));
}

/** The bucket a window opens with: a figure per bar stays readable (31 bars at most). */
export function defaultBucket(days: number): TimeBucket {
  if (days <= 31) return "day";
  if (days <= 200) return "week";
  return "month";
}

/**
 * The stack key a row's total is written over: the last drawn key with a
 * value, so the total stays on the top of the stack even when the topmost
 * series is at zero for that bucket. `undefined` for an empty bucket (no bar).
 */
export function labelKey(row: Record<string, string | number>, keys: string[]): string | undefined {
  for (let index = keys.length - 1; index >= 0; index -= 1) {
    if ((Number(row[keys[index]]) || 0) > 0) return keys[index];
  }
  return undefined;
}

/** Up to this many bars the chart never scrolls: each bar gets at least a flat figure's room on any card. */
const NO_FLOOR_LABELS = 12;
/** The width the chart's axis and margins take beside the bars. */
const AXIS_WIDTH = 64;
/** The figure's normal size, and the size a step smaller. */
const NORMAL_SIZE = 10;
const SMALL_SIZE = 9;
/** The width of one character of a tabular figure, in em (digits, `$`, `.`, `k`, `s`). */
const CHAR_EM = 0.6;
/** The clear space kept between two neighbouring flat figures. */
const LABEL_GAP = 4;
/** The line box a small figure renders in: the font size times the 1.2 line height, 11 px at 9 px. */
const SMALL_LINE_BOX = Math.ceil(SMALL_SIZE * 1.2);
/**
 * The slot a diagonal (45 degree) figure needs: two neighbouring figures stay one line box
 * apart when the slot times sin 45 is at least the rendered line box, so the box x 1.42
 * and a pixel of air: 17 px a bucket at 9 px.
 */
const DIAGONAL_SLOT = Math.ceil(SMALL_LINE_BOX * Math.SQRT2) + 1;
/** Room above the tallest bar for a flat figure, and the most a diagonal one may take. */
const FLAT_TOP = 20;
const MAX_DIAGONAL_TOP = 56;
/** The plot width assumed before the chart is measured (first paint, tests without layout). */
const UNMEASURED_PLOT_WIDTH = 800;

/**
 * How the figures over the bars are written, from the most to the least
 * readable; never turned 90 degrees:
 * - `flat`: level, centred over its bar;
 * - `stagger`: level, every other figure one line higher, so two neighbours never touch;
 * - `diagonal`: 45 degrees, anchored at the top of its bar;
 * - `hidden`: no figure, the tooltip keeps the value.
 */
type ValueLabelMode = "flat" | "stagger" | "diagonal" | "hidden";

export interface ValueLabelLayout {
  mode: ValueLabelMode;
  fontSize: number;
  /** The bucket count and the width each bucket has, for the placement of the edge figures. */
  count: number;
  slot: number;
  /** The right edge of the chart's drawing box in px, the last figure ends at or before it (0: not measured). */
  plotRight: number;
  /** Room above the tallest bar for its figure. */
  topMargin: number;
  /** Extra room right of the last bar, for a diagonal figure leaning past it. */
  rightMargin: number;
  /** The width under which the chart scrolls rather than crowd its figures (0: no floor). */
  minWidth: number;
}

export interface ValueLabelRoom {
  /** The plot's measured width in px (0 or absent: not measured yet). */
  plotWidth?: number;
  /** The character count of the longest figure of the series. */
  longest?: number;
  /** The width of the chart's whole drawing box in px, padding included (0 or absent: not measured). */
  boxWidth?: number;
}

/** The width a figure of *chars* characters takes at *size* px, its clear space included. */
function figureWidth(chars: number, size: number): number {
  return chars * size * CHAR_EM + LABEL_GAP;
}

/** The character count of the longest of *figures* (the formatted values written over the bars). */
export function longestFigure(figures: Iterable<string>): number {
  let longest = 0;
  for (const figure of figures) longest = Math.max(longest, figure.length);
  return longest;
}

/**
 * How the figure over each of *count* bars (or line points) is written. The decision
 * comes from the room each bucket really has (plot width / count) and the longest figure,
 * never from the bucket count alone: flat at the normal size when it fits; flat a size
 * smaller; staggered on two alternating rows; diagonal as the last resort; hidden when
 * even a diagonal figure does not fit. Past a dozen buckets the plot keeps a floor wide
 * enough for a diagonal figure per bucket and scrolls on a narrower card.
 */
export function valueLabelLayout(count: number, room: ValueLabelRoom = {}): ValueLabelLayout {
  const minWidth = count > NO_FLOOR_LABELS ? count * DIAGONAL_SLOT + AXIS_WIDTH : 0;
  const plotWidth = room.plotWidth && room.plotWidth > 0 ? room.plotWidth : Math.max(UNMEASURED_PLOT_WIDTH, minWidth);
  const slot = count > 0 ? plotWidth / count : plotWidth;
  const chars = Math.max(1, room.longest ?? 4);
  const plotRight = room.boxWidth && room.boxWidth > 0 ? room.boxWidth : 0;
  const flat = { rightMargin: 0, minWidth, count, slot, plotRight };
  if (figureWidth(chars, NORMAL_SIZE) <= slot) return { mode: "flat", fontSize: NORMAL_SIZE, topMargin: FLAT_TOP, ...flat };
  if (figureWidth(chars, SMALL_SIZE) <= slot) return { mode: "flat", fontSize: SMALL_SIZE, topMargin: FLAT_TOP, ...flat };
  if (figureWidth(chars, SMALL_SIZE) <= STAGGER_SPAN * slot) {
    return { mode: "stagger", fontSize: SMALL_SIZE, topMargin: FLAT_TOP + STAGGER_ROW, ...flat };
  }
  // The figure leans up and right from the shared baseline: it rises and reaches right by its length x sin 45.
  const lean = Math.ceil((chars * SMALL_SIZE * CHAR_EM + SMALL_SIZE) * Math.SQRT1_2);
  if (slot >= DIAGONAL_SLOT && lean + 8 <= MAX_DIAGONAL_TOP) {
    return { mode: "diagonal", fontSize: SMALL_SIZE, topMargin: lean + 8, rightMargin: lean, minWidth, count, slot, plotRight };
  }
  return { mode: "hidden", fontSize: SMALL_SIZE, topMargin: 8, rightMargin: 0, minWidth, count, slot, plotRight };
}

/**
 * How far apart two figures of the same staggered row may be, in buckets. Same-row neighbours are two
 * buckets apart, and the first and last figures move inward by up to half a bucket to stay inside the
 * plot: 2 - 1/3 leaves a figure room for both.
 */
const STAGGER_SPAN = 5 / 3;
/** The height of a staggered row: the small size and two pixels of leading. */
const STAGGER_ROW = SMALL_SIZE + 2;
/** The gap between the top of a bar (or a point), or the shared baseline, and its figure. */
const VALUE_LABEL_GAP = 4;
/** The angle of a diagonal figure: reading up and to the right. */
const DIAGONAL_ANGLE = -45;

/** Where one figure is written: its anchor point, its text anchor and its angle. */
export interface ValueLabelPlacement {
  x: number;
  y: number;
  anchor: "start" | "middle" | "end";
  angle: number;
}

/** The bar (or line point) a figure is written for: its box in the chart, its index, its figure. */
export interface ValueLabelTarget {
  x: number;
  y: number;
  width: number;
  index: number;
  text: string;
}

/**
 * Where the figure over a bar is written. A flat figure sits over its own bar. A
 * staggered or diagonal figure never sits on its own bar's top, since two neighbouring bars of
 * different heights would then bring their figures together again: every staggered figure sits on one
 * of two rows shared by the whole chart, and every diagonal figure on one baseline shared by the whole
 * chart, both in the room the layout keeps above the plot (`topMargin`, so above the tallest bar in
 * view). Same-row figures are then always as far apart as their buckets, and no figure ever crosses a
 * bar. The first and last staggered figures move inward to stay inside the plot: by up to half a
 * bucket when wider than their bucket, and never past the drawing box's left edge (0) or right edge
 * (`plotRight`), whatever the rounding of the bar positions.
 */
export function placeValueLabel(layout: ValueLabelLayout, target: ValueLabelTarget): ValueLabelPlacement | null {
  if (layout.mode === "hidden" || !target.text) return null;
  const centre = target.x + target.width / 2;
  if (layout.mode === "flat") return { x: centre, y: target.y - VALUE_LABEL_GAP, anchor: "middle", angle: 0 };
  const baseline = layout.topMargin - VALUE_LABEL_GAP;
  if (layout.mode === "diagonal") return { x: centre, y: baseline, anchor: "start", angle: DIAGONAL_ANGLE };
  const y = target.index % 2 === 1 ? baseline - STAGGER_ROW : baseline;
  const half = (target.text.length * layout.fontSize * CHAR_EM) / 2;
  const reach = Math.min(half, layout.slot / 2);
  if (target.index === 0) {
    const left = Math.max(0, centre - reach);
    if (left > centre - half) return { x: left, y, anchor: "start", angle: 0 };
  }
  if (target.index === layout.count - 1) {
    const right = Math.min(layout.plotRight > 0 ? layout.plotRight : Infinity, centre + reach);
    if (right < centre + half) return { x: right, y, anchor: "end", angle: 0 };
  }
  return { x: centre, y, anchor: "middle", angle: 0 };
}
