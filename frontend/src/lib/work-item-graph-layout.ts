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
 * The drawing of the Tasks > Work Items graph (#1062): its direction, layouts
 * and groups. No React: `WorkItemGraphView.tsx` renders what this answers.
 *
 * - Progression: the API's wait edges go from the waiter to what it waits for
 *   (`blocked_on`, `ref`, `person`); the drawing flips them, so a prerequisite
 *   sits LEFT of (or ABOVE) what it releases and the arrow reads "unblocks".
 *   A program edge already goes from the root to a child and is kept.
 * - Layouts: the fleet graph's picker (`GraphLayout`), minus the org chart,
 *   which draws one reporting tree and means nothing here. Left to right and
 *   top down are layered drawings of the cards (dagre, which sizes the
 *   rectangles); force and radial are `layoutGraph` of the agent graph, spaced
 *   for a card so no two overlap; radial runs outward from a root (an item with
 *   nothing to wait for).
 * - Groups: none, program, owner agent, fleet of the owner, key prefix,
 *   status, priority. Each group is laid out on its own, drawn as a zone with
 *   its name and count, and the zones are packed in shelves; a collapsed group
 *   is one node with its count, the edges to its members moved onto it.
 */

import dagre from "dagre";

import { layoutGraph, type GraphLayout } from "./agent-graph-layout.ts";
import { PRIORITY_LABELS, STATUS_LABELS } from "./work-item-context.ts";
import type { WorkItemGraphEdge, WorkItemGraphNode } from "../types/index.ts";

/** The layouts the work items graph offers, in the toolbar's order; left to right is the default. */
export type WorkItemGraphLayout = Exclude<GraphLayout, "orgchart">;
export const WORK_ITEM_GRAPH_LAYOUTS: readonly WorkItemGraphLayout[] = ["hierarchy-lr", "hierarchy-tb", "force", "radial"];
export const DEFAULT_WORK_ITEM_GRAPH_LAYOUT: WorkItemGraphLayout = "hierarchy-lr";

export type WorkItemGraphGroupBy = "none" | "program" | "owner" | "fleet" | "prefix" | "status" | "priority";
export const WORK_ITEM_GRAPH_GROUP_BYS: readonly WorkItemGraphGroupBy[] = ["none", "program", "owner", "fleet", "prefix", "status", "priority"];

/** Edges that point from a waiter to what it waits for in the API. */
const WAIT_KINDS = new Set<WorkItemGraphEdge["kind"]>(["blocked_on", "ref", "person"]);

/**
 * An edge as drawn: from the prerequisite to what it releases (a program edge
 * from the root to a child). The id and kind are the API's, so focus chains
 * and their lit edges keep reading the API edges.
 */
export function progressionEdge(edge: WorkItemGraphEdge): WorkItemGraphEdge {
  return WAIT_KINDS.has(edge.kind) ? { ...edge, source: edge.target, target: edge.source } : edge;
}

export interface Size {
  width: number;
  height: number;
}

interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** A group as the drawing needs it; `label` is a key the view translates or a name it shows as is. */
export interface GraphGroup {
  key: string;
  label: string;
  /** `label` is a sentence of the catalog (No program, Blocked...), not a name. */
  translate: boolean;
  members: string[];
}

export interface GroupContext {
  /** The home fleet of an agent, by agent id (the agents list). */
  fleetOfAgent?: ReadonlyMap<string, { id: string; name: string }>;
}

const NONE = "__none__";

/** The group of an item node; `null` for a node that follows the item it touches (a reference, a person). */
function itemGroup(
  node: WorkItemGraphNode,
  by: WorkItemGraphGroupBy,
  programOf: ReadonlyMap<string, string>,
  titles: ReadonlyMap<string, string>,
  context: GroupContext,
): { key: string; label: string; translate: boolean } | null {
  if (node.kind !== "item") return null;
  switch (by) {
    case "program": {
      const root = node.is_program_root ? node.id : programOf.get(node.id);
      return root ? { key: `program:${root}`, label: titles.get(root) ?? root, translate: false } : { key: NONE, label: "No program", translate: true };
    }
    case "owner":
      return node.owner_agent?.id
        ? { key: `owner:${node.owner_agent.id}`, label: node.owner_agent.name ?? node.owner_agent.id, translate: false }
        : { key: NONE, label: "No owner agent", translate: true };
    case "fleet": {
      const fleet = node.owner_agent?.id ? context.fleetOfAgent?.get(node.owner_agent.id) : undefined;
      return fleet ? { key: `fleet:${fleet.id}`, label: fleet.name, translate: false } : { key: NONE, label: "No fleet", translate: true };
    }
    case "prefix": {
      const prefix = node.correlation_key?.includes(":") ? node.correlation_key.split(":")[0] : null;
      return prefix ? { key: `prefix:${prefix}`, label: `${prefix}:`, translate: false } : { key: NONE, label: "No key prefix", translate: true };
    }
    case "status":
      return { key: `status:${node.status}`, label: STATUS_LABELS[node.status] ?? node.status, translate: true };
    case "priority": {
      const label = node.priority ? PRIORITY_LABELS[node.priority as keyof typeof PRIORITY_LABELS] : undefined;
      return label ? { key: `priority:${node.priority}`, label, translate: true } : { key: NONE, label: "No priority", translate: true };
    }
    default:
      return null;
  }
}

/**
 * The groups of the nodes drawn, sorted by size then name, the "no group"
 * bucket last. A reference or a person joins the group of the first item that
 * waits for it (id order), so a chain's prerequisite stays beside its item.
 */
export function graphGroups(
  nodes: readonly WorkItemGraphNode[],
  edges: readonly WorkItemGraphEdge[],
  by: WorkItemGraphGroupBy,
  context: GroupContext = {},
): GraphGroup[] {
  if (by === "none") return [];
  const programOf = new Map<string, string>();
  for (const edge of edges) if (edge.kind === "program") programOf.set(edge.target, edge.source);
  const titles = new Map(nodes.flatMap((node) => (node.kind === "item" ? [[node.id, node.title] as const] : [])));
  const groups = new Map<string, GraphGroup>();
  const groupOf = new Map<string, string>();
  const add = (id: string, group: { key: string; label: string; translate: boolean }) => {
    const entry = groups.get(group.key) ?? groups.set(group.key, { ...group, members: [] }).get(group.key)!;
    entry.members.push(id);
    groupOf.set(id, group.key);
  };
  for (const node of nodes) {
    const group = itemGroup(node, by, programOf, titles, context);
    if (group) add(node.id, group);
  }
  const waiters = new Map<string, string[]>();
  for (const edge of edges) {
    if (WAIT_KINDS.has(edge.kind)) waiters.set(edge.target, [...(waiters.get(edge.target) ?? []), edge.source]);
  }
  for (const node of nodes) {
    if (node.kind === "item") continue;
    const waiter = (waiters.get(node.id) ?? []).filter((id) => groupOf.has(id)).sort()[0];
    const key = waiter ? groupOf.get(waiter)! : NONE;
    const group = groups.get(key) ?? { key, label: "Other", translate: true, members: [] };
    add(node.id, group);
  }
  return [...groups.values()].sort((a, b) => {
    if (a.key === NONE || b.key === NONE) return a.key === NONE ? 1 : -1;
    return b.members.length - a.members.length || a.label.localeCompare(b.label);
  });
}

/**
 * The items of a group: the count its zone and its collapsed node show, the status
 * line's population (UX r1 B2 on #1067). A reference or a person drawn with an item
 * is a member of its group for the layout, never counted as an item.
 */
export function groupItemCount(group: GraphGroup, nodes: readonly WorkItemGraphNode[]): number {
  const items = new Set(nodes.flatMap((node) => (node.kind === "item" ? [node.id] : [])));
  return group.members.filter((id) => items.has(id)).length;
}

/** Room around the cards of a layout and between two zones. */
const NODE_SEP = 28;
const RANK_SEP = 90;
/** A zone: its header (name, count, collapse button) and its inner margin. */
const ZONE_HEADER = 40;
const ZONE_PAD = 20;
const ZONE_GAP = 48;

/** One drawing of `ids` laid out, top-left corners, from 0,0. */
function layoutPart(
  layout: WorkItemGraphLayout,
  ids: readonly string[],
  edges: readonly { source: string; target: string }[],
  sizeOf: (id: string) => Size,
): Map<string, { x: number; y: number }> {
  if (ids.length === 0) return new Map();
  if (layout === "hierarchy-lr" || layout === "hierarchy-tb") {
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: layout === "hierarchy-lr" ? "LR" : "TB", ranksep: RANK_SEP, nodesep: NODE_SEP, edgesep: 16 });
    g.setDefaultEdgeLabel(() => ({}));
    for (const id of ids) g.setNode(id, sizeOf(id));
    for (const edge of edges) g.setEdge(edge.source, edge.target);
    dagre.layout(g);
    return normalise(new Map(ids.map((id) => {
      const { x, y } = g.node(id);
      const { width, height } = sizeOf(id);
      return [id, { x: x - width / 2, y: y - height / 2 }] as const;
    })));
  }
  // Force and radial place centres: two centres at least a card's diagonal
  // apart can never overlap, whatever their sizes.
  const diagonal = Math.max(...ids.map((id) => Math.hypot(sizeOf(id).width, sizeOf(id).height)));
  const minDistance = Math.ceil(diagonal + NODE_SEP);
  const waits = new Map<string, number>();
  for (const edge of edges) waits.set(edge.target, (waits.get(edge.target) ?? 0) + 1);
  // Radial runs outward from a root: the first node, in id order, with nothing to wait for.
  const centerId = [...ids].sort().find((id) => !waits.has(id)) ?? null;
  const centres = layoutGraph(
    layout,
    ids,
    edges.map((edge) => ({ sourceId: edge.source, targetId: edge.target })),
    { minDistance, layerDistance: minDistance, linkDistance: minDistance * 1.2, centerId },
  );
  return normalise(new Map(ids.map((id) => {
    const p = centres.get(id) ?? { x: 0, y: 0 };
    const { width, height } = sizeOf(id);
    return [id, { x: p.x - width / 2, y: p.y - height / 2 }] as const;
  })));
}

