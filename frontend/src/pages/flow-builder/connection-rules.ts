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
 * Client-side mirror of the server's port compatibility matrix.
 *
 * This exists ONLY to give drag-time feedback: React Flow needs a synchronous
 * answer to "may this connection be drawn?" while the user is still holding the
 * mouse down, and a round-trip cannot provide one. The server
 * (`app/services/flow_graph.py`) is what actually decides — a graph that slips
 * past this file is still rejected on save.
 *
 * The rules are derived from the catalog's own port declarations rather than
 * restated, so adding a node kind server-side needs no change here.
 */

import type { FlowEdgeKind, FlowNodeKind, FlowPort, FlowPortType } from "@/types";

/** Which edge kind a shared port type authorises. Mirrors `EDGE_KIND_BY_PORT_TYPE`. */
const EDGE_KIND_BY_PORT_TYPE: Partial<Record<FlowPortType, FlowEdgeKind>> = {
  flow: "data",
  branch: "condition_branch",
  error: "error",
  handover: "handover",
  chat: "data",
  param: "value",
  capability_tool: "capability",
  capability_integration: "capability",
  capability_skill: "capability",
  capability_knowledge_base: "capability",
  capability_output: "capability",
  capability_memory: "capability",
  capability_guardrails: "capability",
  capability_llm: "capability",
  capability_shared_state: "capability",
  capability_work_items: "capability",
  capability_workset: "capability",
  capability_budget: "capability",
  capability_workspace: "capability",
};

export function findPort(
  kind: FlowNodeKind | undefined,
  portId: string | null | undefined,
  side: "input" | "output",
): FlowPort | undefined {
  if (!kind || !portId) return undefined;
  const ports = side === "output" ? kind.outputs : kind.inputs;
  const port = ports.find((p) => p.id === portId);
  if (port) return port;
  // A Switch (or a Classify node) grows one output socket per configured case
  // (`case:<name>`).
  // Those resolve to the declared `case` port — same channel types, so the
  // compatibility matrix applies unchanged. Mirrors the server's get_port.
  // Declared-parameter pins (trigger.manual) are minted from config; they
  // all share the `param` channel. Mirrors the server's get_port.
  if (
    side === "output" &&
    portId.startsWith("param:") &&
    kind.kind === "trigger.manual"
  ) {
    return {
      id: portId,
      label: portId.slice("param:".length),
      types: ["param"],
      multiple: true,
    };
  }
  if (side === "output" && portId.startsWith("case:")) {
    const casePort = ports.find((p) => p.id === "case");
    if (casePort) {
      return { ...casePort, id: portId, label: portId.slice("case:".length) };
    }
  }
  return undefined;
}

/**
 * Edge kinds a connection between two ports may carry.
 * Empty = incompatible, the connection must be refused.
 */
function allowedEdgeKinds(
  source: FlowPort | undefined,
  target: FlowPort | undefined,
): FlowEdgeKind[] {
  if (!source || !target) return [];
  const shared = source.types.filter((t) => target.types.includes(t));
  return [
    ...new Set(
      shared
        .map((t) => EDGE_KIND_BY_PORT_TYPE[t])
        .filter((k): k is FlowEdgeKind => !!k),
    ),
  ];
}

/** The kind a new connection gets by default: the first legal one. */
export function defaultEdgeKind(
  source: FlowPort | undefined,
  target: FlowPort | undefined,
): FlowEdgeKind | null {
  return allowedEdgeKinds(source, target)[0] ?? null;
}

interface ConnectionCheck {
  ok: boolean;
  /** Why the connection was refused — shown as a tooltip on the canvas. */
  reason?: string;
}

/**
 * Full drag-time check, including the "this input takes one connection" rule
 * that port types alone cannot express.
 */
export function checkConnection(params: {
  sourceKind: FlowNodeKind | undefined;
  sourcePortId: string | null | undefined;
  targetKind: FlowNodeKind | undefined;
  targetPortId: string | null | undefined;
  sourceNodeId: string;
  targetNodeId: string;
  /** Existing edges, so port occupancy can be counted. */
  existing: { target: string; target_handle: string }[];
}): ConnectionCheck {
  const {
    sourceKind,
    sourcePortId,
    targetKind,
    targetPortId,
    sourceNodeId,
    targetNodeId,
    existing,
  } = params;

  if (sourceNodeId === targetNodeId) {
    return { ok: false, reason: "A node cannot connect to itself." };
  }

  const source = findPort(sourceKind, sourcePortId, "output");
  const target = findPort(targetKind, targetPortId, "input");
  if (!source) {
    return { ok: false, reason: `${sourceKind?.label ?? "This node"} has no such output.` };
  }
  if (!target) {
    return {
      ok: false,
      reason: `${targetKind?.label ?? "This node"} does not accept incoming connections.`,
    };
  }

  if (allowedEdgeKinds(source, target).length === 0) {
    return {
      ok: false,
      reason: `${sourceKind?.label}'s ${source.label} cannot feed ${targetKind?.label}'s ${target.label} — these ports carry different kinds of data.`,
    };
  }

  if (!target.multiple) {
    const taken = existing.some(
      (e) => e.target === targetNodeId && e.target_handle === target.id,
    );
    if (taken) {
      return {
        ok: false,
        reason: `${targetKind?.label}'s ${target.label} accepts a single connection.`,
      };
    }
  }

  return { ok: true };
}
