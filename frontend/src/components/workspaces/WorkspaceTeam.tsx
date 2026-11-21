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
 * A workspace's Team tab, only when it hosts a fleet: the fleet's
 * members panel (`FleetMembersPanel`, the fleet page's own: roles, health,
 * edition for who may edit) and a link to the fleet's graph. No second graph:
 * the graph lives on the fleet page.
 */
import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import { FleetMembersPanel } from "@/components/fleets/FleetMembersPanel";
import { Button } from "@/components/ui/button";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useFleet, useFleetHealth, useFleetMembers } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";
import { workspacePath } from "@/lib/task-routes";

/** Where an agent or the fleet opened from the Team tab goes back to. */
function teamBackState(workspaceId: string) {
  return { backTo: workspacePath(workspaceId, "team"), backLabel: "Back to the workspace" };
}

export function WorkspaceTeam({ workspaceId, fleetId }: { workspaceId: string; fleetId: string }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { data: fleet, isLoading } = useFleet(fleetId);
  const { data: members } = useFleetMembers(fleet ? fleetId : null);
  const { data: health } = useFleetHealth(fleet ? fleetId : null);
  // Every agent an admin may see, so a member outside the reader's own library still names.
  const { data: agents } = useAgents({ scope: "all" });
  const openAgent = useCallback(
    (id: string) => navigate(`/agents/${id}`, { state: teamBackState(workspaceId) }),
    [navigate, workspaceId],
  );

  if (isLoading) return <PageSkeleton cards={1} />;
  if (!fleet) return null;

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="gap-1.5"
          onClick={() => navigate(`/fleets/${fleet.id}`, { state: teamBackState(workspaceId) })}
        >
          <Icon name="network" size={16} />
          {t_i18n("Open the fleet graph")}
        </Button>
      </div>
      <FleetMembersPanel
        fleet={fleet}
        members={members ?? []}
        health={health?.members ?? []}
        agents={agents ?? []}
        onOpenAgent={openAgent}
      />
    </div>
  );
}
