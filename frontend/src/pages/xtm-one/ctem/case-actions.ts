// SPDX-License-Identifier: Apache-2.0

/**
 * The actions a person takes on a CTEM case, in one place for the Cases bulk
 * bar and the case page (dev-docs/ctem-cases.md sections 6 and 7.3, #1751):
 * an action added here appears on both surfaces, and
 * `case-actions.test.ts` fails when they drift apart.
 *
 * `scope` says where an action is offered: `both` (one case or several),
 * `single` (a state transition the bulk bar does not offer). An action that
 * needs several cases would be `bulk`; none does today, since Merge from one
 * case picks the case to merge into.
 *
 * `caseActionAvailability` is the state rule of section 6 seen from the
 * page: an action the state refuses stays listed, disabled with its reason,
 * never hidden.
 */
import type { CtemCaseState } from "@/hooks/api/useCtemCasesApi";

export type CaseActionId = "assign" | "due" | "accept" | "merge" | "start" | "resolve" | "reopen";

type CaseActionScope = "both" | "bulk" | "single";

export interface CaseActionDef {
  id: CaseActionId;
  /** English label, translated where drawn; the same text on the bar and in the menu. */
  label: string;
  /** The label of the case page menu row when it differs (Merge from one case names its target). */
  singleLabel?: string;
  scope: CaseActionScope;
}

/** In the order of the bulk bar: the secondary actions, then Start, its filled primary. */
export const CASE_ACTIONS: readonly CaseActionDef[] = [
  { id: "assign", label: "Assign owner", scope: "both" },
  { id: "due", label: "Set due date", scope: "both" },
  { id: "accept", label: "Accept risk", scope: "both" },
  { id: "merge", label: "Merge", singleLabel: "Merge into another case", scope: "both" },
  { id: "start", label: "Start", scope: "both" },
  { id: "resolve", label: "Resolve", scope: "single" },
  { id: "reopen", label: "Reopen", scope: "single" },
];

/** The actions of the Cases bulk bar, in its order. */
export const BULK_CASE_ACTIONS: readonly CaseActionId[] = CASE_ACTIONS.filter((a) => a.scope !== "single").map((a) => a.id);

/** The actions offered on one case (buttons and More actions menu together). */
export const SINGLE_CASE_ACTIONS: readonly CaseActionId[] = CASE_ACTIONS.filter((a) => a.scope !== "bulk").map((a) => a.id);

export function caseActionDef(id: CaseActionId): CaseActionDef {
  const def = CASE_ACTIONS.find((a) => a.id === id);
  if (!def) throw new Error(`unknown case action ${id}`);
  return def;
}

/** Before remediation: Start is the next step. */
const NOT_STARTED: readonly CtemCaseState[] = ["new", "analyzing", "waiting_decision"];
/** In remediation or validation: Resolve is the next step. */
const IN_PROGRESS: readonly CtemCaseState[] = ["remediating", "validating"];

export interface CaseActionAvailability {
  enabled: boolean;
  /** Not offered at all: the action has no meaning in this state (Reopen on a case still open). */
  hidden?: boolean;
  /** English reason, translated where drawn; set when `enabled` is false. */
  reason?: string;
}

const ENABLED: CaseActionAvailability = { enabled: true };

function closedReason(state: CtemCaseState): string {
  if (state === "accepted_risk") return "Accepted risk: reopen first";
  if (state === "resolved") return "Resolved: reopen first";
  return "Mitigated, watching: reopen first";
}

/**
 * Whether one case in `state` takes `action` (section 6 transitions): owner
 * and due date at any state; Start before remediation; Resolve until resolved;
 * Accept risk until accepted or resolved; Reopen from a closed or watched
 * state; Merge while the case is still worked.
 */
export function caseActionAvailability(action: CaseActionId, state: CtemCaseState): CaseActionAvailability {
  const notStarted = NOT_STARTED.includes(state);
  const inProgress = IN_PROGRESS.includes(state);
  const open = notStarted || inProgress;
  switch (action) {
    case "assign":
    case "due":
      return ENABLED;
    case "start":
      if (notStarted) return ENABLED;
      return { enabled: false, reason: inProgress ? "Already started" : closedReason(state) };
    case "resolve":
      if (state === "resolved") return { enabled: false, reason: "Already resolved" };
      if (state === "accepted_risk") return { enabled: false, reason: closedReason(state) };
      return ENABLED;
    case "accept":
      if (state === "accepted_risk") return { enabled: false, reason: "Risk already accepted" };
      if (state === "resolved") return { enabled: false, reason: closedReason(state) };
      return ENABLED;
    case "reopen":
      return open ? { enabled: false, hidden: true } : ENABLED;
    case "merge":
      return open ? ENABLED : { enabled: false, reason: closedReason(state) };
  }
}

/**
 * The page's filled primary for a state: the next step (Start before
 * remediation, Resolve during it, Reopen once closed or watched).
 */
export function primaryCaseAction(state: CtemCaseState): "start" | "resolve" | "reopen" {
  if (NOT_STARTED.includes(state)) return "start";
  if (IN_PROGRESS.includes(state)) return "resolve";
  return "reopen";
}

/**
 * The case page header: which actions are buttons (Accept risk while the state
 * takes it, then the primary) and which go to the More actions menu (every
 * other single-case action, disabled with its reason when the state refuses
 * it). `canDecide` is the one permission check: a viewer who may only read the
 * case gets no action at all once read and decide access are told apart.
 */
export function caseHeaderActions(
  state: CtemCaseState,
  canDecide = true,
): { buttons: CaseActionId[]; menu: { id: CaseActionId; availability: CaseActionAvailability }[] } {
  if (!canDecide) return { buttons: [], menu: [] };
  const primary = primaryCaseAction(state);
  const buttons: CaseActionId[] = [];
  if (caseActionAvailability("accept", state).enabled) buttons.push("accept");
  buttons.push(primary);
  const menu = SINGLE_CASE_ACTIONS.filter((id) => !buttons.includes(id))
    .map((id) => ({ id, availability: caseActionAvailability(id, state) }))
    .filter((row) => !row.availability.hidden);
  return { buttons, menu };
}
