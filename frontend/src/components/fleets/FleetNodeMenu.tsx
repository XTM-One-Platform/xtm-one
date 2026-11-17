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
 * The node menu of the fleet graph: a right
 * click on a node opens the agent, focuses on its handovers, and for a home
 * member of a fleet the reader manages, adds a handover from it, changes its
 * parent or removes it from the fleet. Which items show is `fleetNodeActions`;
 * every write goes through the same hooks as the members panel and the graph
 * (`useHandoverEdgeEditing`, `/api/v1/fleets`), and the API owns every rule.
 *
 * The menu is the design system's (`PointMenu`, the row menu's items), and the
 * change-parent dialog and the remove action are the members panel's own
 * (`fleet-member-actions`, `useRemoveFleetMemberAction`): one behaviour
 * wherever an action starts.
 *
 * Stays mounted while the page shows the graph: the dialogs it opens outlive
 * the menu.
 */
import { useMemo, useState } from "react";
import { ExternalLink, Focus, GitBranch, Info, Share2, Trash2, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import type { OverflowMenuItem } from "@/components/ui/overflow-menu";
import { PointMenu } from "@/components/ui/point-menu";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { Fleet, FleetMember } from "@/hooks/api/useFleetApi";
import type { HandoverEdgeEditing } from "@/hooks/useHandoverEdgeEditing";
import { useFormatter } from "@/i18n";
import { HANDOVER_TYPES, HANDOVER_TYPE_CONFIG } from "@/lib/constants";
import { fleetNodeActions, handoverTargetCandidates, homeMemberOf, type FleetNodeAction } from "@/lib/fleet-node-menu";
import type { Agent } from "@/types";

import { fleetEditRights, graphHandoversEditable } from "./fleet-edition";
import { ChangeParentDialog } from "./fleet-member-actions";
import { useRemoveFleetMemberAction } from "./useRemoveFleetMemberAction";

/** The node a right click opened the menu on, at its position in the graph card. */
export interface FleetNodeMenuTarget {
  agentId: string;
  x: number;
  y: number;
}

export function FleetNodeMenu({
  target,
  fleet,
  members,
  homeAgents,
  agents,
  handoverEditing,
  focused,
  onToggleFocus,
  onOpenAgent,
  onShowDetails,
  onClose,
}: {
  target: FleetNodeMenuTarget | null;
  fleet: Fleet;
  members: readonly FleetMember[];
  /** The fleet's home agents drawn on the graph: the targets a new handover may take. */
  homeAgents: readonly Agent[];
  /** Every agent the reader sees, for the names of ghost nodes. */
  agents: readonly Agent[];
  handoverEditing: HandoverEdgeEditing;
  focused: boolean;
  onToggleFocus: (agentId: string) => void;
  onOpenAgent: (agentId: string) => void;
  /** Opens the agent definition drawer, the node's "i". */
  onShowDetails: (agentId: string) => void;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { confirmElement, removeMember } = useRemoveFleetMemberAction(fleet.id);
  const [handoverFrom, setHandoverFrom] = useState<string | null>(null);
  const [parentMember, setParentMember] = useState<FleetMember | null>(null);
  const [handoverTarget, setHandoverTarget] = useState<string | null>(null);
  const [handoverType, setHandoverType] = useState<string>("delegate");

  const canEditMembers = fleetEditRights(fleet).members.canEdit;
  const canEditGraphHandovers = graphHandoversEditable(fleet);
  const agentById = useMemo(() => new Map(agents.map((a) => [a.id, a])), [agents]);

  const targetsFor = (agentId: string) =>
    handoverTargetCandidates(agentId, agentById.get(agentId)?.handover_agent_ids, homeAgents);

  const run = (action: FleetNodeAction, agentId: string, member: FleetMember | undefined) => {
    switch (action) {
      case "details":
        onShowDetails(agentId);
        return;
      case "open":
        onOpenAgent(agentId);
        return;
      case "focus":
      case "clear_focus":
        onToggleFocus(agentId);
        return;
      case "add_handover":
        setHandoverTarget(null);
        setHandoverType("delegate");
        setHandoverFrom(agentId);
        return;
      case "change_parent":
        if (member) setParentMember(member);
        return;
      case "remove":
        if (member) void removeMember(member);
        return;
    }
  };

  const items = useMemo<OverflowMenuItem[]>(() => {
    if (!target) return [];
    const member = homeMemberOf(members, target.agentId);
    const actions = fleetNodeActions({
      member,
      canEditMembers,
      canEditHandovers: canEditGraphHandovers && handoverEditing.editableAgentIds.has(target.agentId),
      focused,
      handoverTargetCount: targetsFor(target.agentId).length,
    });
    // The same labels, icons and destructive flag as the members panel's row menu.
    const spec: Record<FleetNodeAction, Omit<OverflowMenuItem, "id" | "onSelect">> = {
      details: { label: t_i18n("View details"), icon: Info },
      open: { label: t_i18n("Open the agent"), icon: ExternalLink },
      focus: { label: t_i18n("Focus on its handovers"), icon: Focus },
      clear_focus: { label: t_i18n("Clear the focus"), icon: X },
      add_handover: { label: t_i18n("Add a handover from it..."), icon: Share2 },
      change_parent: { label: t_i18n("Change parent..."), icon: GitBranch },
      remove: { label: t_i18n("Remove from the fleet"), icon: Trash2, destructive: true },
    };
    return actions.map((action) => ({
      ...spec[action],
      id: action,
      separatorBefore:
        action === "remove" ||
        action === "add_handover" ||
        (action === "change_parent" && !actions.includes("add_handover")),
      onSelect: () => run(action, target.agentId, member),
    }));
    // `run` and `targetsFor` read the same inputs listed here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, members, canEditMembers, canEditGraphHandovers, handoverEditing.editableAgentIds, focused, homeAgents, agentById, t_i18n]);

  const nodeName = target ? agentById.get(target.agentId)?.name ?? "" : "";
  const handoverFromName = handoverFrom ? agentById.get(handoverFrom)?.name ?? "" : "";

  const saveHandover = () => {
    if (handoverFrom && handoverTarget) void handoverEditing.onAddEdge(handoverFrom, handoverTarget, handoverType);
    setHandoverFrom(null);
  };

  return (
    <>
      <PointMenu
        at={target ? { x: target.x, y: target.y } : null}
        items={items}
        onClose={onClose}
        label={t_i18n("Actions for {name}", { name: nodeName })}
      />

      <Dialog open={!!handoverFrom} onOpenChange={(next) => !next && setHandoverFrom(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t_i18n("Add a handover")}</DialogTitle>
            <DialogDescription>
              {t_i18n("Who {name} hands over to in this fleet, and how.", { name: handoverFromName })}
            </DialogDescription>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault();
              saveHandover();
            }}
          >
            {handoverFrom && (
              <>
                <div className="space-y-1.5">
                  <Label htmlFor="fleet-handover-target" className="text-default-secondary">
                    {t_i18n("Handover target")}
                  </Label>
                  <SearchableSelect
                    id="fleet-handover-target"
                    aria-label={t_i18n("Handover target")}
                    options={targetsFor(handoverFrom).map((a) => ({ value: a.id, label: a.name }))}
                    value={handoverTarget}
                    onValueChange={(value) => setHandoverTarget(value || null)}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="fleet-handover-type" className="text-default-secondary">
                    {t_i18n("Handover type")}
                  </Label>
                  <Select value={handoverType} onValueChange={setHandoverType}>
                    <SelectTrigger id="fleet-handover-type" className="w-full" aria-label={t_i18n("Handover type")}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {HANDOVER_TYPES.map((type) => (
                        <SelectItem key={type} value={type}>
                          {t_i18n(HANDOVER_TYPE_CONFIG[type].label)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </>
            )}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={() => setHandoverFrom(null)}>
                {t_i18n("Cancel")}
              </Button>
              <Button type="submit" disabled={!handoverTarget}>
                {t_i18n("Add")}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
      <ChangeParentDialog fleetId={fleet.id} members={members} member={parentMember} onClose={() => setParentMember(null)} />
      {confirmElement}
    </>
  );
}
