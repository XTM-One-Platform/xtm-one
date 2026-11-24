// SPDX-License-Identifier: Apache-2.0

/**
 * Who a case Waiting for decision waits on (#1503, UX r1 B3 on #1515): the `by`
 * of its open wait (`ctem_case_audit.open_wait`: a gate's reason, or the actor of
 * a person's move into waiting_decision) is never printed raw. A user id the
 * directory resolves gives the person's name; anything else (a user the
 * directory does not return, a role, a policy or the loop's reason text) is "a
 * person", the gate waiting for one; no `by` at all is "a decision".
 * Pure: the list cell, its CSV and the Kanban card share it.
 */
import { isUserId } from "./case-gates";

export type CaseWaitWho = { kind: "name"; name: string } | { kind: "person" } | { kind: "decision" };

export function caseWaitWho(by: string | null | undefined, ownerName: (id: string | null) => string | null): CaseWaitWho {
  const text = (by ?? "").trim();
  if (!text) return { kind: "decision" };
  if (isUserId(text)) {
    const name = ownerName(text);
    if (name) return { kind: "name", name };
  }
  return { kind: "person" };
}

/** The fallbacks as a cell reads them (a capitalised noun), translated where drawn. @i18n-keys */
const CASE_WAIT_WHO_LABEL = {
  person: "A person",
  decision: "A decision",
} as const;

/** The cell text of a wait's who: the name, or its translated fallback. */
export function caseWaitWhoLabel(who: CaseWaitWho, t: (key: string) => string): string {
  return who.kind === "name" ? who.name : t(CASE_WAIT_WHO_LABEL[who.kind]);
}
