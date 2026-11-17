// SPDX-License-Identifier: Apache-2.0

/**
 * How LLM Analytics writes its figures: one copy for the KPIs,
 * their trends, the charts (labels, axes, tooltips, legends), the rankings and
 * the tables. Every amount and count is written on the same compact scale
 * (k / M / B); the CSV export keeps the raw figures.
 */
import { formatCompactCost, formatCompactNumber } from "@/lib/utils";
import type { TimeBucket } from "@/pages/analytics/chart-data";

/** A cost: `$12.50`, `$752`, `$61.2k`, `$3.4M`. */
export const formatUsd = (value: number): string => formatCompactCost(value);

export function formatLatency(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  if (value < 1000) return `${Math.round(value)}ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(1)}s`;
  return `${(value / 60_000).toFixed(1)}m`;
}

export const formatPercent = (value: number): string => `${value.toFixed(1)}%`;

export function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined || Number.isNaN(value) ? "-" : value.toFixed(2);
}

/** A count (traces, tokens): `999`, `110.6k`, `36.5B`. A stable identity, so `AnimatedNumber` does not restart on every render. */
export const formatCount = (value: number): string => formatCompactNumber(Math.round(value));

/** A bucket on a time axis: its day (a week by its Monday), or its month. Read in UTC, like the rollup's days. */
export function formatBucketLabel(date: string, bucket: TimeBucket): string {
  const day = new Date(`${date.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(day.getTime())) return date;
  return bucket === "month"
    ? day.toLocaleDateString(undefined, { month: "short", year: "numeric", timeZone: "UTC" })
    : day.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}
