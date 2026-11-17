/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The fleet page header's "Modify with AI" (#10162, UX verdict 5dcb88e3 R1,
 * R3, R4, R5): the first of the top-right actions, a secondary button that
 * opens and closes the docked `FleetStudioRail` and shows a selected state
 * while it is open. A manager's only (`fleet.can_manage`): a peer gets nothing.
 *
 * Disabled when AI is off or no assistant carries the fleet functions, with
 * the reason written under the header row (`FleetModifyWithAiReason`), in the
 * wording of the Fleets list's "Create with a prompt" (`fleetPromptReason`),
 * and tied to the button by `aria-describedby`.
 */
import { Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { FLEET_EDIT_JOB, fleetPromptReason } from "@/components/fleets/fleet-handoff";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { useToolAgents } from "@/hooks/useToolAgents";
import { useFormatter } from "@/i18n";

/** The id of the reason line, which the button names with `aria-describedby`. */
const FLEET_MODIFY_WITH_AI_REASON_ID = "fleet-modify-with-ai-reason";

/** Whether the action is off, and why (`null` when it is on). */
export function useFleetModifyWithAi(): { disabled: boolean; reason: string | null } {
  const { t_i18n } = useFormatter();
  const editors = useToolAgents(FLEET_EDIT_JOB);
  const aiOff = useAiActionDisabled();
  const aiReason = useAiDisabledReason();
  const reason = fleetPromptReason(t_i18n, { aiOff, aiReason, none: editors.none });
  return { disabled: aiOff || editors.resolving || editors.none, reason };
}

export function FleetModifyWithAiButton({
  open,
  onToggle,
  disabled,
  reason,
}: {
  open: boolean;
  onToggle: () => void;
  disabled: boolean;
  reason: string | null;
}) {
  const { t_i18n } = useFormatter();
  return (
    <Button
      type="button"
      variant="secondary"
      // Selected while the panel is open (UX r1 B3): the accent tint and border; closed it is a plain
      // secondary like Duplicate. `data-open`, not `data-state`: the design system styles its own states.
      className={cn("gap-1.5", open && "border-accent bg-accent/10 text-accent hover:bg-accent/15")}
      onClick={onToggle}
      // An open panel stays closable whatever happened to the agents meanwhile.
      disabled={disabled && !open}
      aria-pressed={open}
      aria-describedby={reason ? FLEET_MODIFY_WITH_AI_REASON_ID : undefined}
      data-open={open ? "true" : "false"}
      data-testid="fleet-modify-with-ai"
    >
      <Sparkles className="h-4 w-4" aria-hidden />
      {t_i18n("Modify with AI")}
    </Button>
  );
}

/** The reason line under the header row; nothing when the action is on. */
export function FleetModifyWithAiReason({ reason }: { reason: string | null }) {
  if (!reason) return null;
  return (
    <p
      id={FLEET_MODIFY_WITH_AI_REASON_ID}
      className="ml-auto max-w-sm text-right content-compact text-default-secondary"
      data-testid="fleet-modify-with-ai-reason"
    >
      {reason}
    </p>
  );
}
