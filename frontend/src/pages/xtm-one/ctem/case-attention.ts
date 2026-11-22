// SPDX-License-Identifier: Apache-2.0

/**
 * What needs attention first on the CTEM Cases list (#1176): one explained
 * score per open case, the default sort of the list and the ranking of the
 * "Act on this first" band. Every point of the score names its reason, so
 * the band says why a case is on top. Weights are defaults; a settings
 * override plugs in through {@link caseAttention}'s `weights` argument.
 *
 * Defaults (dev-docs/ctem-cases.md section 7.5):
 * - priority: critical 40, high 25, medium 10, low 0
 * - KEV issues: 15 when at least one
 * - crown jewels touched: 15 when at least one
 * - SLA breached or overdue: 20; due within 3 days: 10
 * - no owner: 5
 * - age: 1 point a day open, at most 10
 * A resolved case or an accepted risk scores 0: nothing to act on.
 */
import type { CtemCasePriority, CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";

export interface CaseAttentionWeights {
  priority: Record<CtemCasePriority, number>;
  kev: number;
  crownJewel: number;
  overdue: number;
  dueSoon: number;
  dueSoonDays: number;
  noOwner: number;
  agePerDay: number;
  ageMax: number;
}

export const DEFAULT_CASE_ATTENTION_WEIGHTS: CaseAttentionWeights = {
  priority: { critical: 40, high: 25, medium: 10, low: 0 },
  kev: 15,
  crownJewel: 15,
  overdue: 20,
  dueSoon: 10,
  dueSoonDays: 3,
  noOwner: 5,
  agePerDay: 1,
  ageMax: 10,
};

/** One reason of the score, rendered by the caller (i18n stays in the view). */
export type CaseAttentionReason =
  | { kind: "priority"; priority: CtemCasePriority }
  | { kind: "kev"; count: number; assets: number }
  | { kind: "crown_jewel"; count: number }
  | { kind: "overdue" }
  | { kind: "due_soon"; days: number }
  | { kind: "no_owner" }
  | { kind: "age"; days: number };

/** The action the band offers first for this case. */
export type CaseNextAction = "assign" | "set_due" | "open_case";

export interface CaseAttention {
  score: number;
  reasons: CaseAttentionReason[];
  next: CaseNextAction;
}

const DAY_MS = 24 * 3600 * 1000;

type AttentionInput = Pick<
  CtemCaseSummary,
  "state" | "priority" | "kev_count" | "asset_count" | "crown_jewel_count" | "due_at" | "sla_due_at" | "sla_breached_at" | "owner_user_id" | "created_at" | "first_seen_at"
>;

export function isOpenCase(c: Pick<CtemCaseSummary, "state">): boolean {
  return c.state === "needs_attention" || c.state === "in_progress";
}

/** The date a case started: its creation, else its oldest issue. */
export function caseOpenedAt(c: Pick<CtemCaseSummary, "created_at" | "first_seen_at">): string | null {
  return c.created_at ?? c.first_seen_at ?? null;
}

export function caseAttention(c: AttentionInput, now: number, weights: CaseAttentionWeights = DEFAULT_CASE_ATTENTION_WEIGHTS): CaseAttention {
  if (!isOpenCase(c)) return { score: 0, reasons: [], next: "open_case" };
  const reasons: CaseAttentionReason[] = [];
  let score = weights.priority[c.priority] ?? 0;
  if (score > 0) reasons.push({ kind: "priority", priority: c.priority });
  if (c.kev_count > 0) {
    score += weights.kev;
    reasons.push({ kind: "kev", count: c.kev_count, assets: c.asset_count });
  }
  if (c.crown_jewel_count > 0) {
    score += weights.crownJewel;
    reasons.push({ kind: "crown_jewel", count: c.crown_jewel_count });
  }
  const due = c.sla_due_at ?? c.due_at;
  const dueMs = due ? Date.parse(due) : Number.NaN;
  if (c.sla_breached_at || (!Number.isNaN(dueMs) && dueMs < now)) {
    score += weights.overdue;
    reasons.push({ kind: "overdue" });
  } else if (!Number.isNaN(dueMs) && dueMs - now <= weights.dueSoonDays * DAY_MS) {
    score += weights.dueSoon;
    reasons.push({ kind: "due_soon", days: Math.max(0, Math.ceil((dueMs - now) / DAY_MS)) });
  }
  if (!c.owner_user_id) {
    score += weights.noOwner;
    reasons.push({ kind: "no_owner" });
  }
  const opened = caseOpenedAt(c);
  if (opened) {
    const days = Math.max(0, Math.floor((now - Date.parse(opened)) / DAY_MS));
    const agePoints = Math.min(weights.ageMax, days * weights.agePerDay);
    if (agePoints > 0) score += agePoints;
    reasons.push({ kind: "age", days });
  }
  const next: CaseNextAction = !c.owner_user_id ? "assign" : !due ? "set_due" : "open_case";
  return { score, reasons, next };
}

/** The open cases ranked by attention, highest first (ties: most issues at stake first, then oldest). */
export function rankByAttention<T extends AttentionInput & Pick<CtemCaseSummary, "id">>(
  cases: T[],
  now: number,
  weights: CaseAttentionWeights = DEFAULT_CASE_ATTENTION_WEIGHTS,
): Array<{ kase: T; attention: CaseAttention }> {
  return cases
    .filter(isOpenCase)
    .map((kase) => ({ kase, attention: caseAttention(kase, now, weights) }))
    .sort(
      (a, b) =>
        b.attention.score - a.attention.score ||
        b.kase.kev_count - a.kase.kev_count ||
        (caseOpenedAt(a.kase) ?? "").localeCompare(caseOpenedAt(b.kase) ?? ""),
    );
}
