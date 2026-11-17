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
 * What the fleet About mini graph draws (#9757, UX rounds 3 and 4, B1).
 *
 * The mini graph is a compact map of the fleet, at most about 320 px high. A
 * dense fleet drawn agent by agent needs more than one viewport of graph and
 * wires its nodes with long crossing edges. The mini graph folds, in order,
 * until it draws at most `MINI_GRAPH_MAX_NODES` nodes:
 * 1. every source fleet that lends two agents or more becomes ONE labelled
 *    group node ("3 agents from AI security"), built by the map's own Fleets
 *    layer (`collapseFleets`), so the handovers are rewired the same way;
 * 2. past the budget, every remaining agent of another fleet folds into one
 *    "N agents from other fleets" group (a dense fleet often borrows one agent
 *    from each of several fleets, so step 1 alone folds nothing);
 * 3. still past the budget, the fleet's own leaf members (no handover of
 *    their own) fold under their parent, two or more per parent, into one
 *    "N more agents" node.
 * A group node is a view, never an agent: it opens the Graph view unfocused,
 * and the Graph view keeps every agent.
 */

import type { Agent } from "@/types";
import { collapseFleets, remapHandovers } from "@/lib/fleet-layer";

/**
 * The most nodes the mini graph draws before it folds more: the 9-node CTEM
 * posture fleet stays whole (about 300 px), a 19-node fleet folds to fit.
 */
export const MINI_GRAPH_MAX_NODES = 10;

/** The id of the group of every remaining agent of another fleet. */
const OTHER_FLEETS_ID = "group:other-fleets";

/** The id of the group of a parent's leaf members. */
function leavesGroupId(parentId: string): string {
  return `group:more:${parentId}`;
}

export interface FleetMiniGraphLabels {
  /** "3 agents from AI security": the agents one other fleet lends. */
  fromFleet: (count: number, fleetName: string) => string;
  /** "5 agents from other fleets": every other agent of another fleet. */
  fromOtherFleets: (count: number) => string;
  /** "3 more agents": a parent's leaf members. */
  more: (count: number) => string;
}

export interface FleetMiniGraph {
  /** The agents drawn: the members, the lone ghosts and one node per group. */
  agents: Agent[];
  /** The ghosts drawn, with their home group line (empty for a group node). */
  ghostNodes: Map<string, string>;
  /** The fleet graph's edges, rewired to the drawn ids. */
  edgeIds: Set<string>;
  /** The dotted crossing edges, rewired to the drawn ids. */
  dashedEdgeIds: Set<string>;
  /** The group nodes: a view, never an agent. */
  groupIds: Set<string>;
}

function rewire(ids: ReadonlySet<string>, nodeOf: (id: string) => string): Set<string> {
  const out = new Set<string>();
  for (const id of ids) {
    const [source, target] = id.split("->");
    const from = nodeOf(source);
    const to = nodeOf(target);
    if (from !== to) out.add(`${from}->${to}`);
  }
  return out;
}

/**
 * The drawn agents with some of them folded into group nodes: `foldOf` maps a
 * folded agent to its group id, `nameOf` names each group. Handovers are
 * rewired to the drawn ids, a group carrying its members' handovers.
 */
function fold(
  agents: readonly Agent[],
  foldOf: ReadonlyMap<string, string>,
  nameOf: (groupId: string, count: number) => string,
  keepFleet: boolean,
): { agents: Agent[]; nodeOf: (id: string) => string } {
  const nodeOf = (id: string) => foldOf.get(id) ?? id;
  const counts = new Map<string, number>();
  for (const groupId of foldOf.values()) counts.set(groupId, (counts.get(groupId) ?? 0) + 1);
  const drawn: Agent[] = [];
  const groups = new Map<string, Agent>();
  for (const agent of agents) {
    const groupId = foldOf.get(agent.id);
    const self = groupId ?? agent.id;
    const rewired = remapHandovers(agent.handover_agent_ids, agent.handover_types, agent.handover_conditions, nodeOf, self);
    if (!groupId) {
      drawn.push({ ...agent, handover_agent_ids: rewired.ids, handover_types: rewired.types, handover_conditions: rewired.conditions });
      continue;
    }
    const group = groups.get(groupId);
    if (group) {
      const ids = (group.handover_agent_ids ??= []);
      const types = (group.handover_types ??= {});
      const conditions = (group.handover_conditions ??= {});
      for (const id of rewired.ids) {
        if (ids.includes(id)) continue;
        ids.push(id);
        if (rewired.types[id]) types[id] = rewired.types[id];
        if (rewired.conditions[id]) conditions[id] = rewired.conditions[id];
      }
      continue;
    }
    const node: Agent = {
      ...agent,
      id: groupId,
      name: nameOf(groupId, counts.get(groupId) ?? 1),
      icon: "Boxes",
      is_company_managed: false,
      // A group of the fleet's own members stays in the fleet; a group of
      // other fleets' agents has no home fleet: its name says it all.
      fleet: keepFleet && agent.fleet ? { ...agent.fleet, is_orchestrator: false } : null,
      handover_agent_ids: [...rewired.ids],
      handover_types: { ...rewired.types },
      handover_conditions: { ...rewired.conditions },
    };
    groups.set(groupId, node);
    drawn.push(node);
  }
  return { agents: drawn, nodeOf };
}

