// SPDX-License-Identifier: Apache-2.0

import { CopyPlus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";

/**
 * The page header's Duplicate action of an entity that forks into an editable
 * copy (a fleet, a skill: #9174). A fork route sits behind the AI gate, so the
 * button is off with AI. Disabled, the button is wrapped (`Tip wrap`) so the
 * AI-off reason stays reachable by hover and by Tab: a natively disabled button
 * takes neither. `hint` is the tooltip with AI on (already translated).
 * `size`: "default" (36 px) on a header row brought to standard (the fleet
 * page, #10162 R2); "sm" where the row has not moved yet.
 */
export function DuplicateButton({
  onDuplicate,
  hint,
  size = "sm",
}: {
  onDuplicate: () => void;
  hint: string;
  size?: "sm" | "default";
}) {
  const { t_i18n } = useFormatter();
  const aiOff = useAiActionDisabled();
  const aiOffReason = useAiDisabledReason();
  return (
    <Tip content={aiOff ? aiOffReason ?? hint : hint} wrap={aiOff}>
      <Button type="button" variant="outline" size={size} className="gap-1.5" disabled={aiOff} onClick={onDuplicate}>
        <CopyPlus className="h-4 w-4" aria-hidden />
        {t_i18n("Duplicate")}
      </Button>
    </Tip>
  );
}
