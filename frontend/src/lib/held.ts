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
 * The held state of an assignment run: the refusal code carried by a
 * run waiting while agentic AI is unavailable (`trigger_context.ai_hold`).
 *
 * The marker stays on a run that then ended - failed at the hold's bound,
 * cancelled or dismissed while held, dead-lettered - so only a run still
 * waiting reads as held.
 */

/** Where a held run carries its marker in `trigger_context` (`ai_hold.HOLD_MARKER_KEY`). */
export const HOLD_MARKER_KEY = "ai_hold";

/** What a hold waits for, as a person should read it. `license_required` is
 * Community Edition in XTM One mode; Copilot mode has no edition, so there it
 * is the missing Filigran license (`deps.licence_required_detail`).
 * `quota_unavailable` is a quota store that cannot be reached while quotas
 * fail closed. */
type HeldCause = "ai_disabled" | "community_edition" | "license" | "quota_store";

export function heldCause(reason: string, isXtmOne: boolean): HeldCause {
  if (reason === "ai_disabled") return "ai_disabled";
  if (reason === "quota_unavailable") return "quota_store";
  return isXtmOne ? "community_edition" : "license";
}

/** The statuses held work keeps while it waits: queued, delayed, or paused
 * with its human answer recorded. */
const WAITING_RUN_STATUSES: ReadonlySet<string> = new Set(["pending", "delayed", "awaiting_human"]);

/** How often an open evaluation run page refreshes: until the run ends,
 * because held work resumes by itself - slowly while held, since a hold can
 * last days. `null` stops polling. */
export function evaluationRunPollTier(
  run: { status: string; held_reason?: string | null } | undefined,
): "fast" | "standard" | null {
  if (run?.status !== "pending" && run?.status !== "running") return null;
  return run.held_reason ? "standard" : "fast";
}

export function runHeldReason(run: {
  status: string;
  trigger_context?: Record<string, unknown> | null;
}): string | null {
  if (!WAITING_RUN_STATUSES.has(run.status)) return null;
  const marker = run.trigger_context?.[HOLD_MARKER_KEY];
  if (marker && typeof marker === "object" && "reason" in marker) {
    const reason = (marker as { reason?: unknown }).reason;
    return typeof reason === "string" && reason ? reason : null;
  }
  return null;
}
