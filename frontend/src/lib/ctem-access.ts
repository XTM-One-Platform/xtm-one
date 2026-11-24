// SPDX-License-Identifier: Apache-2.0

import type { User } from "@/types";

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
