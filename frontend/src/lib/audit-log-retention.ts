// SPDX-License-Identifier: Apache-2.0

/** Audit log retention window, in days: the bounds of `audit_log_retention_days` (backend services/audit_retention.py). */
export const AUDIT_LOG_RETENTION_DEFAULT_DAYS = 90;
export const MIN_AUDIT_LOG_RETENTION_DAYS = 30;
export const MAX_AUDIT_LOG_RETENTION_DAYS = 3650;

/**
 * The window a field holds, or `null` when it cannot be saved: an empty field
 * goes back to the default, a whole number within the bounds is kept, anything
 * else (out of the bounds, a decimal, not a number) is refused as typed and
 * never clamped, so the field shows an inline error instead of a silent change.
 */
export function parseAuditLogRetentionDays(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return AUDIT_LOG_RETENTION_DEFAULT_DAYS;
  if (!/^\d+$/.test(trimmed)) return null;
  const parsed = Number.parseInt(trimmed, 10);
  if (parsed < MIN_AUDIT_LOG_RETENTION_DAYS || parsed > MAX_AUDIT_LOG_RETENTION_DAYS) return null;
  return parsed;
}
