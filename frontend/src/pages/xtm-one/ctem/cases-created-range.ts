// SPDX-License-Identifier: Apache-2.0

/**
 * The Created filter of the CTEM Cases list (#1176): when a case was opened,
 * as ranges counted back from now. The ranges nest, so a case opened 3 days
 * ago matches "Last 7 days", "Last 30 days" and "Last 90 days"; a case older
 * than 90 days matches "Older than 90 days" alone. A case with no date
 * matches none.
 */
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { caseOpenedAt } from "./case-attention";

/** The ranges, by key, most recent first. @i18n-keys */
export const CASE_CREATED_RANGE_LABEL = {
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  "90d": "Last 90 days",
  older: "Older than 90 days",
} as const;
type CaseCreatedRange = keyof typeof CASE_CREATED_RANGE_LABEL;
export const CASE_CREATED_RANGES = Object.keys(CASE_CREATED_RANGE_LABEL) as CaseCreatedRange[];

const DAY_MS = 24 * 3600 * 1000;
const RANGE_DAYS: Record<Exclude<CaseCreatedRange, "older">, number> = { "7d": 7, "30d": 30, "90d": 90 };

export function caseCreatedRanges(c: Pick<CtemCaseSummary, "created_at" | "first_seen_at">, now: number): CaseCreatedRange[] {
  const opened = caseOpenedAt(c);
  const openedMs = opened ? Date.parse(opened) : Number.NaN;
  if (Number.isNaN(openedMs)) return [];
  const ageMs = Math.max(0, now - openedMs);
  const ranges = (Object.keys(RANGE_DAYS) as Array<keyof typeof RANGE_DAYS>).filter((r) => ageMs <= RANGE_DAYS[r] * DAY_MS);
  return ranges.length > 0 ? ranges : ["older"];
}
