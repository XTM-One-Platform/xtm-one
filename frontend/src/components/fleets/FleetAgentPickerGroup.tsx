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
 * The Fleet + Agent picker pair of the Operations lists.
 *
 * One group: when a toolbar wraps, the two pickers move to the next row
 * together instead of splitting. Every list that offers both pickers renders
 * this component (Executions, Work Items) rather than copying the wrapper, so
 * the order (fleet first, then agent), the gap and the no-shrink rule stay the
 * same everywhere.
 *
 * `showFleet` is false inside a list already locked on a fleet or a workspace;
 * `FleetFilter` itself renders nothing for a reader with no fleet. The agent
 * picker is the caller's (its options and wording differ per list); with
 * neither picker the group renders nothing.
 */
import type { ReactNode } from "react";

import { FleetFilter } from "@/components/fleets/FleetFilter";
import { cn } from "@/lib/utils";

export interface FleetAgentPickerGroupProps {
  /** False in a list locked on a fleet or a workspace: no fleet picker. */
  showFleet: boolean;
  fleet: string | undefined;
  onFleetChange: (next: string | undefined) => void;
  /** The list's agent picker, or null when the list has none to offer. */
  agentPicker?: ReactNode;
  /** Kept on the wrapper so a list's tests and lab probes can find it. */
  testId?: string;
  className?: string;
  /** Width classes of the fleet picker, matched to the caller's agent picker. */
  fleetClassName?: string;
}

export function FleetAgentPickerGroup({
  showFleet,
  fleet,
  onFleetChange,
  agentPicker,
  testId = "fleet-agent-picker-group",
  className,
  fleetClassName,
}: FleetAgentPickerGroupProps) {
  if (!showFleet && !agentPicker) return null;
  return (
    <div className={cn("flex shrink-0 items-center gap-3", className)} data-testid={testId}>
      {showFleet && <FleetFilter value={fleet} onChange={onFleetChange} className={fleetClassName} />}
      {agentPicker}
    </div>
  );
}
