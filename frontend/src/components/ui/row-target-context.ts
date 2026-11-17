// SPDX-License-Identifier: Apache-2.0

import { createContext, useContext } from "react";

/**
 * True inside a data table row that is itself the keyboard target (it opens a page or a
 * route): a cell then adds no tab stop of its own, so a routed row is ONE tab stop (#12070).
 */
export const RowTargetContext = createContext(false);

/** Whether the enclosing table row is the keyboard target. */
export function useRowIsTarget(): boolean {
  return useContext(RowTargetContext);
}

/**
 * The tabIndex of a tooltip trigger (a date, a mark, a badge, a cut text): a tab stop
 * everywhere, none inside a routed row, where the row is the one stop (UX round 1 B1, B2).
 */
export function useTriggerTabIndex(): 0 | undefined {
  return useRowIsTarget() ? undefined : 0;
}
