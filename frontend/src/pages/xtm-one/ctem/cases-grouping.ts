// SPDX-License-Identifier: Apache-2.0

/**
 * Grouping of the CTEM Cases list (#1176, dev-docs/ctem-cases.md section 7.2):
 * by owner, status, CTEM phase (#1503), priority or fix family. Pure: the page
 * builds the `DataTableGroupBy` from these and translates the labels; the
 * Kanban lays its columns out by status or by phase from the same helpers.
 */
import { CTEM_CASE_STATES, type CtemCasePriority, type CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { CTEM_STAGE_KEYS, type CtemStageKey } from "@/lib/constants";
import { CASE_PRIORITY_LABEL, CASE_PRIORITY_RANK, CASE_STATE_LABEL } from "./cases-shared";

export const CASE_GROUP_BYS = ["none", "owner", "state", "phase", "priority", "fix_family"] as const;
export type CaseGroupBy = (typeof CASE_GROUP_BYS)[number];

/** English labels, translated where drawn. */
export const CASE_GROUP_BY_LABEL: Record<CaseGroupBy, string> = {
  none: "No grouping",
  owner: "Owner",
  state: "Status",
  phase: "CTEM phase",
  priority: "Priority",
  fix_family: "Fix family",
};

/**
 * The CTEM phases a case moves through, in order, with the name the case page
 * gives each (backend ctem_case_audit.PHASES: `remediation` is the Mobilization phase).
 */
export const CASE_PHASES: readonly CtemStageKey[] = CTEM_STAGE_KEYS;
const CASE_PHASE_LABEL: Record<CtemStageKey, string> = {
  scoping: "Scoping",
  discovery: "Discovery",
  prioritization: "Prioritization",
  validation: "Validation",
  remediation: "Mobilization",
};
/** The group of a case whose phase is not known yet (no event, an older API), listed last. */
export const NO_PHASE = "";

/** The fix families (`remediation_kind`), English, translated where drawn. */
const FIX_FAMILY_LABEL: Readonly<Record<string, string>> = {
  upgrade: "Upgrade",
  patch: "Patch",
  config: "Configuration",
  course_of_action: "Course of action",
  manual: "Manual",
  close_exposure: "Close exposure",
  decommission: "Decommission",
  rotate_credential: "Rotate credential",
  restore_control: "Restore control",
  supplier_action: "Supplier action",
  restore_coverage: "Restore coverage",
};

/** The `?c_group=` value read back; anything unknown is no grouping. */
export function readCaseGroupBy(param: string | null): CaseGroupBy {
  return (CASE_GROUP_BYS as readonly string[]).includes(param ?? "") ? (param as CaseGroupBy) : "none";
}

/** The group of a case; an unowned case, or one with no phase yet, is "" (listed last). */
export function caseGroupValue(groupBy: Exclude<CaseGroupBy, "none">, c: CtemCaseSummary): string {
  if (groupBy === "owner") return c.owner_user_id ?? "";
  if (groupBy === "state") return c.state;
  if (groupBy === "phase") return c.phase ?? NO_PHASE;
  if (groupBy === "priority") return c.priority;
  return c.remediation_kind;
}

/**
 * The order of the groups, smallest first: statuses and phases in their life
 * order, priorities critical first, unowned cases and unknown phases last;
 * families by key (`undefined`).
 */
export function caseGroupRank(groupBy: Exclude<CaseGroupBy, "none">): ((value: string) => number) | undefined {
  if (groupBy === "state") return (value) => (CTEM_CASE_STATES as readonly string[]).indexOf(value);
  if (groupBy === "phase") {
    return (value) => {
      const index = (CASE_PHASES as readonly string[]).indexOf(value);
      return index === -1 ? CASE_PHASES.length : index;
    };
  }
  if (groupBy === "priority") return (value) => -(CASE_PRIORITY_RANK[value as CtemCasePriority] ?? 0);
  if (groupBy === "owner") return (value) => (value === "" ? 1 : 0);
  return undefined;
}

/** The English label of a group, before translation; an owner is named by the caller. */
export function caseGroupLabel(groupBy: Exclude<CaseGroupBy, "none">, value: string): string {
  if (groupBy === "state") return CASE_STATE_LABEL[value as keyof typeof CASE_STATE_LABEL] ?? value;
  if (groupBy === "phase") return CASE_PHASE_LABEL[value as CtemStageKey] ?? "Phase not known yet";
  if (groupBy === "priority") return CASE_PRIORITY_LABEL[value as CtemCasePriority] ?? value;
  if (groupBy === "fix_family") return FIX_FAMILY_LABEL[value] ?? value;
  return value;
}
