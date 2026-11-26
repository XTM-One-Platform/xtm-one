// SPDX-License-Identifier: Apache-2.0

/**
 * The actions of a case's Mobilize step (#2061), read from its history and its
 * tickets: each change applied through a bound integration under the gate
 * `ctem.production_change` (an `applied_change` event, backend
 * ctem_case_loop.record_applied_change) and each ticket the case holds. The
 * mode of the case says how its remediation is carried out: a ticket, an
 * applied change, or both.
 */
import type { CtemCaseEvent, CtemCaseTicket } from "@/hooks/api/useCtemCasesApi";

/** How a remediation is carried out (the coordinator output's `mode`). */
export type MobilizationMode = "ticket" | "applied_change" | "both";

/** The words of a mode, short and plain (13bis.10). @i18n-keys */
export const MOBILIZATION_MODE_LABEL = {
  ticket: "Ticket",
  applied_change: "Applied change",
  both: "Ticket and applied change",
} as const satisfies Record<MobilizationMode, string>;

/** The words of an applied change's kind (backend APPLIED_CHANGE_KINDS). @i18n-keys */
export const APPLIED_CHANGE_KIND_LABEL = {
  ioc_prevention: "Indicators blocked",
  detection_rule: "Detection rule deployed",
  configuration: "Configuration changed",
  control_restored: "Control restored",
} as const;

export type AppliedChangeKind = keyof typeof APPLIED_CHANGE_KIND_LABEL;

/** One change applied through an integration, as the case history stores it. */
export interface AppliedChange {
  at: string | null;
  integration: string;
  kind: AppliedChangeKind | null;
  pushed: string;
  ids: string[];
  rollback: string;
}

export function isAppliedChangeKind(value: string | null | undefined): value is AppliedChangeKind {
  return value != null && Object.prototype.hasOwnProperty.call(APPLIED_CHANGE_KIND_LABEL, value);
}

/**
 * The words that name an applied-change event's kind in the History Action text and its
 * CSV (UX round 1 on #2076, B1): rows of several changes are told apart by their kind,
 * as the sibling rows name their gate or signal. Null for another event or an unknown kind.
 */
export function appliedChangeKindLabel(event: Pick<CtemCaseEvent, "kind" | "to">): string | null {
  return event.kind === "applied_change" && isAppliedChangeKind(event.to) ? APPLIED_CHANGE_KIND_LABEL[event.to] : null;
}

/** The `{ids, rollback}` evidence of an applied change; a value that is not that JSON reads as empty. */
function parseEvidence(evidence: string | null): { ids: string[]; rollback: string } {
  if (!evidence) return { ids: [], rollback: "" };
  try {
    const parsed: unknown = JSON.parse(evidence);
    if (!parsed || typeof parsed !== "object") return { ids: [], rollback: "" };
    const record = parsed as { ids?: unknown; rollback?: unknown };
    const ids = Array.isArray(record.ids) ? record.ids.filter((id): id is string => typeof id === "string" && id.trim() !== "") : [];
    const rollback = typeof record.rollback === "string" ? record.rollback : "";
    return { ids, rollback };
  } catch {
    return { ids: [], rollback: "" };
  }
}

/** The changes applied on a case, newest first. */
export function appliedChanges(history: readonly CtemCaseEvent[]): AppliedChange[] {
  return history
    .filter((e) => e.kind === "applied_change")
    .map((e) => {
      const { ids, rollback } = parseEvidence(e.evidence);
      return {
        at: e.at,
        integration: e.from ?? "",
        kind: isAppliedChangeKind(e.to) ? e.to : null,
        pushed: e.reason ?? "",
        ids,
        rollback,
      };
    })
    .sort((a, b) => (b.at ?? "").localeCompare(a.at ?? ""));
}

/** The mode of the case's Mobilize step from what it holds; null when nothing was mobilized yet. */
export function mobilizationMode(changes: readonly AppliedChange[], tickets: readonly CtemCaseTicket[] | undefined): MobilizationMode | null {
  const hasTicket = (tickets ?? []).length > 0;
  const hasChange = changes.length > 0;
  if (hasTicket && hasChange) return "both";
  if (hasChange) return "applied_change";
  if (hasTicket) return "ticket";
  return null;
}
