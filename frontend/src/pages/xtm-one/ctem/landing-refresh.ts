// SPDX-License-Identifier: Apache-2.0

/**
 * The CTEM pages refresh on landing and say so (#12183): what starts by itself
 * when the page opens, and what draws a spinner while it runs. Pure, so the
 * command center, the stage pages and their tests share one rule.
 */
import type { CtemBriefingResponse, CtemPosture, CtemStageAssessmentResponse } from "@/types";

/** Statuses on which an AI artifact never runs (no license, AI off, no model, not seeded). */
const LOCKED: ReadonlySet<string> = new Set(["ee_locked", "ai_disabled", "no_model", "unavailable"]);

/**
 * Whether the page should start a briefing run now: none stored for the
 * vertical (pending), or one past the daily window (stale), and nothing in
 * flight. A locked status never generates.
 */
export function shouldAutoGenerateBriefing(resp: CtemBriefingResponse | undefined): boolean {
  if (!resp || LOCKED.has(resp.status)) return false;
  if (resp.generating || resp.status === "generating") return false;
  if (resp.status === "pending") return true;
  return resp.status === "ready" && !!resp.stale;
}

/**
 * Whether the reader's stored briefing is shown rather than the Sample: one is
 * stored and the AI is not locked. A run regenerating a stale briefing answers
 * "generating" with the stored one, which stays on screen, marked Out of date
 * (#12217); only a first run, with nothing stored, shows the Sample.
 */
export function briefingShown(resp: CtemBriefingResponse | undefined): boolean {
  return !!resp?.briefing && !LOCKED.has(resp.status);
}

/** Whether a briefing run is replacing (or producing) the briefing shown. */
export function briefingBusy(resp: CtemBriefingResponse | undefined, requestPending: boolean): boolean {
  if (resp && LOCKED.has(resp.status)) return false;
  return requestPending || !!resp?.generating || resp?.status === "generating";
}

/**
 * Whether the posture figures on screen are being rebuilt: a first build in
 * flight (no data yet), a background rebuild the server reports, or a Force
 * sync the reader started.
 */
export function postureBusy(
  posture: Pick<CtemPosture, "refreshing"> | undefined,
  state: { loading: boolean; forcing: boolean },
): boolean {
  return state.forcing || (state.loading && !posture) || !!posture?.refreshing;
}

/** Whether a stage assessment is being generated (in flight on the server). */
export function assessmentBusy(resp: CtemStageAssessmentResponse | undefined): boolean {
  if (!resp || LOCKED.has(resp.status)) return false;
  return !!resp.generating || resp.status === "generating";
}
