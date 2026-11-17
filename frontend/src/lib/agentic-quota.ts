// SPDX-License-Identifier: Apache-2.0

/**
 * The agentic count a quota indicator shows. Pure, so `node --test` loads it
 * (`agentic-quota.test.ts`); re-exported by `lib/utils.ts`.
 *
 * Its backend twin is `merge_sandbox_into_agentic_count` in
 * `backend/app/services/platform_chat_extras.py` (the embedded chat's quota).
 * Both are checked against
 * `tests_e2e/fixtures/agentic-quota-count-cases.json`.
 */

import type { QuotaUsageEntry } from "../types/index.ts";

/**
 * Sandbox usage has no row of its own in the per-user usage-review UI — its
 * count is folded into the displayed "agentic" total instead (spec
 * 002-sandbox-quota-usage-tracking FR-013/FR-014). Sandbox has no period of
 * its own (FR-011, revised) — the backend always reports its entry under
 * agentic's resolved period, so the two counts can always be summed
 * directly.
 *
 * Exception: a `scope === "global"` agentic entry is the shared platform-wide
 * pool (every user, every entry point), while the sandbox entry is still this
 * viewer's own slice — adding them would mix two different populations into
 * one number. The pool is returned untouched in that case: the backend already
 * reports it as global agentic + global sandbox, the numerator the gate
 * compares.
 */
export function mergeSandboxIntoAgenticCount(
  usage: QuotaUsageEntry[],
  agenticTotalEntry: QuotaUsageEntry | undefined,
): number {
  const agenticCount = agenticTotalEntry?.count ?? 0;
  if (agenticTotalEntry?.scope === "global") return agenticCount;
  const sandboxEntry = usage.find((u) => u.usage_type === "sandbox" && u.model === null);
  return agenticCount + (sandboxEntry?.count ?? 0);
}
