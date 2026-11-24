// SPDX-License-Identifier: Apache-2.0

/**
 * The case page's history with the fleet runs that worked on the case (#915):
 * one list, newest first, so a reader sees who did what and when, the case's
 * own events (state, owner, issues) beside the agents' runs. Pure: the page
 * reads `GET /ctem-cases/{id}` (`history`) and `GET /fleets/case-runs/{id}`.
 */
import type { CtemCaseEvent, CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import type { TimelineOutcome } from "@/lib/ctem-fleet-timeline";

/**
 * The line of a run on the case History, worded by what is true of the run:
 * queued, running, or ended (UX round 1 on #920, B1: a run that has not run
 * never reads "Ran"). @i18n-keys
 */
export const CASE_RUN_LINE = {
  queued: "Fleet step queued on this case",
  running: "Running a fleet step on this case",
  ended: "Ran a fleet step on this case",
} as const;

/** Which line of `CASE_RUN_LINE` a run with this badge reads. */
export function caseRunLine(outcome: TimelineOutcome | null): keyof typeof CASE_RUN_LINE {
  if (outcome === "pending") return "queued";
  if (outcome === "running") return "running";
  return "ended";
}

export type CaseActivityItem =
  | { kind: "event"; key: string; at: string | null; event: CtemCaseEvent }
  | { kind: "run"; key: string; at: string; run: CtemCaseRun };

function time(at: string | null | undefined): number {
  const t = at ? Date.parse(at) : Number.NaN;
  return Number.isNaN(t) ? Number.NEGATIVE_INFINITY : t;
}

/**
 * The case events and the runs merged newest first. An event with no date
 * goes last; at the same time the run is listed above the event that
 * started it (newest first: the run follows the event that opened or
 * changed the case).
 */
export function caseActivity(history: readonly CtemCaseEvent[], runs: readonly CtemCaseRun[] | undefined): CaseActivityItem[] {
  const items: CaseActivityItem[] = [
    ...history.map((event, i) => ({ kind: "event" as const, key: `event-${i}`, at: event.at ?? null, event })),
    ...(runs ?? []).map((run) => ({ kind: "run" as const, key: `run-${run.id}`, at: run.created_at, run })),
  ];
  return items
    .map((item, order) => ({ item, order }))
    .sort((a, b) => {
      const byTime = time(b.item.at) - time(a.item.at);
      if (byTime !== 0 && !Number.isNaN(byTime)) return byTime;
      if (a.item.kind !== b.item.kind) return a.item.kind === "run" ? -1 : 1;
      return b.order - a.order;
    })
    .map(({ item }) => item);
}
