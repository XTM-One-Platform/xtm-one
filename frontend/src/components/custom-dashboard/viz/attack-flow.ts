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
 * Where each step of an attack flow sits: the `graph` shape laid out from left
 * to right, a step one column after the latest step leading to it (its
 * longest path from a start), the steps of a column stacked and centred.
 * A cycle (a flow that loops back) is cut where it closes: the step keeps the
 * column it had.
 */
import type { WidgetGraphData } from "@/types";

export type FlowKind = "start" | "action" | "condition" | "operator" | "asset";
export type FlowNode = WidgetGraphData["nodes"][number];

/** A card's size, and the gaps between columns and between cards of a column. */
export const FLOW = { width: 220, height: 92, operator: 56, column: 280, row: 112 } as const;

/** A node's kind: as the data says, else an action (a plain graph's node is a step). */
function flowKind(node: FlowNode): FlowKind {
  return node.kind ?? "action";
}

export interface PlacedFlowNode {
  node: FlowNode;
  kind: FlowKind;
  x: number;
  y: number;
}

/** Each node's place; `columns` is how many columns the flow takes. */
export function layoutFlow(graph: WidgetGraphData): { placed: PlacedFlowNode[]; columns: number; width: number; height: number } {
  const ids = graph.nodes.map((n) => n.id);
  const known = new Set(ids);
  const next = new Map<string, string[]>(ids.map((id) => [id, []]));
  const incoming = new Map<string, number>(ids.map((id) => [id, 0]));
  for (const edge of graph.edges) {
    if (!known.has(edge.source) || !known.has(edge.target) || edge.source === edge.target) continue;
    next.get(edge.source)!.push(edge.target);
    incoming.set(edge.target, (incoming.get(edge.target) ?? 0) + 1);
  }
  // Kahn's order, the longest path giving the column; what a cycle leaves unvisited starts after the rest.
  const column = new Map<string, number>();
  const queue = ids.filter((id) => incoming.get(id) === 0);
  for (const id of queue) column.set(id, 0);
  const left = new Map(incoming);
  for (let i = 0; i < queue.length; i++) {
    const id = queue[i];
    for (const target of next.get(id) ?? []) {
      column.set(target, Math.max(column.get(target) ?? 0, (column.get(id) ?? 0) + 1));
      left.set(target, (left.get(target) ?? 1) - 1);
      if (left.get(target) === 0) queue.push(target);
    }
  }
  const last = Math.max(0, ...column.values());
  for (const id of ids) if (!column.has(id)) column.set(id, last + 1);

  const byColumn = new Map<number, FlowNode[]>();
  for (const node of graph.nodes) {
    const c = column.get(node.id) ?? 0;
    byColumn.set(c, [...(byColumn.get(c) ?? []), node]);
  }
  const tallest = Math.max(1, ...[...byColumn.values()].map((nodes) => nodes.length));
  const placed: PlacedFlowNode[] = [];
  for (const [c, nodes] of byColumn) {
    const offset = ((tallest - nodes.length) * FLOW.row) / 2;
    nodes.forEach((node, index) => {
      const kind = flowKind(node);
      // An operator is a small circle: centred on the row a card would take.
      const shift = kind === "operator" ? (FLOW.height - FLOW.operator) / 2 : 0;
      placed.push({ node, kind, x: c * FLOW.column + (kind === "operator" ? (FLOW.width - FLOW.operator) / 2 : 0), y: offset + index * FLOW.row + shift });
    });
  }
  const columns = Math.max(0, ...column.values()) + 1;
  return { placed, columns: byColumn.size, width: (columns - 1) * FLOW.column + FLOW.width, height: (tallest - 1) * FLOW.row + FLOW.height };
}

/** The zoom under which a card's text is too small to read: a larger flow opens at it, from its start. */
export const READABLE_ZOOM = 0.6;

/**
 * Where the flow opens in a box of *width* x *height*: whole when it fits at a readable zoom, else at
 * {@link READABLE_ZOOM} from its start (its left), centred on its rows - the reader pans along it.
 */
export function openingViewport(content: { width: number; height: number }, box: { width: number; height: number }, pad = 16) {
  const fit = Math.min((box.width - 2 * pad) / content.width, (box.height - 2 * pad) / content.height, 1);
  const zoom = Math.max(fit, READABLE_ZOOM);
  const x = fit >= READABLE_ZOOM ? (box.width - content.width * zoom) / 2 : pad;
  const y = (box.height - content.height * zoom) / 2;
  return { x, y, zoom };
}
