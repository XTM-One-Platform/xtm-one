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
 * The confirmation of a bulk fleet control (#9769 S3c, design round 4 APPROVE,
 * frames 6b and 6c): Pause all, Resume or Human approval on the fleets
 * selected in the Fleets list, or the administrators' switch that pauses
 * every fleet automation of the platform.
 *
 * While it is open it reads what the call would change (`POST /fleets/bulk`
 * with `dry_run`: nothing is written), so the confirm button carries the real
 * count and the fleets the backend would refuse are named before anything
 * applies. A centred dialog, Cancel first, the one filled action last.
 */
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SegmentedFilter, type SegmentedFilterOption } from "@/components/ui/segmented-filter";
import { useToast } from "@/components/ui/toast";
import {
  useFleetBulkAction,
  useFleetBulkPreview,
  type FleetAutonomyLevel,
  type FleetBulkRequest,
  type FleetBulkResult,
} from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { AUTONOMY_LEVEL_HINTS, AUTONOMY_LEVEL_LABELS, AUTONOMY_LEVELS } from "./fleet-autonomy";
import { bulkSkipReason, fleetRefusalMessage } from "./fleet-refusals";

/** What the dialog confirms: an action on the selected fleets, or the every-fleet switch. */
export type FleetBulkIntent = { kind: "pause" | "resume" | "set_autonomy"; fleetIds: string[] } | { kind: "pause_every_fleet" };

function requestOf(intent: FleetBulkIntent, level: FleetAutonomyLevel): FleetBulkRequest {
  if (intent.kind === "pause_every_fleet") return { action: "pause", all_fleets: true };
  if (intent.kind === "set_autonomy") return { action: "set_autonomy", fleet_ids: intent.fleetIds, autonomy_level: level };
  return { action: intent.kind, fleet_ids: intent.fleetIds };
}

/** The fleets the call really changes: skipped ones and fleets with nothing to switch excluded (UX r1 B3). */
function changing(preview: FleetBulkResult | undefined): number {
  return preview ? preview.fleets.filter((fleet) => fleet.ok && fleet.changed > 0).length : 0;
}

