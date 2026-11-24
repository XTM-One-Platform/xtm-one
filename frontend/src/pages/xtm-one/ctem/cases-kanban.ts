// SPDX-License-Identifier: Apache-2.0

/**
 * The Kanban view of the CTEM Cases list (#1503, dev-docs/ctem-cases.md section 7.2):
 * pure rules, the page draws them. Columns are the eight case statuses in
 * lifecycle order, or the five CTEM phases; a card dragged across status
 * columns asks for that status change, and the rules a person cannot pass by a
 * drag are refused here before any request, each with its reason, the backend
 * (`ctem_cases.check_transition`) staying the authority.
 */
import { CTEM_CASE_STATES, type CtemCaseState, type CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { CASE_PHASES, NO_PHASE } from "./cases-grouping";

/** The view of the Cases list: the list (`/ctem/cases`) or the Kanban (`/ctem/cases/kanban`), a path, never a query (#10203). */
export type CaseView = "list" | "kanban";

const CASES_LIST_PATH = "/ctem/cases";
const CASES_KANBAN_PATH = "/ctem/cases/kanban";

/** The view a pathname draws; anything but the Kanban path is the list. */
export function readCaseView(pathname: string): CaseView {
  return pathname.replace(/\/+$/, "") === CASES_KANBAN_PATH ? "kanban" : "list";
}

/** The path of a view, the query (filters, grouping, search) kept by the caller. */
export function caseViewPath(view: CaseView): string {
  return view === "kanban" ? CASES_KANBAN_PATH : CASES_LIST_PATH;
}

/** What the Kanban lays its columns out by: status (default) or CTEM phase. */
export type KanbanColumnsBy = "state" | "phase";

/** The column keys in order; by phase, the cases with no phase yet get a last column. */
export function kanbanColumnKeys(by: KanbanColumnsBy, cases: readonly CtemCaseSummary[]): string[] {
  if (by === "state") return [...CTEM_CASE_STATES];
  const keys: string[] = [...CASE_PHASES];
  if (cases.some((c) => !c.phase)) keys.push(NO_PHASE);
  return keys;
}

/** The column of a case. */
function kanbanColumnOf(by: KanbanColumnsBy, c: CtemCaseSummary): string {
  return by === "state" ? c.state : (c.phase ?? NO_PHASE);
}

/** The cases of each column, in the order given (the list's sort), every column present. */
export function kanbanColumns(by: KanbanColumnsBy, cases: readonly CtemCaseSummary[]): Map<string, CtemCaseSummary[]> {
  const columns = new Map<string, CtemCaseSummary[]>(kanbanColumnKeys(by, cases).map((key) => [key, []]));
  for (const c of cases) columns.get(kanbanColumnOf(by, c))?.push(c);
  return columns;
}

/**
 * Statuses a drag never sets: each needs what a drop cannot carry (a reason,
 * evidence, a passing validation, an expiry), so the case page's status
 * dialog sets them. English, translated where drawn.
 */
const NEEDS_THE_CASE_PAGE: Partial<Record<CtemCaseState, string>> = {
  waiting_decision: "Waiting for decision needs what the case waits on: set it from the case page.",
  mitigated_watching: "Mitigated, watching needs a passing validation of the mitigation: set it from the case page.",
  resolved: "Resolving a case needs evidence: set it from the case page.",
  accepted_risk: "Accepting risk needs a reason and an expiry date: set it from the case page.",
};

/**
 * Why a person may not drop *c* on status *target*, or null when the drop is a
 * status change the API is asked for. A move to Remediating names an owner:
 * the case's own, else the person dropping it (`meId`), else it is refused.
 */
export function kanbanDropRefusal(c: CtemCaseSummary, target: CtemCaseState, meId: string | null): string | null {
  if (c.state === target) return null;
  const pageOnly = NEEDS_THE_CASE_PAGE[target];
  if (pageOnly) return pageOnly;
  if (target === "remediating" && !c.owner_user_id && !meId) return "Remediating needs an owner: assign the case first.";
  return null;
}

/** The update a drop sends: the status, and the person dropping as owner of an unowned case moved to Remediating. */
export function kanbanDropUpdate(c: CtemCaseSummary, target: CtemCaseState, meId: string | null): { state: CtemCaseState; owner_user_id?: string } {
  if (target === "remediating" && !c.owner_user_id && meId) return { state: target, owner_user_id: meId };
  return { state: target };
}
