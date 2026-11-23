// SPDX-License-Identifier: Apache-2.0

/** The pure figures of the Cases board (dev-docs/ctem-cases.md 7.0 and 7.1). */
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { CASE_PRIORITY_RANK } from "./cases-shared";

export const DAY = 86_400_000;

/**
 * The board column surface, fixed per theme (7.0, UX r1 B1 on #11666): layer 2
 * in dark, the white layer 1 in light, where the layer 2 tint took the lightest
 * age band under 3:1. Ratios held by tests_unit/ctem-cases-board-contrast.test.ts.
 */
export const BOARD_COLUMN_SURFACE = "bg-[var(--bg-elevation-default-layer-2)] light:bg-[var(--bg-elevation-default-layer-1)]";

/**
 * The board grid: one card per case status, never a row with an orphan card
 * (UX round 1 of #857). Eight statuses (#1503) do not fit one row at 1280:
 * four per row from xl (open statuses then the closed ones read in order), two
 * from md, one stacked below. BOARD_GRID_COLUMNS is held equal to
 * CTEM_CASE_STATES.length and every grid-cols-N divides it, by
 * tests_unit/ctem-cases-board-grid.test.ts.
 */
export const BOARD_GRID_COLUMNS = 8;
export const BOARD_GRID = "grid-cols-1 md:grid-cols-2 xl:grid-cols-4";

/**
 * The overdue line's text (UX r1 B2 on #11666): the error primary falls to
 * 4.26:1 on the dark column, the error tertiary holds 4.5:1 in both themes;
 * the icon keeps the error primary (3:1 is its bar).
 */
export const OVERDUE_TEXT = "text-feedback-error-tertiary";

/** Upper bound (days, inclusive) of each age band, youngest first. */
const AGE_BAND_MAX = [7, 30, 90, Infinity] as const;

/** Days since an ISO time, at least 0; null when the time is unknown. */
export function daysSince(iso: string | null, now: number): number | null {
  if (!iso) return null;
  const at = Date.parse(iso);
  return Number.isNaN(at) ? null : Math.max(0, Math.floor((now - at) / DAY));
}

/** Count of cases per age band of their time in state (no state time: left out of the bar). */
export function ageBands(cases: CtemCaseSummary[], now: number): number[] {
  const counts = AGE_BAND_MAX.map(() => 0);
  for (const c of cases) {
    const days = daysSince(c.state_since, now);
    if (days === null) continue;
    counts[AGE_BAND_MAX.findIndex((max) => days <= max)] += 1;
  }
  return counts;
}

/** Rank of a loop severity, most urgent first; a case with none comes after (CTEM-03-UI, #892). */
const SEVERITY_RANK: Record<string, number> = { P1: 0, P2: 1, P3: 2 };
const severityRank = (c: CtemCaseSummary) => (c.severity ? (SEVERITY_RANK[c.severity] ?? 3) : 3);

/** The three most urgent cases: loop severity (P1 first), then priority, then the oldest in its state. */
export function topCases(cases: CtemCaseSummary[]): CtemCaseSummary[] {
  return [...cases]
    .sort(
      (a, b) =>
        severityRank(a) - severityRank(b) ||
        CASE_PRIORITY_RANK[b.priority] - CASE_PRIORITY_RANK[a.priority] ||
        (a.state_since ?? "").localeCompare(b.state_since ?? ""),
    )
    .slice(0, 3);
}
