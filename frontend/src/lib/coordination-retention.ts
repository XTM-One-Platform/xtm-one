// SPDX-License-Identifier: Apache-2.0

/** Coordination retention windows, in days (backend services/coordination_retention.py, #2070). 0 keeps forever. */
export const MIN_COORDINATION_RETENTION_DAYS = 0;
export const MAX_COORDINATION_RETENTION_DAYS = 3650;

export const COORDINATION_RETENTION_KEYS = [
  "work_item_resolved_days",
  "shared_state_resolved_days",
  "work_item_abandoned_days",
  "shared_state_archive_days",
  "workset_resolved_days",
] as const;

export type CoordinationRetentionKey = (typeof COORDINATION_RETENTION_KEYS)[number];
export type CoordinationRetention = Record<CoordinationRetentionKey, number>;

/** The platform defaults, used before the settings are read. */
export const COORDINATION_RETENTION_DEFAULTS: CoordinationRetention = {
  work_item_resolved_days: 90,
  shared_state_resolved_days: 90,
  work_item_abandoned_days: 14,
  shared_state_archive_days: 14,
  workset_resolved_days: 7,
};

/**
 * The window a field holds, or `null` when it cannot be saved: a whole number
 * from 0 to 3650 is kept; anything else (empty, out of the bounds, a decimal,
 * not a number) is refused as typed and never clamped, so the field shows an
 * inline error instead of a silent change. 0 is a real value: keep forever.
 */
export function parseCoordinationRetentionDays(value: string): number | null {
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const parsed = Number.parseInt(trimmed, 10);
  if (parsed < MIN_COORDINATION_RETENTION_DAYS || parsed > MAX_COORDINATION_RETENTION_DAYS) return null;
  return parsed;
}
