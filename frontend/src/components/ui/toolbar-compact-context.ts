// SPDX-License-Identifier: Apache-2.0

import { createContext, useContext } from "react";

/**
 * True inside a DataTable toolbar drawn with its compact controls (a table under 896 px,
 * or one whose full labels were measured past its box): Columns, the page size and
 * Export CSV then drop their visible label, and a control a page adds to the toolbar
 * reads this to do the same, so the row keeps fitting (UX r2 B1 on #1963).
 */
export const ToolbarCompactContext = createContext(false);

/** Whether the enclosing DataTable toolbar draws its compact controls. */
export function useToolbarCompact(): boolean {
  return useContext(ToolbarCompactContext);
}
