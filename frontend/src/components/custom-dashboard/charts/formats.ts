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
 * How the engine writes numbers, dates and durations, and how a figure
 * counts up: the formats the modules' own widgets use
 * (`dev-docs/dashboard-rendering-reference.md`), named for what they do, so a
 * presentation preset can ask for one by name.
 *
 * Pure, with no import: runs on `node --test`.
 */

/** What a missing value is written as. */
const EMPTY_VALUE = "-";

const SI: readonly [number, string][] = [
  [1e18, "E"],
  [1e15, "P"],
  [1e12, "T"],
  [1e9, "G"],
  [1e6, "M"],
  [1e3, "K"],
];
const TRAILING_ZEROS = /\.0+$|(\.\d*[1-9])0+$/;

/** A figure scaled to its SI symbol (`1500` → 1.5 and `K`), rounded to `digits` decimals, trailing zeros dropped.
 *  Only a positive figure is scaled: a negative one keeps its sign and is written whole (see `signed`). */
export function siParts(value: number, digits = 2): { number: number; symbol: string } {
  const [scale, symbol] = SI.find(([at]) => value >= at) ?? [1, ""];
  return { number: Number.parseFloat((value / scale).toFixed(digits).replace(TRAILING_ZEROS, "$1")), symbol };
}

export interface SiFormat {
  /** Decimals kept once scaled (2 by default). */
  digits?: number;
  /** A space between the figure and its symbol (`1.5 K`). */
  space?: boolean;
  /** A negative figure is scaled too, its sign kept (`-1.5K`). */
  signed?: boolean;
  /** The scaled figure is written in this locale (`1,5K` in French); without one, as JavaScript writes it. */
  locale?: string;
}

/** A figure in SI form (`1.5K`, `1.5 K`, `42`), or `-` when there is none. */
export function formatSi(value: number | null | undefined, format: SiFormat = {}): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EMPTY_VALUE;
  const negative = format.signed && value < 0;
  const { number, symbol } = siParts(negative ? -value : value, format.digits ?? 2);
  const written = format.locale ? new Intl.NumberFormat(format.locale).format(number) : String(number);
  return `${negative ? "-" : ""}${written}${symbol && format.space ? " " : ""}${symbol}`;
}

/** The named number formats a preset may ask for. */
export const NUMBER_FORMATS = ["si", "siSpaced", "compact", "locale"] as const;
export type NumberFormatName = (typeof NUMBER_FORMATS)[number];

/** A figure in a named format: `si` (`1.5K`, in the locale), `siSpaced` (`1.5 K`), `compact`
 *  (`1.5K`, one decimal, a negative figure scaled too) or `locale` (`1,500`). */
export function formatNamedNumber(value: number | null | undefined, name: NumberFormatName, locale?: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EMPTY_VALUE;
  switch (name) {
    case "si":
      return formatSi(value, { locale });
    case "siSpaced":
      return formatSi(value, { space: true });
    case "compact":
      return formatSi(value, { digits: 1, signed: true });
    case "locale":
      return value.toLocaleString(locale);
  }
}

// ── Dates ───────────────────────────────────────────────────────────────────

/** The named date formats a preset may ask for. */
export type DateFormatName = "date" | "longDate" | "longDateTime" | "dateTime";

const DATE_OPTIONS: Record<DateFormatName, Intl.DateTimeFormatOptions> = {
  // 1 Oct 2026
  date: { day: "numeric", month: "short", year: "numeric" },
  // 1 October 2026
  longDate: { day: "numeric", month: "long", year: "numeric" },
  // 1 October 2026 at 10:00:00
  longDateTime: { day: "numeric", month: "long", year: "numeric", hour: "numeric", minute: "numeric", second: "numeric" },
  // 1 Oct 2026, 10:00:00
  dateTime: { day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "numeric", second: "numeric" },
};

const asDate = (value: string | number | Date | null | undefined): Date | null => {
  if (value === null || value === undefined || value === "") return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
};

/** A moment in a named format, in the viewer's time zone unless one is given; `-` when it is none. */
export function formatNamedDate(value: string | number | Date | null | undefined, name: DateFormatName, locale?: string, timeZone?: string): string {
  const date = asDate(value);
  return date ? new Intl.DateTimeFormat(locale, { ...DATE_OPTIONS[name], timeZone }).format(date) : EMPTY_VALUE;
}

/** The interval a time axis is bucketed by. */
export type AxisInterval = "minute" | "hour" | "day" | "week" | "month" | "quarter" | "year";

/** How a time axis writes its ticks for an interval, in UTC: `January 2026` by month or quarter,
 *  `2026` by year, `1 Jan 2026` otherwise. */
export function axisDateOptions(interval: AxisInterval | undefined): Intl.DateTimeFormatOptions {
  if (interval === "month" || interval === "quarter") return { month: "long", year: "numeric", timeZone: "UTC" };
  if (interval === "year") return { year: "numeric", timeZone: "UTC" };
  return { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" };
}

export function formatAxisDate(value: string | number | Date | null | undefined, interval: AxisInterval | undefined, locale?: string): string {
  const date = asDate(value);
  return date ? new Intl.DateTimeFormat(locale, axisDateOptions(interval)).format(date) : EMPTY_VALUE;
}

/** A duration in milliseconds: `45s`, `3min`, `3min 20s`, `2h`, `2h 15min`. */
export function formatDuration(milliseconds: number): string {
  const seconds = Math.max(0, milliseconds) / 1000;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds % 60);
    return rest === 0 ? `${minutes}min` : `${minutes}min ${rest}s`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return minutes === 0 ? `${hours}h` : `${hours}h ${minutes}min`;
}

// ── Count-up ────────────────────────────────────────────────────────────────

/** How long a figure takes to count up to a new value. */
export const COUNT_UP_MS = 1200;

/** The figure shown `elapsed` ms into counting from `from` to `to`, easing out (cubic). */
export function countUpValue(from: number, to: number, elapsed: number, duration = COUNT_UP_MS): number {
  if (duration <= 0 || elapsed >= duration) return to;
  const progress = Math.max(0, elapsed) / duration;
  return from + (to - from) * (1 - (1 - progress) ** 3);
}
