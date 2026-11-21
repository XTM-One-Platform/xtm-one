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
 * From the fleet graph read (`GET /fleets/{id}/graph`) to what
 * the fleet page draws with the agent-to-agent map's graph (phase 3a).
 *
 * The fleet page reuses `AgentFlowGraph`: it draws agents and the handovers
 * between them. The fleet graph names which agents belong on that drawing:
 * the home members, then the connected agents and the external agents (the
 * far end of a cross-fleet handover) the caller can see. The last two are
 * ghost nodes: drawn apart, labelled with their home fleet, view only.
 * A crossing handover no connected row covers is "ungoverned" and drawn
 * dashed; one whose target is a member its fleet does not
 * expose (`allowed: false`) is drawn dashed with its own warning. Only the
 * edges the fleet graph returns are drawn (`fleetEdgeIds`): a handover between
 * two ghosts is outside the fleet's boundary. Connected fleets are left to the
 * Fleets layer.
 */

import { workItemsPath } from "./task-routes.ts";
import type { FleetGraph, FleetMemberHealth, FleetMemberHealthStatus } from "@/hooks/api/useFleetApi";

/**
 * The agent ids the fleet page draws: home members first, then connected
 * agents, then external agents. A node the caller cannot read is left out.
 */
export function fleetAgentIds(graph: FleetGraph | null | undefined): string[] {
  if (!graph) return [];
  const members: string[] = [];
  const connected: string[] = [];
  const external: string[] = [];
  const seen = new Set<string>();
  const linked = edgedAgentIds(graph);
  for (const node of graph.nodes) {
    if (!node.agent_id || !node.visible || seen.has(node.agent_id)) continue;
    if (node.kind === "member") members.push(node.agent_id);
    // A ghost is drawn for the handover that brings it in: one with no edge
    // would float alone on the drawing, unexplained.
    else if (node.kind === "connected" && linked.has(node.agent_id)) connected.push(node.agent_id);
    else if (node.kind === "external" && linked.has(node.agent_id)) external.push(node.agent_id);
    else continue;
    seen.add(node.agent_id);
  }
  return [...members, ...connected, ...external];
}

/**
 * The ghost nodes of the fleet page: every connected or external agent the
 * caller can read and a fleet graph edge reaches, mapped to the name of its
 * home fleet (empty for an agent in no fleet, the playground). A home member is
 * never a ghost; a ghost no edge reaches is not drawn (it would float alone).
 */
export function fleetGhostNodes(graph: FleetGraph | null | undefined): Map<string, string> {
  const ghosts = new Map<string, string>();
  if (!graph) return ghosts;
  const members = new Set(graph.nodes.filter((n) => n.kind === "member" && n.agent_id).map((n) => n.agent_id));
  const linked = edgedAgentIds(graph);
  for (const node of graph.nodes) {
    if (!node.agent_id || !node.visible || members.has(node.agent_id) || !linked.has(node.agent_id)) continue;
    if (node.kind !== "connected" && node.kind !== "external") continue;
    if (!ghosts.has(node.agent_id)) ghosts.set(node.agent_id, node.fleet_name);
  }
  return ghosts;
}

/** The agents at either end of a fleet graph edge between two agents. */
function edgedAgentIds(graph: FleetGraph): Set<string> {
  const ids = new Set<string>();
  for (const edge of graph.edges) {
    if (edge.source === edge.target) continue;
    ids.add(edge.source);
    ids.add(edge.target);
  }
  return ids;
}

/**
 * The handovers drawn dashed: a crossing edge (outbound or inbound) that no
 * connected row governs, as `"source->target"`, the id the graph gives an edge.
 */
export function ungovernedEdgeIds(graph: FleetGraph | null | undefined): Set<string> {
  const ids = new Set<string>();
  if (!graph) return ids;
  for (const edge of graph.edges) {
    if (edge.scope !== "internal" && !edge.governed && edge.source !== edge.target) {
      ids.add(`${edge.source}->${edge.target}`);
    }
  }
  return ids;
}

/**
 * Every edge of the fleet graph between two agents, as `"source->target"`:
 * the only handovers the fleet page draws.
 */
export function fleetEdgeIds(graph: FleetGraph | null | undefined): Set<string> {
  const ids = new Set<string>();
  if (!graph) return ids;
  for (const edge of graph.edges) {
    if (edge.source !== edge.target) ids.add(`${edge.source}->${edge.target}`);
  }
  return ids;
}

export type CrossingWarning = "not_exposed" | "ungoverned";

/**
 * The crossing edges drawn dashed, with the reason: a target its fleet does
 * not expose (`allowed: false`) first, whether a connected row governs it or
 * not; then an edge no connected row governs.
 */
export function crossingEdgeWarnings(graph: FleetGraph | null | undefined): Map<string, CrossingWarning> {
  const warnings = new Map<string, CrossingWarning>();
  if (!graph) return warnings;
  for (const edge of graph.edges) {
    if (edge.scope === "internal" || edge.source === edge.target) continue;
    const id = `${edge.source}->${edge.target}`;
    if (!edge.allowed) warnings.set(id, "not_exposed");
    else if (!edge.governed && !warnings.has(id)) warnings.set(id, "ungoverned");
  }
  return warnings;
}

