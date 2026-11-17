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
 * One-click horizontal DAG layout for the canvas.
 *
 * Uses `dagre`, already a dependency (`agent-flow.tsx` lays out the handover
 * graph with it), so no new layout library enters the bundle.
 *
 * Notes are laid out separately: they are free-floating annotations with no
 * edges, so dagre would stack them all in a single rank at the far left. They
 * are parked in a column beside the graph instead.
 */

import dagre from "dagre";
import type { CanvasEdge, CanvasNode } from "./graph-adapter";

// Node box used for spacing. The real DOM node is measured by React Flow, but
// dagre needs a size up front and these match the rendered card closely enough
// that the result does not need a second pass.
const NODE_WIDTH = 260;
const NODE_HEIGHT = 96;
const RANK_SEP = 120;
const NODE_SEP = 48;

const NOTE_COLUMN_X = -360;
const NOTE_ROW_SEP = 140;

export function autoLayout(
  nodes: CanvasNode[],
  edges: CanvasEdge[],
  isAnnotation: (kind: string) => boolean,
): CanvasNode[] {
  const graphNodes = nodes.filter((n) => !isAnnotation(n.data.kind));

  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", ranksep: RANK_SEP, nodesep: NODE_SEP });
  g.setDefaultEdgeLabel(() => ({}));

  for (const node of graphNodes) {
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  const laidOutIds = new Set(graphNodes.map((n) => n.id));
  for (const edge of edges) {
    // An edge touching a node dagre does not know about would throw.
    if (laidOutIds.has(edge.source) && laidOutIds.has(edge.target)) {
      g.setEdge(edge.source, edge.target);
    }
  }

  dagre.layout(g);

  const positioned = new Map(
    graphNodes.map((node) => {
      const { x, y } = g.node(node.id);
      // dagre reports centres; React Flow positions by the top-left corner.
      return [
        node.id,
        { x: Math.round(x - NODE_WIDTH / 2), y: Math.round(y - NODE_HEIGHT / 2) },
      ] as const;
    }),
  );

  let noteRow = 0;
  return nodes.map((node) => {
    const position = positioned.get(node.id);
    if (position) return { ...node, position };
    // A note: park it in its own column so it stays visible but out of the way.
    const parked = { x: NOTE_COLUMN_X, y: noteRow * NOTE_ROW_SEP };
    noteRow += 1;
    return { ...node, position: parked };
  });
}
