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
 * FlowGraph ⇄ React Flow translation.
 *
 * The platform persists an explicit canvas document (`FlowGraph`) that carries
 * only what a flow *is*. React Flow's own node objects additionally carry
 * transient UI state — `selected`, `dragging`, `measured`, `positionAbsolute` —
 * which must never reach the database: it makes two identical flows serialise
 * differently and turns every drag into a "changed" diff.
 *
 * So the canvas works on React Flow objects and this module is the only place
 * the two shapes meet.
 */

import type { Edge, Node } from "@xyflow/react";
import type { FlowEdge, FlowGraph, FlowNode } from "@/types";

/** Data carried on a React Flow node. Kept flat so node components stay simple. */
interface CanvasNodeData extends Record<string, unknown> {
  kind: string;
  label?: string;
  config: Record<string, unknown>;
  sourcePayload?: Record<string, unknown>;
}

interface CanvasEdgeData extends Record<string, unknown> {
  kind: FlowEdge["kind"];
  /** How the edge is drawn (persisted as `path_style`). Absent = bezier. */
  pathStyle?: FlowEdge["path_style"];
  /** Display-only, injected by FlowCanvas: this edge is inside a loop's body
   *  and draws an orange under-stroke. Never persisted (fromCanvas reads
   *  only `kind`). */
  inLoopBranch?: boolean;
  /** Value edge from an object-typed constant — drawn UE-green. */
  objectTyped?: boolean;
  /** Display-only: the execution front just crossed this wire (pulse). */
  live?: boolean;
}

export type CanvasNode = Node<CanvasNodeData>;
export type CanvasEdge = Edge<CanvasEdgeData>;

export const EMPTY_GRAPH: FlowGraph = {
  nodes: [],
  edges: [],
  viewport: { x: 0, y: 0, zoom: 1 },
};

/** Every node renders through the same component; the kind drives its content. */
const NODE_COMPONENT_TYPE = "flowNode";
const EDGE_COMPONENT_TYPE = "flowEdge";

export function toCanvasNodes(graph: FlowGraph): CanvasNode[] {
  const nodes = graph.nodes.map((node): CanvasNode => ({
    id: node.id,
    type: NODE_COMPONENT_TYPE,
    position: node.position,
    // Children draw ON TOP of their group container.
    ...(node.kind === "util.group" ? { zIndex: -1 } : {}),
    ...(node.parent_id ? { parentId: node.parent_id } : {}),
    data: {
      kind: node.kind,
      label: node.label,
      config: node.config ?? {},
      sourcePayload: node.source_payload,
    },
  }));
  // React Flow requires a parent to appear BEFORE its children.
  return nodes.sort((a, b) =>
    a.data.kind === "util.group" === (b.data.kind === "util.group")
      ? 0
      : a.data.kind === "util.group"
        ? -1
        : 1,
  );
}

export function toCanvasEdges(graph: FlowGraph): CanvasEdge[] {
  return graph.edges.map((edge) => ({
    id: edge.id,
    type: EDGE_COMPONENT_TYPE,
    source: edge.source,
    sourceHandle: edge.source_handle,
    target: edge.target,
    targetHandle: edge.target_handle,
    label: edge.label,
    data: { kind: edge.kind, pathStyle: edge.path_style },
  }));
}

