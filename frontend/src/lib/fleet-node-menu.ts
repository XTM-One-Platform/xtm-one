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
 * The node menu of the fleet graph: which
 * actions a right click on a node offers and the
 * members a handover from it may target. Pure, so the rules are tested apart
 * from the page; every write still goes through `/api/v1/fleets` or
 * `/api/v1/agents`, and the API owns every rule.
 */
import type { FleetMember } from "@/hooks/api/useFleetApi";

export type FleetNodeAction = "details" | "open" | "focus" | "clear_focus" | "add_handover" | "change_parent" | "remove";

export interface FleetNodeMenuInput {
  /** The home member drawn by the node; absent for a ghost (connected or external agent). */
  member: FleetMember | undefined;
  /** The reader edits the fleet's members (`fleetEditRights(fleet).members.canEdit`: manager, not archived). */
  canEditMembers: boolean;
  /** The reader edits this agent's handovers (`useHandoverEdgeEditing().editableAgentIds`). */
  canEditHandovers: boolean;
  /** The current focus is this node's. */
  focused: boolean;
  /** Home members a new handover from this node may still target. */
  handoverTargetCount: number;
}

/**
 * The menu, in display order: first the agent definition drawer of the
 * Agentic Flow map and the agent's page. A ghost, a reader who does not manage the
 * fleet, or an archived fleet gets the reading actions only; adding a
 * handover also needs the right to edit the agent and a member left to
 * target.
 */
export function fleetNodeActions(input: FleetNodeMenuInput): FleetNodeAction[] {
  const actions: FleetNodeAction[] = ["details", "open", input.focused ? "clear_focus" : "focus"];
  if (!input.member || !input.canEditMembers) return actions;
  if (input.canEditHandovers && input.handoverTargetCount > 0) actions.push("add_handover");
  actions.push("change_parent", "remove");
  return actions;
}

/** The home member row of an agent, when the agent is a home member of the fleet. */
export function homeMemberOf(members: readonly FleetMember[], agentId: string): FleetMember | undefined {
  return members.find((m) => m.role === "member" && m.agent_id === agentId);
}

/**
 * The home members a new handover from `sourceId` may target: never itself,
 * never one it already hands over to; by name.
 */
export function handoverTargetCandidates<A extends { id: string; name: string }>(
  sourceId: string,
  existingTargetIds: readonly string[] | null | undefined,
  homeAgents: readonly A[],
): A[] {
  const taken = new Set(existingTargetIds ?? []);
  return homeAgents
    .filter((a) => a.id !== sourceId && !taken.has(a.id))
    .sort((a, b) => a.name.localeCompare(b.name));
}
