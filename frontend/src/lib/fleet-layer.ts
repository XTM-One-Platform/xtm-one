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
 * The Fleets layer of the agent-to-agent map: the members of
 * a fleet drawn as one collapsed fleet node.
 *
 * The map's graph draws agents and the handovers between them. With the layer
 * on, every agent with a home fleet (`Agent.fleet`) is replaced by one
 * stand-in per fleet: an agent-shaped node whose id is `fleet:<fleet id>`,
 * named after the fleet. Handovers are rewired to what is drawn: a handover to
 * a member ends on its fleet node, handovers between members of one fleet are
 * left out (the fleet page draws them), and handovers from one fleet to
 * another become one edge between the two fleet nodes.
 *
 * A fleet node is a view, never a handover endpoint: handovers stay agent to
 * agent, so the graph refuses to start, end or edit a handover on it and opens
 * the fleet page instead.
 */

import type { Agent } from "@/types";
import { ALL_HANDOVER_TYPES, isLegacyHandoverMap } from "@/lib/handover-types";

const FLEET_NODE_PREFIX = "fleet:";

/** The id the map draws for a fleet. */
export function fleetNodeId(fleetId: string): string {
  return `${FLEET_NODE_PREFIX}${fleetId}`;
}

/** The fleet a drawn node stands for, or `null` for an agent's own node. */
export function fleetIdOfNode(nodeId: string): string | null {
  return nodeId.startsWith(FLEET_NODE_PREFIX) ? nodeId.slice(FLEET_NODE_PREFIX.length) : null;
}

/** The fields of an agent the layer reads and rewrites. */
export type FleetLayerAgent = Pick<
  Agent,
  "id" | "name" | "icon" | "enabled" | "is_company_managed" | "handover_agent_ids" | "handover_types" | "handover_conditions" | "fleet"
>;

interface FleetNodeInfo {
  fleetId: string;
  name: string;
  /** The members the caller can see, in the order of the agent list. */
  memberIds: string[];
}

export interface FleetLayer<T> {
  /** What the graph draws: the playground agents and one stand-in per fleet. */
  agents: T[];
  /** The fleet nodes, by drawn id. */
  fleetNodes: Map<string, FleetNodeInfo>;
  /** The id drawn for an agent: its fleet node, or its own id. */
  nodeOf: (agentId: string) => string;
}

/**
 * Handovers rewired to the drawn ids: targets mapped through `nodeOf`, the
 * source's own node left out, each drawn target kept once with the type and
 * condition of its first handover.
 */
export function remapHandovers(
  ids: readonly string[] | null | undefined,
  types: Record<string, string> | null | undefined,
  conditions: Record<string, string> | null | undefined,
  nodeOf: (agentId: string) => string,
  selfNodeId: string | null,
): { ids: string[]; types: Record<string, string>; conditions: Record<string, string> } {
  const out: string[] = [];
  const outTypes: Record<string, string> = {};
  const outConditions: Record<string, string> = {};
  for (const target of ids ?? []) {
    const drawn = nodeOf(target);
    if (drawn === selfNodeId || out.includes(drawn)) continue;
    out.push(drawn);
    if (types?.[target]) outTypes[drawn] = types[target];
    if (conditions?.[target]) outConditions[drawn] = conditions[target];
  }
  return { ids: out, types: outTypes, conditions: outConditions };
}

export interface CollapseOptions {
  /** Whether the caller can open a fleet: a fleet it cannot read keeps its
   *  visible members as agent nodes (no node that opens on a refusal). */
  canOpenFleet?: (fleetId: string) => boolean;
  /** Agents always drawn as themselves, such as the General Assistant's
   *  stand-in, which the graph must find by its own id. */
  keepIds?: ReadonlySet<string>;
}

/** The agents as the map draws them with the Fleets layer on. */
export function collapseFleets<T extends FleetLayerAgent>(
  agents: readonly T[],
  options: CollapseOptions = {},
): FleetLayer<T> {
  const { canOpenFleet = () => true, keepIds } = options;
  const homeNode = new Map<string, string>();
  const fleetNodes = new Map<string, FleetNodeInfo>();
  const membersOf = new Map<string, T[]>();
  for (const agent of agents) {
    if (!agent.fleet || keepIds?.has(agent.id) || !canOpenFleet(agent.fleet.fleet_id)) continue;
    const nodeId = fleetNodeId(agent.fleet.fleet_id);
    homeNode.set(agent.id, nodeId);
    const info = fleetNodes.get(nodeId);
    if (info) info.memberIds.push(agent.id);
    else fleetNodes.set(nodeId, { fleetId: agent.fleet.fleet_id, name: agent.fleet.fleet_name, memberIds: [agent.id] });
    membersOf.set(nodeId, [...(membersOf.get(nodeId) ?? []), agent]);
  }
  const nodeOf = (agentId: string) => homeNode.get(agentId) ?? agentId;

  const drawn: T[] = [];
  const placed = new Set<string>();
  for (const agent of agents) {
    const nodeId = homeNode.get(agent.id);
    if (!nodeId) {
      const rewired = remapHandovers(agent.handover_agent_ids, agent.handover_types, agent.handover_conditions, nodeOf, agent.id);
      drawn.push({
        ...agent,
        handover_agent_ids: rewired.ids,
        handover_types: rewired.types,
        handover_conditions: rewired.conditions,
      });
      continue;
    }
    if (placed.has(nodeId)) continue;
    placed.add(nodeId);
    const members = membersOf.get(nodeId) ?? [agent];
    // The orchestrator stands for the fleet's other fields when it is visible.
    const anchor = members.find((m) => m.fleet?.is_orchestrator) ?? members[0];
    const ids: string[] = [];
    const types: Record<string, string> = {};
    const conditions: Record<string, string> = {};
    for (const member of members) {
      const rewired = remapHandovers(member.handover_agent_ids, member.handover_types, member.handover_conditions, nodeOf, nodeId);
      // A member in the legacy mode runs every type on each of its targets:
      // say so per target, so another member's typed targets do not turn it
      // into Delegate on the merged map.
      const legacy = isLegacyHandoverMap(member.handover_types);
      for (const id of rewired.ids) {
        if (ids.includes(id)) continue;
        ids.push(id);
        if (rewired.types[id]) types[id] = rewired.types[id];
        else if (legacy) types[id] = ALL_HANDOVER_TYPES;
        if (rewired.conditions[id]) conditions[id] = rewired.conditions[id];
      }
    }
    // Every target running every type is the plain legacy map.
    const allLegacy = ids.length > 0 && ids.every((id) => types[id] === ALL_HANDOVER_TYPES);
    drawn.push({
      ...anchor,
      id: nodeId,
      name: fleetNodes.get(nodeId)?.name ?? anchor.name,
      icon: "Boxes",
      enabled: members.some((m) => m.enabled),
      is_company_managed: false,
      handover_agent_ids: ids,
      handover_types: allLegacy ? {} : types,
      handover_conditions: conditions,
    });
  }
  return { agents: drawn, fleetNodes, nodeOf };
}

/** A set of agent ids (runs in flight, a focus) as drawn ids. */
export function drawnIds(ids: Iterable<string>, nodeOf: (agentId: string) => string): Set<string> {
  const out = new Set<string>();
  for (const id of ids) out.add(nodeOf(id));
  return out;
}
