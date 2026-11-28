// SPDX-License-Identifier: Apache-2.0

/**
 * A gate answered on the case page and not yet applied by the loop (#2746).
 * Between the answer and the loop resolving the gate, the case still reads the
 * gate as waiting: the page keeps the answer here (by the decision's work item
 * id) and shows "being applied" in place of the waiting block, never the stale
 * "waits for a person" text. A work item read answered while its gate still
 * waits (after a reload, in another tab or in the person's list; open or done)
 * reads the same way.
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

/**
 * The answer a work item carries (`payload.answer`, as `answer_work_item` stores
 * it), whatever its status: a single answer leaves the item `open` with
 * `needs_human` false, a grouped one sets it `done` (#2757 UX round 2, B1).
 * Null when it carries no answer, still waits for a person, was cancelled, or
 * was answered before `askedAt` (an answer to an earlier ask of the same item).
 */
export function itemAnswer(item: WorkItem | null | undefined, askedAt?: string | null): GateAnswer | null {
  if (!item || item.status === "cancelled" || item.needs_human) return null;
  const answer = (item.payload as { answer?: unknown } | null)?.answer;
  if (!answer || typeof answer !== "object") return null;
  const fields = answer as Record<string, unknown>;
  const answeredAt = typeof fields.answered_at === "string" && fields.answered_at ? Date.parse(fields.answered_at) : Number.NaN;
  if (!Number.isFinite(answeredAt)) return null;
  const asked = askedAt ? Date.parse(askedAt) : Number.NaN;
  if (Number.isFinite(asked) && answeredAt < asked) return null;
  const label = typeof fields.label === "string" && fields.label ? fields.label : null;
  return { label, at: answeredAt };
}

/** The delay before the next refetch of the case, backing off; null once the window is spent. */
export function applyPollDelay(elapsedMs: number): number | null {
  if (elapsedMs >= APPLY_WINDOW_MS) return null;
  if (elapsedMs < 10_000) return 2_000;
  if (elapsedMs < 30_000) return 3_000;
  return 5_000;
}

/**
 * Every one of these gates (by their decision's work item id) has an answer kept
 * on the page: the waiting card then reads "Decision being applied" instead of
 * "Waits for a decision" (#2746 UX round 1, B2). False for an empty list.
 */
export function useGatesAnswered(workItemIds: readonly (string | null | undefined)[]): boolean {
  return useSyncExternalStore(
    subscribe,
    () => workItemIds.length > 0 && workItemIds.every((id) => !!id && answers.has(id)),
    () => false,
  );
}

export type GateApplyingPhase = "applying" | "timeout" | "failed";

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
