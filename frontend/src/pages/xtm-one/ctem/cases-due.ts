// SPDX-License-Identifier: Apache-2.0

/**
 * The time flags of a CTEM case (dev-docs/ctem-cases.md section 7.4): overdue
 * (an open case past its due date) and expiring (an accepted risk whose
 * acceptance ends within {@link EXPIRING_DAYS} days). One definition for the
 * command center row and the Cases list filter it links to, so the figure
 * and the filtered list always agree.
 */
import { isOpenCaseState, type CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";

/** How far ahead an accepted risk counts as expiring. */
export const EXPIRING_DAYS = 14;

const DAY_MS = 24 * 3600 * 1000;

export type CaseDueFlag = "overdue" | "expiring";

/** The flags of one case at `now` (ms since the epoch). */
export function caseDueFlags(c: Pick<CtemCaseSummary, "state" | "due_at" | "accepted_until">, now: number): CaseDueFlag[] {
  const flags: CaseDueFlag[] = [];
  const open = isOpenCaseState(c.state);
  if (open && c.due_at && Date.parse(c.due_at) < now) flags.push("overdue");
  if (c.state === "accepted_risk" && c.accepted_until) {
    const until = Date.parse(c.accepted_until);
    if (until >= now && until - now <= EXPIRING_DAYS * DAY_MS) flags.push("expiring");
  }
  return flags;
}

/**
 * The Cases list on one state, on all states with one due flag, or on the open
 * cases of one CTEM phase (`c_f_phase`, #1621; with `waiting`, those waiting for a decision).
 */
export function casesListHref(
  target: { state: string } | { due: CaseDueFlag } | { phase: string; waiting?: boolean },
): string {
  if ("phase" in target) {
    return target.waiting ? `/ctem/cases?state=waiting_decision&c_f_phase=${target.phase}` : `/ctem/cases?c_f_phase=${target.phase}`;
  }
  return "state" in target ? `/ctem/cases?state=${target.state}` : `/ctem/cases?state=all&c_f_due=${target.due}`;
}
