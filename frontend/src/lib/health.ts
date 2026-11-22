/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Labels and statuses of the Platform Health's cycles, signals and digests (#8261),
 * one module. Every label is an i18n key; an unknown code (a newer server's)
 * is shown as itself, never as a known one.
 */

import type { HealthScheduleMark, HealthTargetType } from "@/types";

/**
 * Whether a schedule mark is a real firing (#735). The scheduler writes a mark
 * with `now` the first time it sees a schedule, so the cycle first fires at its
 * next occurrence and never at boot; that mark carries neither a cycle nor a
 * refusal and is not a firing (the Schedule table shows "Never").
 */
export function isScheduleFiring(mark: HealthScheduleMark | undefined): mark is HealthScheduleMark {
  return !!mark && (mark.cycle_id !== null || mark.refusal !== null);
}

type BadgeVariant = "default" | "secondary" | "outline" | "success" | "warning" | "destructive" | "cyan";

/** @i18n-keys */
export const HEALTH_DIGEST_STATUS_LABELS = {
  open: "In progress",
  completed: "Completed",
  partial: "Partly completed",
  budget_reached: "Budget reached",
  failed: "Failed",
  cancelled: "Cancelled",
} as const;

export const HEALTH_DIGEST_STATUS_VARIANTS: Record<string, BadgeVariant> = {
  open: "cyan",
  completed: "success",
  partial: "warning",
  budget_reached: "warning",
  failed: "destructive",
  cancelled: "outline",
};

/** Platform Health's default name (`naming.default_concierge_name`), the same in every platform mode (#101). */
export function defaultHealthName(): string {
  return "Platform Health";
}

/** @i18n-keys */
export const HEALTH_SIGNAL_LABELS = {
  feedback_drop: "Feedback drop",
  failure_spike: "Failure spike",
  cost_drift: "Cost drift",
  model_change: "Model change",
} as const;

/** @i18n-keys */
export const HEALTH_SPECIALIST_LABELS = {
  orchestrator: "Platform Health",
  memory_steward: "Memory Steward",
  quality_analyst: "Quality Analyst",
  flow_lab: "Flow Lab",
  experimenter: "Experimenter",
  opencti_checker: "OpenCTI Health Checker",
  openaev_checker: "OpenAEV Health Checker",
  opencrq_checker: "OpenCRQ Health Checker",
  module_analyst: "Module Analyst",
} as const;

/** Every role a module checker cycle is booked under, the S14 alias included (backend `identity.MODULE_CHECKER_ROLES`). */
const MODULE_CHECKER_ROLES: ReadonlySet<string> = new Set(["opencti_checker", "openaev_checker", "opencrq_checker", "module_analyst"]);

/** Whether a cycle's specialist is a module checker (#101 S15): its digest shows recommendations and agentic findings. */
export function isModuleCheckerRole(specialist: string): boolean {
  return MODULE_CHECKER_ROLES.has(specialist);
}

/** @i18n-keys */
export const HEALTH_TRIGGER_LABELS = {
  schedule: "Schedule",
  signal: "Signal",
  on_demand: "On demand",
} as const;

/** @i18n-keys */
const SKIP_LABELS = {
  run: "The run",
  budget: "Budget",
  experiments: "Experiments",
  measurement: "Measurement",
  writes: "Writes",
} as const;

/** Reasons and counted keys: run statuses, experiment statuses, verdicts, refusals. */
/** @i18n-keys */
const REASON_LABELS = {
  failed: "failed",
  skipped: "skipped",
  cancelled: "cancelled",
  degraded: "degraded",
  incomplete: "incomplete",
  not_started: "not started",
  budget_reached: "budget reached",
  cycle_budget_reached: "the budget per cycle is reached",
  month_budget_reached: "the monthly budget is reached",
  unmeasured: "unmeasured",
  progression: "progression",
  neutral: "neutral",
  regression: "regression",
  completed: "completed",
  running: "running",
  new: "new",
  known: "known",
  merge: "merged",
  promote: "promoted",
  demote: "demoted",
  remove: "removed",
  flag: "flagged",
  dry_run: "dry-run",
  high: "high confidence",
  medium: "medium confidence",
  low: "low confidence",
  opened: "opened",
  resolved: "resolved",
} as const;

export function healthSkipLabel(what: string): string {
  return labelOf(SKIP_LABELS, what);
}

export function healthReasonLabel(reason: string): string {
  return labelOf(REASON_LABELS, reason);
}

/** Why "Improve with {name}" started nothing, by the code a 409 carries (`cycles.REFUSAL_MESSAGES`). */
/** @i18n-keys */
const REFUSAL_LABELS = {
  inactive: "{name} is not enabled.",
  concierge_disabled: "{name} is not enabled.",
  no_assignment: "{name}'s fleet is not set up yet.",
  paused: "{name}'s assignment for this work is paused.",
  not_in_scope: "This target is outside {name}'s supervision scope.",
  not_found: "Target not found.",
  cycle_open: "{name} is already working on this target.",
  ai_disabled: "Agentic AI is turned off on this platform.",
  license_required: "Agentic AI needs a licence on this platform.",
  transient: "{name} could not check its budget. Try again shortly.",
  cycle_budget_reached: "{name}'s budget per cycle is reached.",
  month_budget_reached: "{name}'s monthly budget is reached.",
  start_failed: "{name} could not start this work.",
} as const;

/** The i18n key of a refusal code (`{name}` placeholder), `null` for a code this build does not know. */
export function healthRefusalCodeKey(code: unknown): string | null {
  return typeof code === "string" && Object.prototype.hasOwnProperty.call(REFUSAL_LABELS, code)
    ? REFUSAL_LABELS[code as keyof typeof REFUSAL_LABELS]
    : null;
}

/** The i18n key of a refusal: by its code, else the generic one. */
export function healthRefusalKey(error: unknown): string {
  const detail = (error as { detail?: unknown } | null)?.detail;
  const code = detail && typeof detail === "object" ? (detail as { code?: unknown }).code : undefined;
  return healthRefusalCodeKey(code) ?? REFUSAL_LABELS.start_failed;
}

/** @i18n-keys */
export const IMPROVEMENT_TYPE_LABELS = {
  persona: "Persona",
  skill: "Skill",
  assignment_prompt: "Assignment prompt",
  flow_repair: "Flow repair",
  flow_optimization: "Flow optimization",
  new_flow: "New flow",
  agent_to_flow: "Agent to flow",
} as const;

/** @i18n-keys */
export const IMPROVEMENT_STATUS_LABELS = {
  pending_approval: "Awaiting approval",
  applying: "Applying",
  shadow: "In shadow",
  watching: "Watching",
  kept: "Kept",
  rejected: "Rejected",
  rolled_back: "Rolled back",
  regression_kept: "Kept after a human edit",
  failed: "Failed",
} as const;

/** The experiment's verdict: a proposal without one is unmeasured, said as such. */
/** @i18n-keys */
export const IMPROVEMENT_VERDICT_LABELS = {
  progression: "Measured gain",
  neutral: "No measured change",
  regression: "Measured regression",
  unmeasured: "Unmeasured",
} as const;

export const IMPROVEMENT_VERDICT_VARIANTS: Record<string, BadgeVariant> = {
  progression: "success",
  neutral: "secondary",
  regression: "destructive",
  unmeasured: "warning",
};

/** A label of *table* for *key*, the key itself for a code this build does not know. */
export function labelOf(table: Readonly<Record<string, string>>, key: string): string {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : key;
}

/** @i18n-keys */
export const EVIDENCE_KIND_LABELS = {
  trace: "Trace",
  rating: "Rating",
  assignment_run: "Run",
  flow_run: "Flow run",
  evaluation_run: "Evaluation run",
} as const;

/** The page of a cycle's, signal's or proposal's target. */
export function healthTargetHref(type: HealthTargetType | null, id: string | null): string | null {
  if (!id) return null;
  if (type === "agent") return `/agents/${id}`;
  if (type === "flow") return `/flows/library/${id}`;
  if (type === "assignment") return `/agents/assignments/${id}`;
  return null;
}
