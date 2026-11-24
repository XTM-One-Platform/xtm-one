// SPDX-License-Identifier: Apache-2.0

/**
 * The quick filters of the CTEM Cases list (#1503): one full list, no state tabs.
 * A scope says which statuses the list holds (`?state=`: open by default, closed,
 * all, or one status a link names); toggles narrow it further and combine
 * (`?c_q=mine,overdue`). Pure: the page draws the chips and translates the labels.
 */
import { CTEM_CASE_STATES, isOpenCaseState, type CtemCaseState, type CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { caseDueFlags } from "./cases-due";

/** The scopes a chip sets; a link may also name one status (`?state=remediating`). */
export const CASE_SCOPES = ["open", "waiting_decision", "closed", "all"] as const;
export type CaseScopeChip = (typeof CASE_SCOPES)[number];
export type CaseScope = CaseScopeChip | CtemCaseState;

/** The toggles, combinable with the scope and with each other. */
export const CASE_TOGGLES = ["mine", "unassigned", "overdue", "critical_high"] as const;
export type CaseToggle = (typeof CASE_TOGGLES)[number];

/**
 * English labels, translated where drawn. @i18n-keys
 * The open and closed scopes have their own keys: the bare "Open" is the verb
 * buttons use ("Ouvrir") and a scope names the cases it holds, plural adjective
 * ("Ouverts", "Fermés"). The waiting scope has its own key too: the status
 * label stays "En attente de décision" where a case shows it, and the chip reads
 * "En attente" so the toolbar keeps one row at 1080 px in every locale.
 */
export const CASE_QUICK_FILTER_LABEL = {
  open: "Open (case scope)",
  waiting_decision: "Waiting (case scope)",
  closed: "Closed (case scope)",
  all: "All",
  mine: "Mine",
  unassigned: "Unassigned",
  overdue: "Overdue",
  critical_high: "Critical and high",
} as const satisfies Record<CaseScopeChip | CaseToggle, string>;

/** The first-model states a saved link may still carry, read as the open scope. */
const LEGACY_SCOPES: ReadonlySet<string> = new Set(["needs_attention", "in_progress"]);

/** The `?state=` value read back: open when absent, unknown or a first-model state. */
export function readCaseScope(param: string | null): CaseScope {
  if (!param || LEGACY_SCOPES.has(param)) return "open";
  if ((CASE_SCOPES as readonly string[]).includes(param)) return param as CaseScopeChip;
  if ((CTEM_CASE_STATES as readonly string[]).includes(param)) return param as CtemCaseState;
  return "open";
}

/** The `?c_q=` toggles read back, unknown values dropped, in their chip order. */
export function readCaseToggles(param: string | null): CaseToggle[] {
  const asked = new Set((param ?? "").split(",").map((s) => s.trim()));
  return CASE_TOGGLES.filter((t) => asked.has(t));
}

/** The `?c_q=` value of a toggle set; empty when none. */
export function writeCaseToggles(toggles: readonly CaseToggle[]): string {
  return CASE_TOGGLES.filter((t) => toggles.includes(t)).join(",");
}

/** True when the case is in the scope. */
function inCaseScope(scope: CaseScope, c: Pick<CtemCaseSummary, "state">): boolean {
  if (scope === "all") return true;
  if (scope === "open") return isOpenCaseState(c.state);
  if (scope === "closed") return c.state === "resolved" || c.state === "accepted_risk";
  return c.state === scope;
}

type ToggleCase = Pick<CtemCaseSummary, "state" | "owner_user_id" | "due_at" | "accepted_until" | "priority" | "sla_breached_at">;

/** True when an open case waits on nobody: the sweep found no gate and no reminder, and nothing moved it since (#1736). */
export function isStalledCase(c: Pick<CtemCaseSummary, "state" | "stalled_since">): boolean {
  return Boolean(c.stalled_since) && isOpenCaseState(c.state);
}

/** Whom an open case waits on, the "Waits on" facet of the list (#1736): a person (a gate), nobody (stalled), else none. */
export function caseWaitFacet(c: Pick<CtemCaseSummary, "state" | "stalled_since" | "waiting_on">): ("person" | "nobody")[] {
  if (c.state === "waiting_decision" && c.waiting_on) return ["person"];
  return isStalledCase(c) ? ["nobody"] : [];
}

/** True when the case passes the toggle; `me` is the caller's user id. */
function passesCaseToggle(toggle: CaseToggle, c: ToggleCase, now: number, me: string | null): boolean {
  if (toggle === "mine") return me !== null && c.owner_user_id === me;
  if (toggle === "unassigned") return !c.owner_user_id;
  if (toggle === "overdue") return Boolean(c.sla_breached_at && isOpenCaseState(c.state)) || caseDueFlags(c, now).includes("overdue");
  return c.priority === "critical" || c.priority === "high";
}

/** The cases a scope and a toggle set keep. */
export function applyCaseQuickFilters<T extends ToggleCase>(
  cases: readonly T[],
  scope: CaseScope,
  toggles: readonly CaseToggle[],
  now: number,
  me: string | null,
): T[] {
  return cases.filter((c) => inCaseScope(scope, c) && toggles.every((t) => passesCaseToggle(t, c, now, me)));
}

/**
 * The count a chip shows: a scope chip counts its scope under the active toggles,
 * a toggle chip counts what the list would hold with it added. So each count is
 * what one click on the chip shows.
 */
export function caseQuickFilterCount<T extends ToggleCase>(
  chip: CaseScopeChip | CaseToggle,
  cases: readonly T[],
  scope: CaseScope,
  toggles: readonly CaseToggle[],
  now: number,
  me: string | null,
): number {
  if ((CASE_SCOPES as readonly string[]).includes(chip)) {
    return applyCaseQuickFilters(cases, chip as CaseScopeChip, toggles, now, me).length;
  }
  const toggle = chip as CaseToggle;
  const next = toggles.includes(toggle) ? toggles : [...toggles, toggle];
  return applyCaseQuickFilters(cases, scope, next, now, me).length;
}
