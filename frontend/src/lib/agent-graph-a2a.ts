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
 * The A2A liaisons on the agent graphs (#12337).
 *
 * `GET /api/v1/a2a/liaisons` serves partner nodes and directed edges for the
 * agents the caller sees (`app.a2a.liaisons`). The Agentic Flow map and the
 * fleet graph draw them beside the agents they already draw: a partner node is
 * a view-only stand-in (a ghost with a sub-label), its edges are typed
 * `a2a_inbound` (partner -> agent, "tasks") or `a2a_outbound` (agent ->
 * partner agent, "delegates"), drawn in their own ink and dash, and never
 * edited from the graph: a liaison is an integration binding, changed on the
 * agent's Tools tab or the integration page.
 */

import type { Agent } from "@/types";

export type A2ADirection = "inbound" | "outbound";

export interface A2APartnerNode {
  id: string;
  name: string;
  integration_id: string;
  integration_name: string;
  provider: string;
  direction: A2ADirection;
  card_urls: string[];
}

interface A2ALiaisonEdge {
  source_id: string;
  target_id: string;
  direction: A2ADirection;
  integration_id: string;
}

export interface A2ALiaisons {
  partners: A2APartnerNode[];
  edges: A2ALiaisonEdge[];
}

/** The edge type of a liaison, beside the handover types. */
const A2A_EDGE_TYPES: Record<A2ADirection, string> = {
  inbound: "a2a_inbound",
  outbound: "a2a_outbound",
};

/**
 * How a liaison edge is drawn: an ink no handover type uses, and a dash-dot
 * pattern of its own, so it is told from a handover by its pattern and not
 * by colour alone. The label names the direction ("tasks", "delegates").
 */
const A2A_INK = {
  color: "text-default-primary",
  edgeColor: "#f472b6", // pink 400
  lightEdgeColor: "#9d174d", // pink 800
  dash: "8 3 2 3" as string | null,
};

export const A2A_EDGE_CONFIG: Record<string, { label: string; description: string; color: string; edgeColor: string; lightEdgeColor: string; dash: string | null }> = {
  a2a_inbound: {
    ...A2A_INK,
    label: "Tasks",
    description: "A partner outside the platform submits work to this agent through an A2A integration.",
  },
  a2a_outbound: {
    ...A2A_INK,
    label: "Delegates",
    description: "This agent delegates work to a partner agent through an A2A integration.",
  },
};

/** The drawing config of a liaison edge type, or null for a handover type. */
export function a2aEdgeConfig(type: string): (typeof A2A_EDGE_CONFIG)[string] | null {
  return A2A_EDGE_CONFIG[type] ?? null;
}

/**
 * The sub-label a partner node carries under its name, its direction first:
 * a narrow room cuts the end of the line, never the direction (#12347 UX r1 B2).
 */
export function a2aPartnerSubLabel(direction: A2ADirection): string {
  return direction === "inbound" ? "Inbound A2A partner" : "Outbound A2A partner";
}

/**
 * What a partner node's tooltip adds to its name: its integration and its
 * card URLs, never a credential. Empty for a partner with nothing to add.
 */
export function a2aPartnerDetail(partner: A2APartnerNode): string {
  const parts: string[] = [];
  if (partner.integration_name && partner.integration_name !== partner.name) parts.push(partner.integration_name);
  parts.push(...partner.card_urls);
  return parts.join(" · ");
}

export interface A2AGraphLayer<T> {
  /** The agents with each kept partner as a stand-in and the outbound edges added. */
  agents: T[];
  /** The partner nodes kept, by drawn id. */
  partners: Map<string, A2APartnerNode>;
  /** The type of each liaison edge kept, by `"source->target"`. */
  edgeTypes: Map<string, string>;
}

/**
 * The drawn agents with the liaisons of the agents among them.
 *
 * `nodeOf` maps an agent id to the id it is drawn as (a collapsed fleet node
 * on the map); a liaison whose agent is not drawn is left out, and so is a
 * partner left with no edge. One partner bound to several drawn agents keeps
 * one node and an edge to each. The input is never mutated.
 */
export function withA2ALiaisons<T extends Agent>(
  agents: readonly T[],
  liaisons: A2ALiaisons | null | undefined,
  nodeOf: (agentId: string) => string = (id) => id,
): A2AGraphLayer<T> {
  const partners = new Map<string, A2APartnerNode>();
  const edgeTypes = new Map<string, string>();
  if (!liaisons || liaisons.edges.length === 0) return { agents: [...agents], partners, edgeTypes };

  const drawn = new Set(agents.map((a) => a.id));
  const partnerById = new Map(liaisons.partners.map((p) => [p.id, p]));
  const inboundTargets = new Map<string, string[]>();
  const outboundTargets = new Map<string, string[]>();
  const push = (map: Map<string, string[]>, key: string, value: string) => {
    const list = map.get(key) ?? [];
    if (!list.includes(value)) list.push(value);
    map.set(key, list);
  };

  for (const edge of liaisons.edges) {
    const partnerId = edge.direction === "inbound" ? edge.source_id : edge.target_id;
    const agentId = edge.direction === "inbound" ? edge.target_id : edge.source_id;
    const partner = partnerById.get(partnerId);
    const drawnAgent = nodeOf(agentId);
    if (!partner || !drawn.has(drawnAgent) || drawn.has(partnerId)) continue;
    partners.set(partnerId, partner);
    if (edge.direction === "inbound") {
      push(inboundTargets, partnerId, drawnAgent);
      edgeTypes.set(`${partnerId}->${drawnAgent}`, A2A_EDGE_TYPES.inbound);
    } else {
      push(outboundTargets, drawnAgent, partnerId);
      edgeTypes.set(`${drawnAgent}->${partnerId}`, A2A_EDGE_TYPES.outbound);
    }
  }

  const out: T[] = agents.map((agent) => {
    const extra = outboundTargets.get(agent.id);
    if (!extra) return agent;
    const ids = [...(agent.handover_agent_ids ?? [])];
    for (const id of extra) if (!ids.includes(id)) ids.push(id);
    return { ...agent, handover_agent_ids: ids };
  });
  for (const partner of [...partners.values()].sort((a, b) => a.id.localeCompare(b.id))) {
    out.push({
      id: partner.id,
      name: partner.name,
      icon: "Handshake",
      enabled: true,
      is_company_managed: false,
      output_format: "markdown",
      handover_agent_ids: inboundTargets.get(partner.id) ?? [],
      handover_types: {},
      handover_conditions: {},
      fleet: null,
    } as unknown as T);
  }
  return { agents: out, partners, edgeTypes };
}

/** The integration page a partner node opens. */
export function a2aPartnerPath(partner: A2APartnerNode): string {
  return `/integrations/${partner.integration_id}`;
}