/** The caller's agents restricted to the fleet, in the fleet's order. */
export function fleetScopedAgents<T extends { id: string }>(
  agents: readonly T[] | null | undefined,
  graph: FleetGraph | null | undefined,
): T[] {
  const byId = new Map((agents ?? []).map((a) => [a.id, a]));
  return fleetAgentIds(graph)
    .map((id) => byId.get(id))
    .filter((a): a is T => a !== undefined);
}

/** Handovers between two agents of the fleet, counted once per source and target. */
export function internalHandoverCount(graph: FleetGraph | null | undefined): number {
  if (!graph) return 0;
  const pairs = new Set<string>();
  for (const edge of graph.edges) {
    if (edge.scope === "internal" && edge.source !== edge.target) pairs.add(`${edge.source}->${edge.target}`);
  }
  return pairs.size;
}

/** The order the members panel lists health in: what needs a look first. */
const HEALTH_ORDER: Record<FleetMemberHealthStatus, number> = {
  failing: 0,
  stale: 1,
  disabled: 2,
  idle: 3,
  ok: 4,
};

export function compareHealth(
  a: { status: FleetMemberHealthStatus; name: string },
  b: { status: FleetMemberHealthStatus; name: string },
): number {
  return HEALTH_ORDER[a.status] - HEALTH_ORDER[b.status] || a.name.localeCompare(b.name);
}

/** The English lines of the health badge's tooltip, read by `t_i18n(key, values)`. @i18n-keys */
export const HEALTH_REASON_KEYS = {
  failing: "Its latest run failed ({failed} of {total, plural, one {# recent run} other {# recent runs}})",
  recovered: "{failed} of {total, plural, one {# recent run} other {# recent runs}} failed, then a run succeeded",
  clean: "{total, plural, one {No failure in its last run} other {No failure in its last # runs}}",
  lastInterrupted: "Interrupted by the platform",
  interruptions: "{count, plural, one {# run interrupted by the platform, not counted} other {# runs interrupted by the platform, not counted}}",
  disabled: "The agent is disabled",
  stale: "{count, plural, one {# stale work item} other {# stale work items}}",
  idle: "No run yet",
} as const;

/** The English labels of the health badge, read by `t_i18n(key, values)`. @i18n-keys */
export const HEALTH_BADGE_KEYS = {
  failing: "Failed run",
  stale: "{count, plural, one {# stale item} other {# stale items}}",
  disabled: "Disabled",
  idle: "Idle",
  ok: "OK",
} as const;

/**
 * What a member's badge reads: the cause and, for stale items, their count.
 * Never a bare "Stale": the reader sees how many items and opens them
 * through {@link staleWorkItemsHref}.
 */
export function healthBadge(h: Pick<FleetMemberHealth, "status" | "stale_work_items">): {
  key: (typeof HEALTH_BADGE_KEYS)[keyof typeof HEALTH_BADGE_KEYS];
  values?: Record<string, number>;
} {
  if (h.status === "stale") return { key: HEALTH_BADGE_KEYS.stale, values: { count: h.stale_work_items } };
  return { key: HEALTH_BADGE_KEYS[h.status] };
}

/** Operations > Work Items, filtered on the stale items this agent owns. */
export function staleWorkItemsHref(agentId: string): string {
  return workItemsPath({ view: "stale", agent: agentId });
}

/** One line of the health badge's tooltip: an i18n key and its values. */
export interface HealthReason {
  key: (typeof HEALTH_REASON_KEYS)[keyof typeof HEALTH_REASON_KEYS];
  values?: Record<string, number>;
}

/**
 * Why a member's badge reads what it reads: the server's verdict
 * (`run_outcome.member_verdict`) put in words, never recomputed here. Empty
 * for an OK member whose only runs are still in flight: the badge then shows
 * no tooltip at all, never an empty one.
 */
export function healthReasons(h: Pick<FleetMemberHealth,
  "status" | "last_run_outcome" | "failures_24h" | "recent_runs" | "recent_interruptions" | "stale_work_items">,
): HealthReason[] {
  const reasons: HealthReason[] = [];
  const failed = h.failures_24h ?? 0;
  const judged = h.recent_runs ?? 0;
  const interrupted = h.recent_interruptions ?? 0;
  const K = HEALTH_REASON_KEYS;
  if (h.status === "failing") {
    reasons.push({ key: K.failing, values: { failed, total: judged } });
  } else if (judged > 0) {
    reasons.push(failed > 0
      ? { key: K.recovered, values: { failed, total: judged } }
      : { key: K.clean, values: { total: judged } });
  }
  if (h.last_run_outcome === "interrupted") reasons.push({ key: K.lastInterrupted });
  if (interrupted > 0) reasons.push({ key: K.interruptions, values: { count: interrupted } });
  if (h.status === "disabled") reasons.push({ key: K.disabled });
  if (h.status === "stale") reasons.push({ key: K.stale, values: { count: h.stale_work_items } });
  if (h.status === "idle") reasons.push({ key: K.idle });
  return reasons;
}
