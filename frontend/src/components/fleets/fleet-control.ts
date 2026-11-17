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
 * The fleet header's control (#9769 S2): what the status pill says and which
 * one-click actions the header offers, computed from the fleet health's
 * `automations` alone (`GET /fleets/{id}/health`). Nothing here calls the API.
 *
 * - Not set up, Partially on, Paused by a person: "Enable fleet" ("Enable N
 *   more" when some are on), which opens the setup dialog (run-as,
 *   prerequisites, run estimate, Confirm; #9769 S4b, UX #9835 N1). Opening the
 *   page or the dialog never enables anything.
 * - Partially on and On: "Pause all".
 * - Paused by the fleet's Pause all: "Resume", which switches back on exactly
 *   the automations Pause all switched off. Paused otherwise (a person, a
 *   quarantine): the pill alone, Resume having nothing to restore.
 */
import type { FleetAutomations } from "@/hooks/api/useFleetApi";
import { fleetOptIn, type FleetOptIn } from "@/lib/fleet-activity";

export interface FleetControlState {
  optIn: FleetOptIn;
  /** "Resume" is offered, as the one filled action at the right end of the header. */
  canResume: boolean;
  /** "Pause all" is offered: at least one automation is on. */
  canPause: boolean;
  /** The automations Resume switches back on. */
  resumable: number;
  /** "Enable fleet" is offered: some automation is off and Resume would not restore it. */
  canEnable: boolean;
  /** The automations off, what Enable fleet switches on. */
  enableable: number;
}

/** `null` while the health is not read (loading, refused): the header shows no control then. */
export function fleetControlState(automations: FleetAutomations | null | undefined): FleetControlState | null {
  const optIn = fleetOptIn(automations);
  if (!automations || !optIn) return null;
  const resumable = automations.fleet_paused ?? 0;
  const enableable = Math.max(0, automations.total - automations.enabled);
  return {
    optIn,
    canResume: resumable > 0,
    canPause: automations.enabled > 0,
    resumable,
    // After the fleet's Pause all, Resume is the one action: Enable would switch on more than it paused.
    canEnable: enableable > 0 && resumable === 0,
    enableable,
  };
}

/**
 * Who may switch the fleet's automations, as the backend decides it
 * (`_controllable_fleet`): whoever manages the fleet; on a built-in fleet an
 * administrator only. An archived fleet is read as it was: no control.
 */
export function canControlFleet(
  fleet: { can_manage: boolean; is_builtin?: boolean; is_archived: boolean },
  isAdmin: boolean,
): boolean {
  if (fleet.is_archived || !fleet.can_manage) return false;
  return !fleet.is_builtin || isAdmin;
}
