// SPDX-License-Identifier: Apache-2.0

/**
 * A gate answered on the case page and not yet applied by the loop (#2746).
 * Between the answer and the loop resolving the gate, the case still reads the
 * gate as waiting: the page keeps the answer here (by the decision's work item
 * id) and shows "being applied" in place of the waiting block, never the stale
 * "waits for a person" text. A work item read done while its gate still waits
 * (answered in another tab or in the person's list) reads the same way.
 * Client state only: the case and its runs are refetched through their own
 * query keys, no endpoint of its own.
 */
import { useSyncExternalStore } from "react";
import type { CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import type { WorkItem } from "@/types";

/** The answer the page sent: the option's label (null for a free-text answer) and when. */
export interface GateAnswer {
  label: string | null;
  at: number;
}

/** How long the page polls the case for the gate to leave waiting before it says to refresh later. */
export const APPLY_WINDOW_MS = 120_000;
/** The collapse of a waiting block once its gate is applied (none under prefers-reduced-motion). */
export const COLLAPSE_MS = 300;

const answers = new Map<string, GateAnswer>();
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Keep the answer sent on a gate's decision, so the page shows it being applied. */
export function recordGateAnswer(workItemId: string, label: string | null, at: number = Date.now()) {
  answers.set(workItemId, { label, at });
  emit();
}

/** Forget a gate's answer once the case reads the gate applied. */
export function clearGateAnswer(workItemId: string | null | undefined) {
  if (workItemId && answers.delete(workItemId)) emit();
}

/** The answer this page sent on a decision, live; null when none. */
export function useGateAnswer(workItemId: string | null | undefined): GateAnswer | null {
  return useSyncExternalStore(
    subscribe,
    () => (workItemId ? (answers.get(workItemId) ?? null) : null),
    () => null,
  );
}

/** The answer a done work item carries (`payload.answer`, as `answer_work_item` stores it); null when it is not done. */
export function doneItemAnswer(item: WorkItem | null | undefined): GateAnswer | null {
  if (!item || item.status !== "done") return null;
  const answer = (item.payload as { answer?: unknown } | null)?.answer;
  const fields = answer && typeof answer === "object" ? (answer as Record<string, unknown>) : {};
  const label = typeof fields.label === "string" && fields.label ? fields.label : null;
  const stamp = [fields.answered_at, item.completed_at, item.updated_at].find((v): v is string => typeof v === "string" && !!v);
  const at = stamp ? Date.parse(stamp) : Number.NaN;
  return { label, at: Number.isFinite(at) ? at : Date.now() };
}

/** The delay before the next refetch of the case, backing off; null once the window is spent. */
export function applyPollDelay(elapsedMs: number): number | null {
  if (elapsedMs >= APPLY_WINDOW_MS) return null;
  if (elapsedMs < 10_000) return 2_000;
  if (elapsedMs < 30_000) return 3_000;
  return 5_000;
}

type GateApplyingPhase = "applying" | "timeout" | "failed";

export interface GateApplying {
  phase: GateApplyingPhase;
  label: string | null;
  since: number;
  /** The failed run, on `failed`. */
  runId: string | null;
}

/**
 * Where an answered gate stands while the case still reads it waiting: being
 * applied, past the polling window, or failed (the newest case run started
 * after the answer failed and none is in flight). Null when the gate does not
 * wait or nothing was answered. Pure.
 */
export function gateApplying({
  waiting,
  answer,
  runs,
  now,
}: {
  waiting: boolean;
  answer: GateAnswer | null;
  runs?: readonly CtemCaseRun[];
  now: number;
}): GateApplying | null {
  if (!waiting || !answer) return null;
  const base = { label: answer.label, since: answer.at };
  const after = (runs ?? [])
    .filter((run) => Date.parse(run.created_at) >= answer.at)
    .slice()
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const newest = after[after.length - 1];
  if (newest && newest.outcome === "failed" && !after.some((run) => run.outcome === "active")) {
    return { ...base, phase: "failed", runId: newest.id };
  }
  if (now - answer.at >= APPLY_WINDOW_MS) return { ...base, phase: "timeout", runId: null };
  return { ...base, phase: "applying", runId: null };
}
