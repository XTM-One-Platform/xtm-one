// SPDX-License-Identifier: Apache-2.0

import type { User } from "@/types";

/**
 * Whether the person reaches CTEM at all (#2441): an administrator, or a member
 * of a group with a CTEM view or decide right. Without it the CTEM menu, its
 * routes and the command palette entries are hidden; the server answers 403.
 */
export function ctemMayView(user: Pick<User, "ctem_access"> | null | undefined, isAdmin: boolean): boolean {
  return isAdmin || user?.ctem_access === "admin" || user?.ctem_access === "decide" || user?.ctem_access === "view";
}

/**
 * Whether the home page is the XTM One command center (#2441): XTM One mode AND a
 * CTEM right. The command center is CTEM through and through (posture, stages,
 * cases), so a person without a right gets the mode-neutral home instead, never
 * widgets that answer 403 or links to pages that send them back home.
 */
export function homeIsCtemCommandCenter(
  xtmOneMode: boolean,
  user: Pick<User, "ctem_access"> | null | undefined,
  isAdmin: boolean,
): boolean {
  return xtmOneMode && ctemMayView(user, isAdmin);
}

/** Whether the person may act on system signals and cases: an administrator or a CTEM decide member (#1748). */
export function ctemMayDecide(user: Pick<User, "ctem_access"> | null | undefined, isAdmin: boolean): boolean {
  return isAdmin || user?.ctem_access === "admin" || user?.ctem_access === "decide";
}

/**
 * Whether the person may answer a case gate's decision item: anyone on a private
 * item, an administrator or a CTEM decide member on a company-managed one. The
 * server checks the recipient again at the answer.
 */
export function mayAnswerCaseGate(
  item: { is_company_managed?: boolean | null },
  user: Pick<User, "ctem_access"> | null | undefined,
  isAdmin: boolean,
): boolean {
  return !item.is_company_managed || ctemMayDecide(user, isAdmin);
}
