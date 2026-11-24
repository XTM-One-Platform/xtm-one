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
 * The pure reading of `GET /work-items/graph` (Tasks > Work Items, Graph view):
 * what is drawn (done and cancelled hidden unless asked, isolated items
 * collapsed into a count), the focus chains of a selected item and the
 * critical leaves (what only a person can release). No React, no layout:
 * `WorkItemGraphView.tsx` lays the result out with dagre.
 */

import type { WorkItemGraph, WorkItemGraphEdge, WorkItemGraphNode } from "../types/index.ts";

const TERMINAL = new Set(["done", "cancelled"]);

/** Edges that point from a waiter to what it waits for (a program edge points from the root to a child). */
const WAIT_KINDS = new Set<WorkItemGraphEdge["kind"]>(["blocked_on", "ref", "person"]);

interface ShownGraph {
  nodes: WorkItemGraphNode[];
  edges: WorkItemGraphEdge[];
  /** Items with no edge at all, left out of the canvas and counted. */
  isolated: number;
}

function isTerminal(node: WorkItemGraphNode): boolean {
  return node.kind === "item" && TERMINAL.has(node.status);
}

/**
 * What the canvas draws: done and cancelled items hidden unless *showDone*
 * (a blocker outside the filter stays, so a chain shows what it waited for),
 * references and people kept only while something drawn points at them,
 * isolated items collapsed into a count.
 */
export function shownGraph(graph: WorkItemGraph, { showDone }: { showDone: boolean }): ShownGraph {
  const kept = graph.nodes.filter(
    (node) => showDone || !isTerminal(node) || (node.kind === "item" && node.outside_filter),
  );
  let ids = new Set(kept.map((node) => node.id));
  let edges = graph.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target));
  const touched = new Set(edges.flatMap((edge) => [edge.source, edge.target]));
  // An outside-filter blocker or an external node nobody drawn points at goes too.
  const nodes = kept.filter(
    (node) => touched.has(node.id) || (node.kind === "item" && !node.outside_filter),
  );
  ids = new Set(nodes.map((node) => node.id));
  edges = edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target));
  const linked = nodes.filter((node) => touched.has(node.id));
  return { nodes: linked, edges, isolated: nodes.length - linked.length };
}

/**
 * The status line's counts of a drawing: its items, its links and the items
 * waiting for a person. One population with the zones (UX r2 B2 on #1067): the
 * graph as drawn, so Show only counts the focused chain alone.
 */
export function drawnCounts(graph: { nodes: WorkItemGraphNode[]; edges: WorkItemGraphEdge[] }): { items: number; links: number; waitingForPerson: number } {
  return {
    items: graph.nodes.filter((node) => node.kind === "item").length,
    links: graph.edges.length,
    waitingForPerson: new Set(graph.edges.filter((edge) => edge.kind === "person").map((edge) => edge.source)).size,
  };
}

/** Every node reached from *start* along *next*, *start* excluded. */
function reach(start: string, next: Map<string, string[]>): Set<string> {
  const seen = new Set<string>();
  const queue = [...(next.get(start) ?? [])];
  while (queue.length) {
    const id = queue.shift() as string;
    if (seen.has(id) || id === start) continue;
    seen.add(id);
    queue.push(...(next.get(id) ?? []));
  }
  return seen;
}

interface FocusChains {
  /** What the selected item waits for, recursively. */
  upstream: Set<string>;
  /** What waits for it, recursively. */
  downstream: Set<string>;
  /** The edges on either chain. */
  edges: Set<string>;
}

/** The chains a selected node lights: upstream along wait edges, downstream against them. */
export function focusChains(edges: WorkItemGraphEdge[], selected: string): FocusChains {
  const waitsFor = new Map<string, string[]>();
  const waitedBy = new Map<string, string[]>();
  for (const edge of edges) {
    if (!WAIT_KINDS.has(edge.kind)) continue;
    waitsFor.set(edge.source, [...(waitsFor.get(edge.source) ?? []), edge.target]);
    waitedBy.set(edge.target, [...(waitedBy.get(edge.target) ?? []), edge.source]);
  }
  const upstream = reach(selected, waitsFor);
  const downstream = reach(selected, waitedBy);
  const up = new Set([selected, ...upstream]);
  const down = new Set([selected, ...downstream]);
  const lit = new Set(
    edges
      .filter((edge) => WAIT_KINDS.has(edge.kind) && ((up.has(edge.source) && up.has(edge.target)) || (down.has(edge.source) && down.has(edge.target))))
      .map((edge) => edge.id),
  );
  return { upstream, downstream, edges: lit };
}

/**
 * The critical leaves: the items that wait for a person and for nothing a
 * platform event can release (no open item, no unsatisfied reference).
 * Nobody but that person can move them, and every chain above them waits.
 */
export function criticalLeaves(graph: { nodes: WorkItemGraphNode[]; edges: WorkItemGraphEdge[] }): Set<string> {
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const out = new Set<string>();
  for (const node of graph.nodes) {
    if (node.kind !== "item" || !node.needs_human || isTerminal(node)) continue;
    const pending = graph.edges.filter((edge) => {
      if (edge.source !== node.id || edge.kind === "person" || !WAIT_KINDS.has(edge.kind)) return false;
      const target = byId.get(edge.target);
      if (!target) return false;
      if (target.kind === "ref") return !target.satisfied;
      return target.kind === "item" && !isTerminal(target);
    });
    if (pending.length === 0) out.add(node.id);
  }
  return out;
}
