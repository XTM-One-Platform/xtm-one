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
 * Structured failure causes.
 *
 * Mirrors `backend/app/services/failure_causes.py`. That module is the source
 * of truth: a cause is added there first, then here — and the backend rejects
 * a filter value it does not know (422), so a drift shows up as a failed
 * request rather than as a silently empty result page.
 *
 * Grouped by *who* the failure points at, because that is the question the
 * Trace Explorer's failure filter exists to answer. The grouping is a display
 * concern only — it deliberately does NOT imply a billing bucket, which is a
 * commercial decision the backend has not made yet.
 */

/** Not a cause: selects FAILED rows carrying no classification at all.
 */
const UNRECORDED_FAILURE_CAUSE = "unrecorded";

/**
 * Not a cause either: the remainder the quota card reports when its tallied
 * causes do not add up to a headline total — credits granted before the
 * breakdown existed, or lost with its Redis hash. Distinct from `unknown`,
 * which IS a classification ("we looked and could not attribute it").
 */
export const NOT_RECORDED_REFUND_CAUSE = "not_recorded";

interface FailureCauseGroup {
  /**
   * Group name. The filter's multi-select has no section headers, so this is
   * prefixed onto each option's own label instead (`Provider · Rate limited`)
   * — which is also what keeps the grouping visible on the selected chips.
   * See `failureCauseOptions` in `pages/Traces.tsx`.
   */
  label: string;
  causes: readonly string[];
}

export const FAILURE_CAUSE_GROUPS: readonly FailureCauseGroup[] = [
  {
    label: "Configuration",
    causes: [
      "assignment_unavailable",
      "agent_unavailable",
      "failed_before_model_call",
      "ai_disabled",
      "license_required",
      "stale_integration_reference",
      "pipeline_tool_failed",
      "platform_quota_exceeded",
      "quota_backend_unavailable",
    ],
  },
  {
    label: "Request",
    causes: [
      "context_overflow",
      "request_too_large",
      "model_rejected_request",
      "model_declined",
    ],
  },
  {
    label: "Provider",
    causes: [
      "provider_rate_limited",
      "provider_overloaded",
      "provider_auth_rejected",
      "provider_timeout",
      "provider_connection_failed",
      "all_providers_failed",
    ],
  },
  {
    label: "Run control",
    causes: ["run_budget_exhausted", "run_cancelled"],
  },
  {
    label: "Orchestration",
    causes: [
      "engine_never_claimed",
      "engine_attempts_exhausted",
      "engine_deadline_exceeded",
      "engine_abandoned",
      "deadline_exceeded_before_start",
    ],
  },
  {
    label: "Unattributed",
    causes: ["unknown", UNRECORDED_FAILURE_CAUSE],
  },
] as const;

/**
 * Human label per cause. Written as a short statement of what happened, not as
 * the enum value prettified — "Provider rate limited" reads faster than
 * "Provider Rate Limited" and says the same thing an engineer would say out
 * loud when pointing at the row.
 */
const FAILURE_CAUSE_LABELS: Record<string, string> = {
  assignment_unavailable: "Assignment missing or disabled",
  agent_unavailable: "Agent missing or disabled",
  failed_before_model_call: "Failed before any model call",
  // The platform switch, or a group of the person the work runs for.
  ai_disabled: "AI disabled",
  license_required: "AI not licensed",
  stale_integration_reference: "Integration resource missing",
  pipeline_tool_failed: "Pipeline tool refused or failed",
  platform_quota_exceeded: "Platform quota exceeded",
  quota_backend_unavailable: "Quota backend unavailable",
  context_overflow: "Context overflow",
  request_too_large: "Request too large",
  model_rejected_request: "Model rejected the request",
  model_declined: "Model declined to answer",
  provider_rate_limited: "Provider rate limited",
  provider_overloaded: "Provider overloaded",
  provider_auth_rejected: "Provider rejected credentials",
  provider_timeout: "Provider timed out",
  provider_connection_failed: "Provider unreachable",
  all_providers_failed: "All providers failed",
  run_budget_exhausted: "Run budget exhausted",
  run_cancelled: "Cancelled",
  engine_never_claimed: "Never claimed by a worker",
  engine_attempts_exhausted: "Attempts exhausted",
  engine_deadline_exceeded: "Deadline exceeded",
  engine_abandoned: "Abandoned by the engine",
  deadline_exceeded_before_start: "Never started in time",
  unknown: "Unattributed",
  [UNRECORDED_FAILURE_CAUSE]: "No cause recorded",
  [NOT_RECORDED_REFUND_CAUSE]: "No cause recorded",
};

export function formatFailureCause(cause: string | null | undefined): string {
  if (!cause) return FAILURE_CAUSE_LABELS[UNRECORDED_FAILURE_CAUSE];
  return FAILURE_CAUSE_LABELS[cause] ?? cause;
}