function normalise(points: Map<string, { x: number; y: number }>): Map<string, { x: number; y: number }> {
  const minX = Math.min(...[...points.values()].map((p) => p.x));
  const minY = Math.min(...[...points.values()].map((p) => p.y));
  return new Map([...points].map(([id, p]) => [id, { x: Math.round(p.x - minX), y: Math.round(p.y - minY) }]));
}

function extentOf(points: ReadonlyMap<string, { x: number; y: number }>, sizeOf: (id: string) => Size): Size {
  let width = 0;
  let height = 0;
  for (const [id, p] of points) {
    const size = sizeOf(id);
    width = Math.max(width, p.x + size.width);
    height = Math.max(height, p.y + size.height);
  }
  return { width, height };
}

export interface LaidOutGraph {
  /** Top-left corner of each drawn node (a collapsed group is the node `group:<key>`). */
  positions: Map<string, { x: number; y: number }>;
  /** The zone of each expanded group, around its members. */
  zones: (Box & { group: GraphGroup })[];
  /** The drawn edges, prerequisite to what it releases, moved onto collapsed groups and deduplicated. */
  edges: (WorkItemGraphEdge & { drawnSource: string; drawnTarget: string })[];
  /** Collapsed groups drawn as one node. */
  collapsed: GraphGroup[];
}

