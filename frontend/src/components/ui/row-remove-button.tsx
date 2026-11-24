// SPDX-License-Identifier: Apache-2.0

import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";

/**
 * The Remove action of an editable table row (a settings row editor: primary assets, tech
 * stack, departments): one ghost icon button of 32 px with a 16 px trash glyph,
 * so every row editor of a page draws the same row action (UX r1 B1 on #10853). The label
 * names what goes ("Remove the department"), already translated by the caller.
 */
export function RowRemoveButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Button variant="ghost" size="icon" className="h-8 w-8" aria-label={label} onClick={onClick}>
      <Icon name="trash-2" size={16} aria-hidden />
    </Button>
  );
}