export function FleetBulkDialog({
  intent,
  onClose,
  onApplied,
}: {
  /** `null`: closed. */
  intent: FleetBulkIntent | null;
  onClose: () => void;
  /** After the call applied (the selection is cleared by the caller). */
  onApplied?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [level, setLevel] = useState<FleetAutonomyLevel>("full_gates");
  const request = intent ? requestOf(intent, level) : null;
  const preview = useFleetBulkPreview(request);
  const bulk = useFleetBulkAction();

  if (!intent || !request) return null;

  const loaded = preview.data !== undefined;
  const count = preview.data?.changed ?? 0;
  const fleets = changing(preview.data);
  const refused = preview.data?.fleets.filter((fleet) => !fleet.ok) ?? [];
  const levelName = t_i18n(AUTONOMY_LEVEL_LABELS[level]);

  const title =
    intent.kind === "pause_every_fleet"
      ? t_i18n("Pause every fleet automation?")
      : intent.kind === "pause"
        ? t_i18n("{count, plural, one {Pause all automations of # fleet?} other {Pause all automations of # fleets?}}", { count: intent.fleetIds.length })
        : intent.kind === "resume"
          ? t_i18n("{count, plural, one {Resume # fleet?} other {Resume # fleets?}}", { count: intent.fleetIds.length })
          : t_i18n("{count, plural, one {Human approval on # fleet} other {Human approval on # fleets}}", { count: intent.fleetIds.length });

  // No count until the preview is read; at zero, say why nothing changes (UX r1 B4).
  const summary = !loaded
    ? t_i18n("Counting what changes...")
    : count === 0 && intent.kind === "pause_every_fleet"
      ? t_i18n("Nothing to pause: no fleet automation is switched on.")
      : count === 0 && intent.kind === "pause"
        ? t_i18n("Nothing to pause: no automation of the selected fleets is switched on.")
        : count === 0 && intent.kind === "resume"
          ? t_i18n("Nothing to resume: Pause all switched off no automation of the selected fleets.")
          : intent.kind === "pause_every_fleet"
      ? t_i18n("This pauses {count, plural, one {# automation} other {# automations}} on {fleets, plural, one {# fleet} other {# fleets}}, for every user. Runs in flight continue to their end. Each fleet's Resume restores exactly what this paused.", { count, fleets })
      : intent.kind === "pause"
        ? t_i18n("This pauses {count, plural, one {# automation} other {# automations}}. Runs in flight continue to their end. Each fleet's Resume restores exactly what this paused.", { count })
        : intent.kind === "resume"
          ? t_i18n("This switches back on {count, plural, one {# automation} other {# automations}}: the ones each fleet's Pause all switched off.", { count })
          : t_i18n("{count, plural, =0 {No selected fleet changes level.} one {# fleet changes level.} other {# fleets change level.}} Raising autonomy is open to administrators only.", { count });

  const confirmLabel =
    intent.kind === "set_autonomy"
      ? t_i18n("Set to {level}", { level: levelName })
      : !loaded || count === 0
        ? intent.kind === "resume"
          ? t_i18n("Resume")
          : t_i18n("Pause all")
        : intent.kind === "resume"
          ? t_i18n("{count, plural, one {Resume # automation} other {Resume # automations}}", { count })
          : t_i18n("{count, plural, one {Pause # automation} other {Pause # automations}}", { count });

  const apply = async () => {
    try {
      const result = await bulk.mutateAsync(request);
      const done =
        result.action === "set_autonomy"
          ? t_i18n("{count, plural, one {Human approval set to {level} on # fleet} other {Human approval set to {level} on # fleets}}", { count: result.changed, level: levelName })
          : result.action === "resume"
            ? t_i18n("{count, plural, one {# automation resumed} other {# automations resumed}}", { count: result.changed })
            : t_i18n("{count, plural, one {# automation paused} other {# automations paused}}", { count: result.changed });
      toast({
        title: done,
        description: result.refused > 0 ? t_i18n("{count, plural, one {# fleet skipped} other {# fleets skipped}}", { count: result.refused }) : undefined,
        variant: "success",
      });
      onApplied?.();
      onClose();
    } catch (err) {
      toast({ title: t_i18n("The fleets were not changed"), description: fleetRefusalMessage(err, t_i18n), variant: "error" });
    }
  };

  const levelOptions: SegmentedFilterOption<FleetAutonomyLevel>[] = AUTONOMY_LEVELS.map((key) => ({
    key,
    label: AUTONOMY_LEVEL_LABELS[key],
    tip: t_i18n(AUTONOMY_LEVEL_HINTS[key]),
  }));

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-lg" data-testid="fleet-bulk-dialog">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{summary}</DialogDescription>
        </DialogHeader>
        {intent.kind === "set_autonomy" && (
          <div className="space-y-1.5">
            <SegmentedFilter
              options={levelOptions}
              value={level}
              onChange={setLevel}
              ariaLabel={t_i18n("Human approval")}
              testId="fleet-bulk-levels"
              className="w-fit"
            />
            <p className="content-compact text-default-secondary">{t_i18n(AUTONOMY_LEVEL_HINTS[level])}</p>
          </div>
        )}
        {preview.error && (
          <p role="alert" className="content-compact text-default-primary">
            {fleetRefusalMessage(preview.error, t_i18n) ?? t_i18n("Try again in a moment.")}
          </p>
        )}
        {refused.length > 0 && (
          <div className="space-y-1" data-testid="fleet-bulk-refused">
            <p className="content-compact font-medium text-default-primary">
              {t_i18n("{count, plural, one {# fleet is skipped:} other {# fleets are skipped:}}", { count: refused.length })}
            </p>
            <ul className="max-h-40 space-y-0.5 overflow-y-auto content-compact text-default-secondary">
              {refused.map((fleet) => (
                <li key={fleet.fleet_id}>
                  {t_i18n("{fleet}: {reason}", {
                    fleet: fleet.name || fleet.fleet_id,
                    reason: t_i18n(bulkSkipReason(fleet.detail)),
                  })}
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="secondary" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            variant={intent.kind === "pause_every_fleet" ? "destructive" : "default"}
            disabled={!preview.data || count === 0 || bulk.isPending}
            onClick={() => void apply()}
          >
            {confirmLabel}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
