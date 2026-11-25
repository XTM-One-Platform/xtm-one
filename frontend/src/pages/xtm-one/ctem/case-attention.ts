// SPDX-License-Identifier: Apache-2.0

/**
 * What needs attention first on the CTEM Cases list (#1176): one explained
 * score per open case, the default sort of the list and the ranking of the
 * "Act on this first" band. Every point of the score names its reason, so
 * the band says why a case is on top. The points are the organization's
 * (profile `case_attention`, set by an administrator from the band), else the
 * defaults below; {@link weightsFromProfile} maps one to the other.
 *
 * Defaults (dev-docs/ctem-cases.md section 7.2bis):
 * - priority: critical 40, high 25, medium 10, low 0
 * - KEV issues: 15 when at least one
 * - primary assets touched: 15 when at least one
 * - SLA breached or overdue: 20; due within 3 days: 10
 * - no owner: 5
 * - age: 1 point a day open, at most 10
 * A resolved case or an accepted risk scores 0: nothing to act on.
 */
import { isOpenCaseState, type CtemCasePriority, type CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import type { CaseAttentionProfile } from "@/lib/organization-profile";

export interface CaseAttentionWeights {
  priority: Record<CtemCasePriority, number>;
  kev: number;
  primaryAsset: number;
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
  primaryAsset: 15,
  overdue: 20,
  dueSoon: 10,
  dueSoonDays: 3,
  noOwner: 5,
  agePerDay: 1,
  ageMax: 10,
};

/** The profile's points as weights; absent (an older backend or never set): the defaults. */
export function weightsFromProfile(saved: CaseAttentionProfile | null | undefined): CaseAttentionWeights {
  if (!saved) return DEFAULT_CASE_ATTENTION_WEIGHTS;
  return {
    priority: { critical: saved.priority_critical, high: saved.priority_high, medium: saved.priority_medium, low: saved.priority_low },
    kev: saved.kev,
    primaryAsset: saved.primary_asset,
    overdue: saved.overdue,
    dueSoon: saved.due_soon,
    dueSoonDays: saved.due_soon_days,
    noOwner: saved.no_owner,
    agePerDay: saved.age_per_day,
    ageMax: saved.age_max,
  };
}

/** The weights as the profile stores them. */
export function weightsToProfile(w: CaseAttentionWeights): CaseAttentionProfile {
  return {
    priority_critical: w.priority.critical,
    priority_high: w.priority.high,
    priority_medium: w.priority.medium,
    priority_low: w.priority.low,
    kev: w.kev,
    primary_asset: w.primaryAsset,
    overdue: w.overdue,
    due_soon: w.dueSoon,
    due_soon_days: w.dueSoonDays,
    no_owner: w.noOwner,
    age_per_day: w.agePerDay,
    age_max: w.ageMax,
  };
}

/** One reason of the score, rendered by the caller (i18n stays in the view). */
export type CaseAttentionReason =
  | { kind: "priority"; priority: CtemCasePriority }
  | { kind: "kev"; count: number; assets: number }
  | { kind: "primary_asset"; count: number }
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
  "state" | "priority" | "kev_count" | "asset_count" | "primary_asset_count" | "due_at" | "sla_due_at" | "sla_breached_at" | "owner_user_id" | "created_at" | "first_seen_at"
>;

function isOpenCase(c: Pick<CtemCaseSummary, "state">): boolean {
  return isOpenCaseState(c.state);
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
  // A reason whose points are set to 0 is dropped from the score and from the why line (UX r2 B1 on #1260).
  // A null count is unknown for this reader (scope-limited): it adds no points and names no reason.
  const kev = c.kev_count ?? 0;
  const primaryAssets = c.primary_asset_count ?? 0;
  if (kev > 0 && weights.kev > 0) {
    score += weights.kev;
    reasons.push({ kind: "kev", count: kev, assets: c.asset_count ?? 0 });
  }
  if (primaryAssets > 0 && weights.primaryAsset > 0) {
    score += weights.primaryAsset;
    reasons.push({ kind: "primary_asset", count: primaryAssets });
  }
  const due = c.sla_due_at ?? c.due_at;
  const dueMs = due ? Date.parse(due) : Number.NaN;
  const overdue = Boolean(c.sla_breached_at) || (!Number.isNaN(dueMs) && dueMs < now);
  if (overdue) {
    if (weights.overdue > 0) {
      score += weights.overdue;
      reasons.push({ kind: "overdue" });
    }
  } else if (weights.dueSoon > 0 && !Number.isNaN(dueMs) && dueMs - now <= weights.dueSoonDays * DAY_MS) {
    score += weights.dueSoon;
    reasons.push({ kind: "due_soon", days: Math.max(0, Math.ceil((dueMs - now) / DAY_MS)) });
  }
  if (!c.owner_user_id && weights.noOwner > 0) {
    score += weights.noOwner;
    reasons.push({ kind: "no_owner" });
  }
  const opened = caseOpenedAt(c);
  // Age is named while it can add points (per day and cap above 0), so a case opened today still reads "Opened today".
  if (opened && weights.agePerDay > 0 && weights.ageMax > 0) {
    const days = Math.max(0, Math.floor((now - Date.parse(opened)) / DAY_MS));
    const agePoints = Math.min(weights.ageMax, days * weights.agePerDay);
    score += agePoints;
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
        (b.kase.kev_count ?? 0) - (a.kase.kev_count ?? 0) ||
        (caseOpenedAt(a.kase) ?? "").localeCompare(caseOpenedAt(b.kase) ?? ""),
    );
}
