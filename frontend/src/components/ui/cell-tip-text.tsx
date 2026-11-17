// SPDX-License-Identifier: Apache-2.0

import { useTriggerTabIndex } from "@/components/ui/row-target-context";
import { Tip } from "@/components/ui/tooltip";

/**
 * A free text cut to one line in a data table cell, the whole text in the shared tooltip.
 * A tab stop where the row is not one, none inside a routed row: the row is then the one
 * stop (#12070, UX round 1 B1).
 */
export function CellTipText({ text, testId }: { text: string; testId?: string }) {
  const tabIndex = useTriggerTabIndex();
  return (
    <Tip content={text}>
      <span
        tabIndex={tabIndex}
        data-testid={testId}
        className="block truncate rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        {text}
      </span>
    </Tip>
  );
}
