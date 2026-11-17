// SPDX-License-Identifier: Apache-2.0

/** Trace retention window, in days: the bounds of `trace_retention_days` (backend api/v1/settings.py, ObservabilityConfigUpdate). */
export const TRACE_RETENTION_DEFAULT_DAYS = 90;
export const MIN_TRACE_RETENTION_DAYS = 1;
export const MAX_TRACE_RETENTION_DAYS = 365;

/**
 * The window a field holds, or `null` when it cannot be saved: an empty field
 * goes back to the default, a whole number within the bounds is kept, anything
 * else (out of the bounds, a decimal, not a number) is refused as typed and
 * never clamped, so the field shows an inline error instead of a silent change.
 */
export function parseTraceRetentionDays(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return TRACE_RETENTION_DEFAULT_DAYS;
  if (!/^\d+$/.test(trimmed)) return null;
  const parsed = Number.parseInt(trimmed, 10);
  if (parsed < MIN_TRACE_RETENTION_DAYS || parsed > MAX_TRACE_RETENTION_DAYS) return null;
  return parsed;
}
