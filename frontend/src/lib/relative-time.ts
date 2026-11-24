// SPDX-License-Identifier: Apache-2.0

import type { IntlShape } from "react-intl";

/**
 * Localized relative and absolute times for list rows.
 *
 * `timeAgo()` and `formatShortDateTime()` (`lib/utils.ts`) read English or
 * the browser's locale; a row rendered in the app language goes through the
 * `intl` of `useFormatter()` instead, so a French page reads "il y a 6 minutes"
 * and "4 nov., 03:41" whatever the browser is set to.
 */

export type RelativeUnit = "second" | "minute" | "hour" | "day";

/**
 * How long ago `then` was, in the unit that reads best, for
 * `Intl.RelativeTimeFormat` (`{ value: 0, unit: "second" }` reads "now"), or
 * `null` for a timestamp that is not a date. A server clock slightly ahead
 * reads "now", never "in 2 minutes". Render it under `useTick()` to keep it fresh.
 */
export function relativeTimeParts(then: number, now: number = Date.now()): { value: number; unit: RelativeUnit } | null {
  if (!Number.isFinite(then) || !Number.isFinite(now)) return null;
  const seconds = Math.min(0, Math.round((then - now) / 1000));
  const elapsed = -seconds;
  if (elapsed < 45) return { value: 0, unit: "second" };
  if (elapsed < 45 * 60) return { value: Math.round(seconds / 60), unit: "minute" };
  if (elapsed < 22 * 3600) return { value: Math.round(seconds / 3600), unit: "hour" };
  return { value: Math.round(seconds / 86400), unit: "day" };
}

/** "6 minutes ago" in the app language; the ISO string as written when it is not a date. */
export function formatTimeAgo(intl: Pick<IntlShape, "formatRelativeTime">, iso: string, now: number = Date.now()): string {
  const parts = relativeTimeParts(Date.parse(iso), now);
  if (!parts) return iso;
  return intl.formatRelativeTime(parts.value, parts.unit, { numeric: "auto" });
}

/**
 * A whole sentence around a relative time ("Checked 5 minutes ago", "Ran 2 hours ago")
 * in the app language: `line` wraps the relative time in its catalogued
 * sentence, `justNow` is the sentence read under 45 seconds ("Checked just now"),
 * and a `then` that is not a date reads `line(fallback)`. The callers keep their
 * literal `t_i18n` keys (the catalogue validator reads them); one helper decides
 * the bucket, so no page special-cases 0 seconds by hand.
 */
export function formatTimeAgoLine(
  intl: Pick<IntlShape, "formatRelativeTime">,
  then: number,
  { line, justNow, fallback }: { line: (when: string) => string; justNow: () => string; fallback: string },
  now: number = Date.now(),
): string {
  const parts = relativeTimeParts(then, now);
  if (!parts) return line(fallback);
  if (parts.value === 0) return justNow();
  return line(intl.formatRelativeTime(parts.value, parts.unit, { numeric: "auto" }));
}

/** A compact date and time without the year ("Nov 4, 03:41 AM") in the app language. */
export function formatShortDateTimeIntl(intl: Pick<IntlShape, "formatDate">, iso: string | Date): string {
  const at = typeof iso === "string" ? Date.parse(iso) : iso.getTime();
  if (Number.isNaN(at)) return String(iso);
  return intl.formatDate(at, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/**
 * A calendar day with its year ("Jun 30, 2027") in the app language, for a
 * date a record carries without a time (an expiry, "2027-06-30"). Read in
 * UTC so a date-only value never shifts to the day before west of Greenwich.
 */
export function formatDayIntl(intl: Pick<IntlShape, "formatDate">, iso: string): string {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return String(iso);
  return intl.formatDate(at, { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
}
