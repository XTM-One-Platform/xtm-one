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
 * The fleet page header's one-click control (#9769 S2, design round 4
 * APPROVE): the status pill goes into the header's badges, the actions into
 * its actions row, secondary first and the one filled primary at the right
 * end (design-system "Page-title actions").
 *
 * - "Set up" on a fleet nobody switched on ("Enable N more" when some are on,
 *   "Enable fleet" when a person paused them) opens the one fleet setup
 *   (`FleetSetupDialog`, #1151: the wizard, or the CTEM loop's guided setup).
 *   Opening the page or the setup never enables anything.
 * - "Pause all" switches off every automation that is on, in one call
 *   (`POST /fleets/{id}/pause`); "Resume" switches back on exactly those
 *   (`POST /fleets/{id}/resume`). A toast says how many changed.
 * - A reader who may not switch the automations (not a manager, a built-in
 *   fleet for a non-administrator, an archived fleet) sees the pill only.
 */
import { useState } from "react";
import { Pause, Play, Power } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useFleetControl, type FleetAutomations } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { fleetRefusalMessage } from "./fleet-refusals";
import { FleetSetupDialog } from "./FleetSetupDialog";
import { FleetOptInBadge } from "./FleetOptInBadge";
import { fleetControlState } from "./fleet-control";

/** The pill for the header's badges row; nothing while the health is not read. */
export function FleetHeaderStatus({ automations }: { automations: FleetAutomations | null | undefined }) {
  const state = fleetControlState(automations);
  if (!state) return null;
  return <FleetOptInBadge optIn={state.optIn} />;
}

/** The actions for the header's actions row, after the page's own secondary ones. */
export function FleetHeaderActions({
  fleetId,
  fleetName,
  fleetSlug,
  automations,
  canControl,
}: {
  fleetId: string;
  /** Named in the setup's title. */
  fleetName: string;
  /** The CTEM loop fleet opens its own guided setup (#1087). */
  fleetSlug?: string | null;
  automations: FleetAutomations | null | undefined;
  /** `canControlFleet`: the backend refuses the others with a 403. */
  canControl: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const pause = useFleetControl(fleetId, "pause");
  const resume = useFleetControl(fleetId, "resume");
  const [enableOpen, setEnableOpen] = useState(false);
  const state = fleetControlState(automations);
  if (!state || !canControl) return null;
  const busy = pause.isPending || resume.isPending;

  const run = async (action: "pause" | "resume") => {
    try {
      const result = await (action === "pause" ? pause : resume).mutateAsync();
      toast({
        title:
          action === "pause"
            ? t_i18n("{count, plural, =0 {No automation was on} one {# automation paused} other {# automations paused}}", { count: result.changed })
            : t_i18n("{count, plural, =0 {No automation to resume} one {# automation resumed} other {# automations resumed}}", { count: result.changed }),
        description: action === "pause" ? t_i18n("Runs already started finish. Resume switches back on the same automations.") : undefined,
        variant: "success",
      });
    } catch (err) {
      toast({
        title: action === "pause" ? t_i18n("The fleet's automations were not paused") : t_i18n("The fleet's automations were not resumed"),
        description: fleetRefusalMessage(err, t_i18n),
        variant: "error",
      });
    }
  };

  return (
    <>
      {state.canPause && (
        <Button type="button" variant="outline" className="gap-1.5" disabled={busy} onClick={() => run("pause")} data-testid="fleet-pause-all">
          <Pause className="h-4 w-4" aria-hidden />
          {t_i18n("Pause all")}
        </Button>
      )}
      {state.canResume && (
        <Button type="button" className="gap-1.5" disabled={busy} onClick={() => run("resume")} data-testid="fleet-resume">
          <Play className="h-4 w-4" aria-hidden />
          {t_i18n("Resume")}
        </Button>
      )}
      {state.canEnable && (
        <Button type="button" className="gap-1.5" disabled={busy} onClick={() => setEnableOpen(true)} data-testid="fleet-enable">
          <Power className="h-4 w-4" aria-hidden />
          {state.optIn === "partial"
            ? t_i18n("Enable {count} more", { count: state.enableable })
            : state.optIn === "not_set_up"
              ? t_i18n("Set up")
              : t_i18n("Enable fleet")}
        </Button>
      )}
      <FleetSetupDialog fleetId={fleetId} fleetName={fleetName} fleetSlug={fleetSlug} open={enableOpen} onOpenChange={setEnableOpen} />
    </>
  );
}
