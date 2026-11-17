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
 * Short cause labels of an assignment pause - the "Status" tile of the
 * assignment page and the badge of the agent's assignment list. The full
 * explanation (actor, timestamp, remediation) lives in the PausedBanner.
 *
 * `Partial` on purpose: `paused_reason` is a free-form string at runtime, and
 * the backend DELIBERATELY stores unrecognised reason codes rather than lose
 * the pause record. A total `Record` would let the compiler vouch for a lookup
 * the API contract does not guarantee.
 */

import type { AssignmentPauseReason } from "@/types";

export const PAUSE_REASON_SUBTITLES: Partial<Record<AssignmentPauseReason, string>> = {
  user_toggle: "paused by a user",
  agent_tool: "paused by an agent",
  auth_quarantine: "auto-paused: integration auth failing",
  stale_reference: "auto-paused: integration resource missing",
  invalid_schedule: "auto-paused: invalid schedule",
  created_disabled: "created off, awaiting review",
  system_seed: "switched off by the deployment config",
  panic_stop: "stopped by an emergency stop",
  concierge_switch_over: "replaced by a flow (Concierge)",
};

/** Pauses the platform decided on its own because something upstream broke. */
const AUTO_PAUSE_REASONS: ReadonlySet<string> = new Set<AssignmentPauseReason>([
  "auth_quarantine",
  "stale_reference",
  "invalid_schedule",
]);

export function isAutoPaused(assignment: { enabled: boolean; paused_reason: string | null }): boolean {
  return (
    !assignment.enabled &&
    assignment.paused_reason !== null &&
    AUTO_PAUSE_REASONS.has(assignment.paused_reason)
  );
}

/**
 * Short label of the pause a deactivated owner puts on an assignment that is
 * not company-managed. Not a `paused_reason`: the backend derives it from the owner's
 * account on every read, and `enabled` keeps the owner's own choice.
 */
export const OWNER_DEACTIVATED_SUBTITLE = "paused: owner's account deactivated";

/** Whether its triggers can start the assignment right now. */
export function isAssignmentActive(assignment: {
  enabled: boolean;
  paused_by_owner_deactivation?: boolean;
}): boolean {
  return assignment.enabled && !assignment.paused_by_owner_deactivation;
}