/** The fleet's own orchestrator among the drawn agents, or null. */
function orchestratorOf(agents: readonly Agent[]): string | null {
  return agents.find((a) => a.fleet?.is_orchestrator && !a.id.startsWith("group:"))?.id ?? null;
}

/**
 * The handovers to and from the group of other fleets, aggregated at the
 * fleet's orchestrator: one edge from it to the group and, when the group
 * hands back, one edge from the group to it (#9760 UX round 5, B2). Drawn
 * member by member, the group pulled every edge of the fleet through the
 * middle of the drawing (68 crossing pairs, edges across names); the Graph
 * view keeps every handover. A fleet with no orchestrator keeps its edges.
 */
function aggregateAtOrchestrator(agents: Agent[], groupId: string): Agent[] {
  const hubId = orchestratorOf(agents);
  if (!hubId) return agents;
  let typeIn: string | undefined;
  let toGroup = false;
  for (const agent of agents) {
    if (agent.id === groupId || !(agent.handover_agent_ids ?? []).includes(groupId)) continue;
    toGroup = true;
    typeIn ??= agent.handover_types?.[groupId];
  }
  const group = agents.find((a) => a.id === groupId);
  const fromGroup = (group?.handover_agent_ids ?? []).length > 0;
  return agents.map((agent) => {
    if (agent.id === groupId) {
      const typeOut = Object.values(agent.handover_types ?? {})[0];
      return {
        ...agent,
        handover_agent_ids: fromGroup ? [hubId] : [],
        handover_types: fromGroup && typeOut ? { [hubId]: typeOut } : {},
        handover_conditions: {},
      };
    }
    const ids = (agent.handover_agent_ids ?? []).filter((id) => id !== groupId);
    const types = { ...(agent.handover_types ?? {}) };
    const conditions = { ...(agent.handover_conditions ?? {}) };
    delete types[groupId];
    delete conditions[groupId];
    if (agent.id === hubId && toGroup) {
      ids.push(groupId);
      if (typeIn) types[groupId] = typeIn;
    }
    return { ...agent, handover_agent_ids: ids, handover_types: types, handover_conditions: conditions };
  });
}

/**
 * The drawn nodes no handover reaches or leaves (`edgeIds` are "from->to"),
 * in the order drawn: the members a force layout leaves apart from the
 * others, which the mini graph's key explains (#10147 UX r3 B2: no node
 * floats unexplained).
 */
export function edgelessIds(agents: readonly Agent[], edgeIds: ReadonlySet<string>): string[] {
  const linked = new Set<string>();
  for (const id of edgeIds) {
    const [from, to] = id.split("->");
    if (from === to) continue;
    linked.add(from);
    linked.add(to);
  }
  return agents.filter((a) => !linked.has(a.id)).map((a) => a.id);
}

