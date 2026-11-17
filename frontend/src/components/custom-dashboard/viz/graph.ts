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
 * Where the nodes of a graph go: a **link graph** left to right in ranks
 * (`dagre`, already the flow builder's and the handover graph's layout; a
 * table's stages keep their columns), a **network graph** by forces
 * (`forceLayout`, `viz/force.ts`), each node as large as its degree. Both
 * are drawn on React Flow (`GraphViz.tsx`), which pans and zooms them.
 *
 * A node's colour is its group's, in the order groups first appear.
 *
 * Pure (dagre and `force.ts` are): runs on `node --test`.
 */
import dagre from "dagre";

import type { WidgetGraphData } from "@/types";

import { forceLayout, type ForceNode } from "./force.ts";

export const LINK_NODE = { width: 150, height: 34 } as const;
export const MIN_RADIUS = 6;
export const MAX_RADIUS = 18;

export interface PlacedNode {
  id: string;
  label: string;
  group: string | null;
  /** Its group's index, in the order groups first appear (`null`: no group). */
  tone: number | null;
  /** Top-left corner, as React Flow places a node. */
  x: number;
  y: number;
  w: number;
  h: number;
  degree: number;
}

export interface PlacedGraph {
  nodes: PlacedNode[];
  edges: WidgetGraphData["edges"];
  groups: string[];
}

function tones(graph: WidgetGraphData): { groups: string[]; toneOf: (group: string | null) => number | null } {
  const groups: string[] = [];
  for (const node of graph.nodes) if (node.group && !groups.includes(node.group)) groups.push(node.group);
  return { groups, toneOf: (group) => (group ? groups.indexOf(group) : null) };
}

function degrees(graph: WidgetGraphData): Map<string, number> {
  const degree = new Map(graph.nodes.map((node) => [node.id, 0]));
  for (const edge of graph.edges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  }
  return degree;
}

export function linkGraphLayout(graph: WidgetGraphData): PlacedGraph {
  const { groups, toneOf } = tones(graph);
  const degree = degrees(graph);
  const g = new dagre.graphlib.Graph({ multigraph: true });
  g.setGraph({ rankdir: "LR", ranksep: 80, nodesep: 14 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const node of graph.nodes) g.setNode(node.id, { width: LINK_NODE.width, height: LINK_NODE.height });
  graph.edges.forEach((edge, index) => {
    if (edge.source !== edge.target) g.setEdge(edge.source, edge.target, {}, String(index));
  });
  dagre.layout(g);
  const nodes = graph.nodes.map((node) => {
    const at = g.node(node.id);
    return {
      id: node.id,
      label: node.label,
      group: node.group,
      tone: toneOf(node.group),
      x: Math.round(at.x - LINK_NODE.width / 2),
      y: Math.round(at.y - LINK_NODE.height / 2),
      w: LINK_NODE.width,
      h: LINK_NODE.height,
      degree: degree.get(node.id) ?? 0,
    };
  });
  return { nodes, edges: graph.edges, groups };
}

/** A node's radius: from `MIN_RADIUS` for a lone node to `MAX_RADIUS` for the most connected. */
export function nodeRadius(degree: number, most: number): number {
  if (most <= 0) return MIN_RADIUS;
  return MIN_RADIUS + ((MAX_RADIUS - MIN_RADIUS) * Math.sqrt(degree)) / Math.sqrt(most);
}

export function networkLayout(graph: WidgetGraphData, width: number, height: number): PlacedGraph {
  const { groups, toneOf } = tones(graph);
  const degree = degrees(graph);
  const index = new Map(graph.nodes.map((node, i) => [node.id, i]));
  const points: ForceNode[] = graph.nodes.map(() => ({ x: 0, y: 0, vx: 0, vy: 0 }));
  const edges = graph.edges
    .filter((edge) => edge.source !== edge.target)
    .map((edge) => ({ s: index.get(edge.source)!, t: index.get(edge.target)! }))
    .filter((edge) => edge.s !== undefined && edge.t !== undefined);
  // A larger graph needs a larger stage: React Flow fits the view to it afterwards.
  const scale = Math.max(1, Math.sqrt(graph.nodes.length / 12));
  const w = width * scale;
  const h = height * scale;
  forceLayout(points, edges, w, h, MAX_RADIUS * 2, MAX_RADIUS * 2, 220, { minDistance: 56, separationPasses: 20 });
  const most = Math.max(0, ...degree.values());
  const nodes = graph.nodes.map((node, i) => {
    const r = nodeRadius(degree.get(node.id) ?? 0, most);
    return {
      id: node.id,
      label: node.label,
      group: node.group,
      tone: toneOf(node.group),
      x: Math.round(points[i].x - r),
      y: Math.round(points[i].y - r),
      w: Math.round(2 * r),
      h: Math.round(2 * r),
      degree: degree.get(node.id) ?? 0,
    };
  });
  return { nodes, edges: graph.edges, groups };
}
