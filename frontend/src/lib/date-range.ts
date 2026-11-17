// SPDX-License-Identifier: Apache-2.0

/**
 * The time range of a list page's filter row: a preset counted back
 * from now ("Last 7 days"), or a custom From / To day. A page keeps it in its
 * URL as three strings and sends the server the instants it resolves to,
 * `date_from` / `date_to`. Rendered by `components/ui/date-range-filter.tsx`;
 * the audit logs and the Video Studio's media library read it.
 */

/** The presets, by key. @i18n-keys */
export const DATE_RANGE_PRESET_LABELS = {
  "1h": "Last hour",
  "24h": "Last 24 hours",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  "90d": "Last 90 days",
} as const;
export type DateRangePreset = keyof typeof DATE_RANGE_PRESET_LABELS;
export const DATE_RANGE_PRESETS = Object.keys(DATE_RANGE_PRESET_LABELS) as DateRangePreset[];

const HOUR_MS = 60 * 60 * 1000;
const PRESET_SPANS_MS: Record<DateRangePreset, number> = {
  "1h": HOUR_MS,
  "24h": 24 * HOUR_MS,
  "7d": 7 * 24 * HOUR_MS,
  "30d": 30 * 24 * HOUR_MS,
  "90d": 90 * 24 * HOUR_MS,
};

/** A range as the URL keeps it: a preset key, or `yyyy-mm-dd` days; `""` for none. */
export interface DateRange {
  preset: string;
  from: string;
  to: string;
}

export const NO_DATE_RANGE: DateRange = { preset: "", from: "", to: "" };

function isDateRangePreset(value: string): value is DateRangePreset {
  return (DATE_RANGE_PRESETS as string[]).includes(value);
}

/** Whether the range narrows anything. */
export function hasDateRange(range: DateRange): boolean {
  return isDateRangePreset(range.preset) || !!range.from || !!range.to;
}

/**
 * The instants *range* asks the server for: a preset counts back from *now*
 * and has no end; a custom day runs from its local midnight to its last
 * millisecond, both days included. An unknown preset narrows nothing.
 */
export function resolveDateRange(range: DateRange, now: number = Date.now()): { date_from?: string; date_to?: string } {
  if (range.preset) {
    return isDateRangePreset(range.preset) ? { date_from: new Date(now - PRESET_SPANS_MS[range.preset]).toISOString() } : {};
  }
  return {
    ...(range.from ? { date_from: new Date(`${range.from}T00:00:00`).toISOString() } : {}),
    ...(range.to ? { date_to: new Date(`${range.to}T23:59:59.999`).toISOString() } : {}),
  };
}