/** The mini graph of a fleet, folded to at most `maxNodes` nodes where it can. */
export function fleetMiniGraph(
  agents: readonly Agent[],
  ghostNodes: ReadonlyMap<string, string>,
  edgeIds: ReadonlySet<string>,
  dashedEdgeIds: ReadonlySet<string>,
  labels: FleetMiniGraphLabels,
  maxNodes: number = MINI_GRAPH_MAX_NODES,
): FleetMiniGraph {
  // Step 1: the agents one other fleet lends, two or more, as one group.
  const lent = new Map<string, number>();
  for (const agent of agents) {
    if (ghostNodes.has(agent.id) && agent.fleet) lent.set(agent.fleet.fleet_id, (lent.get(agent.fleet.fleet_id) ?? 0) + 1);
  }
  const layer = collapseFleets(agents, {
    // Only another fleet's agents fold, and only two or more of one fleet.
    keepIds: new Set(agents.filter((a) => !ghostNodes.has(a.id)).map((a) => a.id)),
    canOpenFleet: (fleetId) => (lent.get(fleetId) ?? 0) >= 2,
  });
  const groupIds = new Set(layer.fleetNodes.keys());
  let drawn: Agent[] = layer.agents.map((agent) => {
    const info = layer.fleetNodes.get(agent.id);
    // A group node has no home fleet of its own: its name says it all.
    return info ? { ...agent, name: labels.fromFleet(info.memberIds.length, info.name), fleet: null } : agent;
  });
  const steps: Array<(id: string) => string> = [layer.nodeOf];
  const nodeOf = (id: string) => steps.reduce((cur, step) => step(cur), id);
  const isGhost = (id: string) => groupIds.has(id) || (ghostNodes.has(id) && nodeOf(id) === id);

  // Step 2: past the budget, every agent of another fleet in one group.
  const outside = drawn.filter((a) => isGhost(a.id));
  if (drawn.length > maxNodes && outside.length >= 2) {
    const count = outside.reduce((n, a) => n + (layer.fleetNodes.get(a.id)?.memberIds.length ?? 1), 0);
    const foldOf = new Map(outside.map((a) => [a.id, OTHER_FLEETS_ID]));
    const folded = fold(drawn, foldOf, () => labels.fromOtherFleets(count), false);
    for (const id of foldOf.keys()) groupIds.delete(id);
    groupIds.add(OTHER_FLEETS_ID);
    drawn = aggregateAtOrchestrator(folded.agents, OTHER_FLEETS_ID);
    steps.push(folded.nodeOf);
  }

  // Step 3: still past the budget, the leaf members of each parent, two or more.
  if (drawn.length > maxNodes) {
    const parents = new Map<string, string[]>();
    const outgoing = new Set<string>();
    for (const agent of drawn) {
      for (const target of agent.handover_agent_ids ?? []) {
        if (agent.id !== target) outgoing.add(agent.id);
        parents.set(target, [...(parents.get(target) ?? []), agent.id]);
      }
    }
    const leavesOf = new Map<string, string[]>();
    for (const agent of drawn) {
      const from = parents.get(agent.id) ?? [];
      // A leaf: a member of this fleet with one parent and no handover of its own.
      if (groupIds.has(agent.id) || isGhost(agent.id) || outgoing.has(agent.id) || from.length !== 1) continue;
      if (agent.fleet?.is_orchestrator) continue;
      leavesOf.set(from[0], [...(leavesOf.get(from[0]) ?? []), agent.id]);
    }
    const foldOf = new Map<string, string>();
    for (const [parentId, leaves] of leavesOf) {
      if (leaves.length < 2) continue;
      for (const id of leaves) foldOf.set(id, leavesGroupId(parentId));
    }
    if (foldOf.size > 0) {
      const folded = fold(drawn, foldOf, (_groupId, count) => labels.more(count), true);
      for (const groupId of foldOf.values()) groupIds.add(groupId);
      drawn = folded.agents;
      steps.push(folded.nodeOf);
    }
  }

  // The edges to and from the group of other fleets meet at the orchestrator,
  // as the agents' handovers above do (#9760 UX round 5, B2).
  const hubId = drawn.some((a) => a.id === OTHER_FLEETS_ID) ? orchestratorOf(drawn) : null;
  const edgeOf = (id: string) => {
    const [from, to] = id.split("->");
    if (!hubId) return id;
    if (to === OTHER_FLEETS_ID && from !== hubId) return `${hubId}->${to}`;
    if (from === OTHER_FLEETS_ID && to !== hubId) return `${from}->${hubId}`;
    return id;
  };
  const aggregate = (ids: Set<string>) => new Set([...ids].map(edgeOf));

  const ghosts = new Map<string, string>();
  for (const [id, group] of ghostNodes) if (nodeOf(id) === id) ghosts.set(id, group);
  // A group of other fleets' agents is drawn as a ghost with no group line; a
  // group of the fleet's own members is drawn as a member.
  for (const id of groupIds) if (!id.startsWith("group:more:")) ghosts.set(id, "");
  return {
    agents: drawn,
    ghostNodes: ghosts,
    edgeIds: aggregate(rewire(edgeIds, nodeOf)),
    dashedEdgeIds: aggregate(rewire(dashedEdgeIds, nodeOf)),
    groupIds,
  };
}
