// SPDX-License-Identifier: Apache-2.0

/**
 * D3-2 of the CTEM open-source program (#9618): the period of the stage score
 * trend on the stage page. Pure and deterministic: it reads the recorded days of
 * `GET /ctem-metrics/posture/history` (one snapshot per day, up to 90 days) and
 * never calls a model.
 */

/** The periods the History card offers, in days. */
export const TREND_PERIODS = [30, 90] as const;
export type TrendPeriod = (typeof TREND_PERIODS)[number];

const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * A recorded day ("2026-10-08") as a short date without the year in the app
 * language ("Oct 8" in en, "8 oct." in fr). The day is read and formatted in
 * UTC so a browser west of UTC never shows the day before. An unreadable date
 * is shown as it is rather than guessed.
 */
export function formatTrendDay(
  intl: { formatDate: (value: number, options: Intl.DateTimeFormatOptions) => string },
  date: string,
): string {
  const at = Date.parse(`${date.slice(0, 10)}T00:00:00Z`);
  if (!Number.isFinite(at)) return date;
  return intl.formatDate(at, { day: "numeric", month: "short", timeZone: "UTC" });
}

/**
 * The recorded days within the last `days` days before `today` (inclusive of
 * the boundary day), oldest first, as the endpoint already orders them. A day
 * whose date cannot be read is left out rather than guessed.
 */
export function historyWindow<T extends { date: string }>(history: readonly T[], days: number, today: Date): T[] {
  const end = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  const start = end - days * DAY_MS;
  return history.filter((point) => {
    const at = Date.parse(`${point.date.slice(0, 10)}T00:00:00Z`);
    return Number.isFinite(at) && at >= start && at <= end;
  });
}

/** The live posture against the start of its period (C2 V1, UX r1 B4). */
interface PeriodChange {
  /** First recorded score of the period. */
  start: number;
  /** Live score minus the start, rounded to whole points. */
  change: number;
}

/**
 * The one period delta of the posture, read by the command center hero and by
 * the value band so the page never shows two figures for one trend: the live
 * score against the first recorded day of the history. Null without a live
 * score or without two recorded days (one day is no trend).
 */
export function periodChange(current: number | null, history: readonly (number | null)[]): PeriodChange | null {
  const recorded = history.filter((value): value is number => value !== null && Number.isFinite(value));
  if (current === null || !Number.isFinite(current) || recorded.length < 2) return null;
  const start = recorded[0] as number;
  return { start, change: Math.round(current - start) };
}

/** The change of a series over the period and the day it starts from. */
export interface SeriesChange {
  /** Last recorded value minus the first, rounded; null without two values. */
  change: number | null;
  /** Index of the first recorded value, the start of the change; null without one. */
  fromIndex: number | null;
}

/**
 * The change between the first and the last recorded value of a series. Two
 * values at least, on different days: one day is no trend, and a missing day
 * is never read as 0.
 */
export function seriesChange(values: readonly (number | null)[]): SeriesChange {
  let first = -1;
  let last = -1;
  values.forEach((value, index) => {
    if (value === null || !Number.isFinite(value)) return;
    if (first < 0) first = index;
    last = index;
  });
  if (first < 0) return { change: null, fromIndex: null };
  if (last === first) return { change: null, fromIndex: first };
  return { change: Math.round((values[last] as number) - (values[first] as number)), fromIndex: first };
}
