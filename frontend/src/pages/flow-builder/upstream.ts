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
 * Which nodes run before a given node.
 *
 * `{{node_id.output}}` may only reference an *upstream* node — the server
 * enforces it on save, and the builder's autocomplete offers nothing else, so a
 * reference that would be rejected is never suggested in the first place.
 *
 * Cycle-safe: `util.loop` makes cycles legitimate, so the walk is bounded by a
 * visited set rather than an acyclicity assumption.
 */

import type { FlowNodeKind } from "@/types";
import type { CanvasEdge, CanvasNode } from "./graph-adapter";
import { shapePaths, type Shape, type ShapeType } from "./output-shapes.ts";

/** Ids of every node that can reach `nodeId` by following edges backwards. */
function upstreamNodeIds(
  nodeId: string,
  edges: CanvasEdge[],
): Set<string> {
  const incoming = new Map<string, string[]>();
  for (const edge of edges) {
    const list = incoming.get(edge.target);
    if (list) list.push(edge.source);
    else incoming.set(edge.target, [edge.source]);
  }

  const seen = new Set<string>();
  const stack = [...(incoming.get(nodeId) ?? [])];
  while (stack.length) {
    const current = stack.pop()!;
    if (seen.has(current)) continue;
    seen.add(current);
    stack.push(...(incoming.get(current) ?? []));
  }
  return seen;
}

export interface VariableSuggestion {
  /** What gets inserted, e.g. `step_1.output`. */
  ref: string;
  nodeLabel: string;
  outputLabel: string;
  description: string;
  /** What the value is, when the catalog, a declaration or a run says. */
  type?: ShapeType;
  /** Where the path comes from: a declared structure, the last run, or both. */
  declared?: boolean;
  observed?: boolean;
  /** A value the last run produced at this path. */
  sample?: string | number | boolean | null;
}

/**
 * Every `{{...}}` reference legal inside `nodeId`, in graph order.
 *
 * `shapes` (node id → output name → shape, see `output-shapes.ts`) adds the
 * paths below each output: `step.fields.severity`, not only `step.fields`.
 * A text output gets no path: it has no fields to read.
 */
export function variableSuggestions(
  nodeId: string,
  nodes: CanvasNode[],
  edges: CanvasEdge[],
  kindsById: Map<string, FlowNodeKind>,
  shapes?: Map<string, Record<string, Shape>>,
): VariableSuggestion[] {
  const upstream = upstreamNodeIds(nodeId, edges);
  const suggestions: VariableSuggestion[] = [];

  for (const node of nodes) {
    // Constants are pre-evaluated, not executed: a Value node is
    // referenceable from anywhere, wired or not (the UE literal pin).
    const isPrimitive = node.data.kind === "util.primitive";
    if (node.id === nodeId) continue;
    if (!isPrimitive && !upstream.has(node.id)) continue;
    const kindDef = kindsById.get(node.data.kind);
    if (!kindDef) continue;
    const nodeLabel = node.data.label || kindDef.label;
    const nodeShapes = shapes?.get(node.id) ?? {};
    for (const output of kindDef.output_variables) {
      const catalogType = output.type && output.type !== "any" ? output.type : undefined;
      const shape = nodeShapes[output.name];
      suggestions.push({
        ref: `${node.id}.${output.name}`,
        nodeLabel,
        outputLabel: output.label,
        description: output.description,
        type: catalogType ?? (shape && shape.type !== "null" ? shape.type : undefined),
        declared: shape?.declared,
        observed: shape?.observed,
        sample: shape?.sample,
      });
      if (!shape || catalogType === "text") continue;
      for (const { path, shape: child } of shapePaths(shape)) {
        suggestions.push({
          ref: `${node.id}.${output.name}.${path}`,
          nodeLabel,
          outputLabel: `${output.label} › ${path}`,
          description: child.description ?? "",
          type: child.type === "null" ? undefined : child.type,
          declared: child.declared,
          observed: child.observed,
          sample: child.sample,
        });
      }
    }
  }

  return suggestions;
}
