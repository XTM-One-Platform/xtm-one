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
 * The instructions-governed part of human approval (#10176, B2-I of #10000):
 * where the fleet's persona, guardrails and skills put it on the same scale as
 * the enforced level, between-step positions included (`instructions_position`,
 * #10168). Never a platform gate: the line says so in plain words.
 *
 * Not set: the line says the fleet's instructions decide, with no position
 * claimed. A manager may move it (`PUT /fleets/{id}/instructions-position`,
 * audited); toward more autonomy is administrator only, the same rule as the
 * level, so those positions are disabled for others. "Tell the members"
 * (checked by default) decides whether the move becomes a line in the members'
 * run prompt; unchecked, the position is recorded for display only (#10255).
 * The switch starts from what is saved: a set position reads its stored
 * `announced` flag, so a reload never shows "Tell the members" on for a
 * position recorded without telling them (UX r1 B1 of #10261).
 */
import { useState } from "react";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useSetFleetInstructionsPosition, type FleetInstructionsPosition } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { INSTRUCTIONS_POSITION_LABELS, INSTRUCTIONS_POSITIONS, isInstructionsRaise } from "./fleet-autonomy";
import { fleetRefusalMessage } from "./fleet-refusals";

export function FleetInstructionsPositionControl({
  fleetId,
  position,
  canManage,
  isAdmin,
  announced,
}: {
  fleetId: string;
  position: FleetInstructionsPosition | null;
  canManage: boolean;
  isAdmin: boolean;
  /** Saved `instructions_position_announced`; read only when a position is set. */
  announced?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const save = useSetFleetInstructionsPosition(fleetId);
  const [announce, setAnnounce] = useState(position ? (announced ?? true) : true);

  const move = async (next: FleetInstructionsPosition) => {
    if (next === position) return;
    try {
      await save.mutateAsync({ position: next, announce });
      toast({
        title: t_i18n("Instructions position set to {position}", { position: t_i18n(INSTRUCTIONS_POSITION_LABELS[next]) }),
        description: announce
          ? t_i18n("The members are told in their next run. No tool call pauses because of it.")
          : t_i18n("Shown on this page only: the members' instructions are unchanged. No tool call pauses because of it."),
        variant: "success",
      });
    } catch (err) {
      toast({ title: t_i18n("The instructions position was not changed"), description: fleetRefusalMessage(err, t_i18n), variant: "error" });
    }
  };

  return (
    // display: contents, so the label and the control take the two columns of the
    // strip's sources grid, aligned with the enforced row (UX r1 B2 of #10178).
    <div className="contents content-compact" data-testid="fleet-instructions-position">
      <span className="content-compact text-default-secondary" data-testid="fleet-instructions-position-label">
        {t_i18n("Set by the fleet's instructions, not enforced by the platform")}
      </span>
      {canManage ? (
        <div className="flex flex-wrap items-center gap-4 justify-self-start">
          <Select value={position ?? undefined} onValueChange={(value) => void move(value as FleetInstructionsPosition)} disabled={save.isPending}>
            <SelectTrigger className="w-fit min-w-[220px]" aria-label={t_i18n("Instructions position")} data-testid="fleet-instructions-position-select">
              <SelectValue placeholder={t_i18n("Not set")} />
            </SelectTrigger>
            <SelectContent>
              {INSTRUCTIONS_POSITIONS.map((key) => (
                <SelectItem key={key} value={key} disabled={!isAdmin && isInstructionsRaise(position, key)}>
                  {t_i18n(INSTRUCTIONS_POSITION_LABELS[key])}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <label className="flex items-center gap-2 content-compact text-default-secondary" data-testid="fleet-instructions-position-announce">
            <Switch aria-label={t_i18n("Tell the members")} checked={announce} onCheckedChange={setAnnounce} disabled={save.isPending} />
            {t_i18n("Tell the members")}
          </label>
        </div>
      ) : (
        <span className="content-compact font-medium text-default-primary" data-testid="fleet-instructions-position-value">
          {position ? t_i18n(INSTRUCTIONS_POSITION_LABELS[position]) : t_i18n("Not set")}
        </span>
      )}
    </div>
  );
}