export function fromCanvas(
  nodes: CanvasNode[],
  edges: CanvasEdge[],
  viewport: FlowGraph["viewport"],
): FlowGraph {
  return {
    nodes: nodes.map((node): FlowNode => {
      const data = node.data;
      const persisted: FlowNode = {
        id: node.id,
        kind: data.kind,
        // Round to whole pixels: React Flow produces long floats while dragging,
        // which would make every micro-move look like a graph change.
        position: {
          x: Math.round(node.position.x),
          y: Math.round(node.position.y),
        },
        config: data.config ?? {},
      };
      if (data.label) persisted.label = data.label;
      if (data.sourcePayload) persisted.source_payload = data.sourcePayload;
      if (node.parentId) persisted.parent_id = node.parentId;
      return persisted;
    }),
    edges: edges.map((edge): FlowEdge => {
      const persisted: FlowEdge = {
        id: edge.id,
        source: edge.source,
        source_handle: edge.sourceHandle ?? "out",
        target: edge.target,
        target_handle: edge.targetHandle ?? "in",
        kind: edge.data?.kind ?? "data",
      };
      if (typeof edge.label === "string" && edge.label) {
        persisted.label = edge.label;
      }
      if (edge.data?.pathStyle) {
        persisted.path_style = edge.data.pathStyle;
      }
      return persisted;
    }),
    viewport: {
      x: Math.round(viewport.x),
      y: Math.round(viewport.y),
      // Two decimals is finer than any zoom step the UI offers, and it stops a
      // pinch-zoom jitter from registering as an unsaved change.
      zoom: Math.round(viewport.zoom * 100) / 100,
    },
  };
}

/**
 * Stable comparison used for the "unsaved changes" flag.
 *
 * Key order is normalised, so a graph rebuilt from the canvas compares equal to
 * the one the server sent back even though the objects were built differently.
 */
export function graphsEqual(a: FlowGraph, b: FlowGraph): boolean {
  return stableStringify(a) === stableStringify(b);
}

/**
 * Equality of what the flow DOES — node kinds/configs/edges — ignoring where
 * things sit on the canvas (positions, viewport). Panning, zooming or tidying
 * the layout must never read as "unsaved changes" that gate running: those are
 * auto-saved silently instead.
 */
export function graphsSemanticallyEqual(a: FlowGraph, b: FlowGraph): boolean {
  const strip = (graph: FlowGraph) => ({
    nodes: graph.nodes.map(({ position: _position, ...rest }) => {
      // Scratch/test content is not flow definition: typing a test message
      // in the Chat trigger's embedded input must not gate Run behind a
      // save — it auto-saves silently like a node move (mirrors the
      // server's _semantic_shape).
      const scratch = TEST_ONLY_CONFIG_KEYS[rest.kind];
      if (!rest.config) return rest;
      const config = Object.fromEntries(
        Object.entries(rest.config).filter(
          ([k]) => !scratch?.has(k) && !PRESENTATION_CONFIG_KEYS.has(k),
        ),
      );
      return { ...rest, config };
    }),
    edges: graph.edges.map(({ path_style: _pathStyle, ...rest }) => rest),
  });
  return stableStringify(strip(a)) === stableStringify(strip(b));
}

/** Per-kind config keys that are test scratch, not flow definition. */
const TEST_ONLY_CONFIG_KEYS: Record<string, Set<string>> = {
  "trigger.chat": new Set(["default_message"]),
};

/** Presentation-only keys on every kind — a utility's flowchart shape
 *  changes how the node draws, never what it does. */
const PRESENTATION_CONFIG_KEYS = new Set(["shape", "breakpoint"]);

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) {
    return `[${value.map(stableStringify).join(",")}]`;
  }
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== undefined)
    .sort(([a], [b]) => a.localeCompare(b));
  return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${stableStringify(v)}`).join(",")}}`;
}

/**
 * A node id the server will accept: `[A-Za-z0-9_-]{1,64}`, no dots.
 *
 * Dots are excluded because `{{node_id.output}}` splits on the first one — an
 * id containing a dot would make every reference to it ambiguous.
 */
export function nextNodeId(kind: string, taken: Set<string>): string {
  const base = kind.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "node";
  for (let i = 1; ; i += 1) {
    const candidate = `${base}_${i}`.slice(0, 64);
    if (!taken.has(candidate)) return candidate;
  }
}

export function nextEdgeId(taken: Set<string>): string {
  for (let i = 1; ; i += 1) {
    const candidate = `e${i}`;
    if (!taken.has(candidate)) return candidate;
  }
}

export { NODE_COMPONENT_TYPE, EDGE_COMPONENT_TYPE };
