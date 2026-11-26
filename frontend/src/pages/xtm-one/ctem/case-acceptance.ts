// SPDX-License-Identifier: Apache-2.0

/**
 * Longest accepted risk, in days: the backend's DEFAULT_MAX_ACCEPTANCE
 * (backend/app/services/ctem_cases.py). The backend accepts any expiry whose
 * UTC day is on or before today + this many days, the end of that day included
 * (#2490), so the default and the latest day of the picker are that day.
 */
export const ACCEPTANCE_MAX_DAYS = 90;

/** The UTC day `days` from now, `yyyy-mm-dd`. */
export function utcDayIn(days: number, now: number = Date.now()): string {
  return new Date(now + days * 86_400_000).toISOString().slice(0, 10);
}

/** The latest expiry day an acceptance may take, and the dialogs' default. */
export function acceptanceMaxDay(now: number = Date.now()): string {
  return utcDayIn(ACCEPTANCE_MAX_DAYS, now);
}