/** The node id of a collapsed group. */
export const groupNodeId = (key: string) => `group:${key}`;

/** The size of a collapsed group's node. */
export const GROUP_NODE_SIZE: Size = { width: 220, height: 56 };

/**
 * Lays the graph out: one drawing per group (or one for the whole graph), the
 * zones packed in shelves of about the width that keeps the result close to the
 * canvas's shape, collapsed groups as one node each.
 */
export function layoutWorkItemGraph(
  nodes: readonly WorkItemGraphNode[],
  apiEdges: readonly WorkItemGraphEdge[],
  {
    layout,
    groups,
    collapsedKeys = new Set<string>(),
    sizeOf,
    aspect = 16 / 9,
  }: {
    layout: WorkItemGraphLayout;
    groups: readonly GraphGroup[];
    collapsedKeys?: ReadonlySet<string>;
    sizeOf: (node: WorkItemGraphNode) => Size;
    aspect?: number;
  },
): LaidOutGraph {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const collapsed = groups.filter((group) => collapsedKeys.has(group.key));
  const drawnOf = new Map<string, string>();
  for (const group of collapsed) for (const id of group.members) drawnOf.set(id, groupNodeId(group.key));
  const drawn = (id: string) => drawnOf.get(id) ?? id;
  const size = (id: string): Size => (id.startsWith("group:") && !byId.has(id) ? GROUP_NODE_SIZE : sizeOf(byId.get(id)!));

  const seen = new Set<string>();
  const edges: LaidOutGraph["edges"] = [];
  for (const edge of apiEdges.map(progressionEdge)) {
    const drawnSource = drawn(edge.source);
    const drawnTarget = drawn(edge.target);
    if (drawnSource === drawnTarget) continue;
    const pair = `${edge.kind}|${drawnSource}|${drawnTarget}`;
    if (drawnSource !== edge.source || drawnTarget !== edge.target) {
      if (seen.has(pair)) continue;
      seen.add(pair);
    }
    edges.push({ ...edge, drawnSource, drawnTarget });
  }
  const layoutEdges = edges.map((edge) => ({ source: edge.drawnSource, target: edge.drawnTarget }));

  if (groups.length === 0) {
    return { positions: layoutPart(layout, nodes.map((node) => node.id), layoutEdges, size), zones: [], edges, collapsed: [] };
  }

  // Each expanded group is laid out with its own edges; a collapsed group is one node.
  const parts: { group: GraphGroup | null; ids: string[]; points: Map<string, { x: number; y: number }>; size: Size }[] = [];
  const collapsedIds = collapsed.map((group) => groupNodeId(group.key));
  for (const group of groups) {
    if (collapsedKeys.has(group.key)) continue;
    const members = new Set(group.members);
    const inner = layoutEdges.filter((edge) => members.has(edge.source) && members.has(edge.target));
    const points = layoutPart(layout, group.members, inner, size);
    const extent = extentOf(points, size);
    parts.push({ group, ids: group.members, points, size: { width: extent.width + 2 * ZONE_PAD, height: extent.height + ZONE_HEADER + ZONE_PAD } });
  }
  if (collapsedIds.length) {
    const points = layoutPart(layout === "hierarchy-tb" ? "hierarchy-tb" : "hierarchy-lr", collapsedIds, layoutEdges.filter((e) => collapsedIds.includes(e.source) && collapsedIds.includes(e.target)), size);
    parts.push({ group: null, ids: collapsedIds, points, size: extentOf(points, size) });
  }

  // Shelves: rows of parts, each row as wide as the drawing's target width.
  const area = parts.reduce((sum, part) => sum + (part.size.width + ZONE_GAP) * (part.size.height + ZONE_GAP), 0);
  const rowWidth = Math.max(Math.sqrt(area * aspect), ...parts.map((part) => part.size.width));
  const positions = new Map<string, { x: number; y: number }>();
  const zones: LaidOutGraph["zones"] = [];
  let x = 0;
  let y = 0;
  let rowHeight = 0;
  for (const part of parts) {
    if (x > 0 && x + part.size.width > rowWidth) {
      x = 0;
      y += rowHeight + ZONE_GAP;
      rowHeight = 0;
    }
    const inset = part.group ? { x: ZONE_PAD, y: ZONE_HEADER } : { x: 0, y: 0 };
    for (const [id, p] of part.points) positions.set(id, { x: x + inset.x + p.x, y: y + inset.y + p.y });
    if (part.group) zones.push({ x, y, width: part.size.width, height: part.size.height, group: part.group });
    x += part.size.width + ZONE_GAP;
    rowHeight = Math.max(rowHeight, part.size.height);
  }
  return { positions, zones, edges, collapsed };
}
