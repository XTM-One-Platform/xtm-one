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
 * Where the fleet-workspace chip leads, and the back state the page it
 * opens gets, so the round trip returns where it started.
 */
import { workspacePath } from "./task-routes.ts";

export type FleetWorkspaceChipTarget =
  | { kind: "fleet"; id: string; name: string; archived?: boolean }
  | { kind: "workspace"; id: string; name: string };

export function fleetWorkspaceChipLink(
  target: FleetWorkspaceChipTarget,
  from: { path: string; label: string },
): { to: string; state: { backTo: string; backLabel: string } } {
  return {
    to: target.kind === "fleet" ? `/fleets/${target.id}` : workspacePath(target.id),
    state: { backTo: from.path, backLabel: from.label },
  };
}
