// SPDX-License-Identifier: Apache-2.0

/**
 * What a `platform_quota_reset` support code reset, in the admin's words.
 * A code can be narrowed to some quota types and windows, so the
 * confirmation names every counter the server says it reset - never a blanket
 * "your usage starts again" that a narrowed code would make false.
 */

import type { PlatformQuotaResetCounter, PlatformQuotaResetResult } from "../types/index.ts";

type Translate = (id: string, values?: Record<string, string | number>) => string;

/** @i18n-keys */
export const RESET_QUOTA_TYPE_LABELS = {
  agentic: "Agentic Executions",
  sandbox: "Sandbox executions",
  llm_proxy: "LLM Proxy Requests",
} as const satisfies Record<PlatformQuotaResetCounter["quota_type"], string>;

/** @i18n-keys */
export const RESET_PERIOD_LABELS = {
  daily: "Today",
  monthly: "This Month",
  yearly: "This Year",
} as const satisfies Record<PlatformQuotaResetCounter["period"], string>;

/** @i18n-keys */
export const RESET_MESSAGES = {
  counter: "{counter}, {period}: {count} to 0",
  summary: "Reset: {counters}. Every other counter, each user's own usage and the license limits are unchanged.",
} as const;

/** One line per counter the server reset, in the order it reported them. */
export function describeResetCounters(
  result: Pick<PlatformQuotaResetResult, "counters">,
  t: Translate,
): string[] {
  return result.counters.map((counter) =>
    t(RESET_MESSAGES.counter, {
      counter: t(RESET_QUOTA_TYPE_LABELS[counter.quota_type]),
      period: t(RESET_PERIOD_LABELS[counter.period]),
      count: counter.previous_count,
    }),
  );
}

/** The confirmation an admin reads once the reset committed. */
export function describePlatformQuotaReset(
  result: Pick<PlatformQuotaResetResult, "counters">,
  t: Translate,
): string {
  return t(RESET_MESSAGES.summary, { counters: describeResetCounters(result, t).join("; ") });
}
