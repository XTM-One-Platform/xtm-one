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
 * The fleet a workspace hosts, on the workspace card and page.
 *
 * A fleet is a team of agents; a workspace is where a team works. One fleet
 * reads "[Boxes] Hosts fleet <name>" through the chip the fleet page uses the
 * other way ("Works in workspace", `FleetWorkspaceChip`). Several (a state
 * the model no longer creates, kept as a fallback) read "Hosts N fleets"
 * followed by their names. A fleet the reader cannot see is counted, never
 * named ("and 1 fleet you cannot see"): it still blocks the delete.
 */
import { workspacePath } from "@/lib/task-routes";
import { Boxes } from "lucide-react";

import { FleetWorkspaceChip } from "@/components/workspaces/FleetWorkspaceChip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { hostedFleetCount } from "@/lib/workspace-hosted-fleets";
import type { Workspace } from "@/types";

export function WorkspaceHostedFleets({
  workspace,
  className,
}: {
  workspace: Workspace;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const fleets = workspace.hosted_fleets ?? [];
  const total = hostedFleetCount(workspace);
  if (total === 0) return null;
  const hidden = total - fleets.length;
  const from = { path: workspacePath(workspace.id), label: "Back to the workspace" };

  if (total === 1 && fleets.length === 1) {
    const fleet = fleets[0];
    return (
      <div data-testid="workspace-hosted-fleets" className={cn("flex min-w-0", className)}>
        <FleetWorkspaceChip
          target={{ kind: "fleet", id: fleet.id, name: fleet.name, archived: fleet.is_archived }}
          from={from}
        />
      </div>
    );
  }

  return (
    <div
      data-testid="workspace-hosted-fleets"
      className={cn("relative z-10 flex flex-wrap items-center gap-1.5 content-caption text-default-secondary", className)}
    >
      <Boxes className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />
      <span>{total === 1 ? t_i18n("Hosts fleet") : t_i18n("Hosts {n} fleets", { n: total })}</span>
      {fleets.map((fleet) => (
        <FleetWorkspaceChip
          key={fleet.id}
          target={{ kind: "fleet", id: fleet.id, name: fleet.name, archived: fleet.is_archived }}
          from={from}
          prefix=""
        />
      ))}
      {hidden > 0 && (
        <span>
          {hidden === 1
            ? t_i18n("and 1 fleet you cannot see")
            : t_i18n("and {n} fleets you cannot see", { n: hidden })}
        </span>
      )}
    </div>
  );
}
