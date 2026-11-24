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
 * - Edges (UX r3 B1 on #1067): every drawn edge is routed here, as a polyline,
 *   under one invariant held by every layout and every grouping: no segment
 *   runs inside a card that is not one of its two ends. A direct route (the
 *   ranked step, the floating straight line) is kept when it is clear; else the
 *   edge goes around the cards on an orthogonal grid built from their sides.
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

export interface Box {
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
    // A copy: dagre writes x and y onto the label, never onto a shared size constant.
    for (const id of ids) {
      const { width, height } = sizeOf(id);
      g.setNode(id, { width, height });
    }
    for (const edge of edges) g.setEdge(edge.source, edge.target);
    dagre.layout(g);
    return normalise(new Map(ids.map((id) => {
      const { x, y } = g.node(id);
      const { width, height } = sizeOf(id);
      return [id, { x: x - width / 2, y: y - height / 2 }] as const;
    })));
  }
  // Force and radial draw each connected part on its own (UX r2 B1 on #1067):
  // radial runs rings around the root of its part, never one row of the parts
  // its root does not reach, and the parts are packed in shelves.
  const components = connectedParts(ids, edges);
  if (components.length > 1) {
    const blocks = components.map((members) => {
      const own = new Set(members);
      const points = layoutPart(layout, members, edges.filter((edge) => own.has(edge.source) && own.has(edge.target)), sizeOf);
      return { points, size: extentOf(points, sizeOf) };
    });
    const origins = packShelves(blocks.map((block) => block.size), NODE_SEP * 2);
    const placed = new Map<string, { x: number; y: number }>();
    blocks.forEach((block, i) => {
      for (const [id, p] of block.points) placed.set(id, { x: origins[i].x + p.x, y: origins[i].y + p.y });
    });
    return placed;
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

/** The connected parts of a drawing, largest first, ties in id order. */
function connectedParts(ids: readonly string[], edges: readonly { source: string; target: string }[]): string[][] {
  const next = new Map<string, string[]>(ids.map((id) => [id, []]));
  for (const edge of edges) {
    if (!next.has(edge.source) || !next.has(edge.target)) continue;
    next.get(edge.source)!.push(edge.target);
    next.get(edge.target)!.push(edge.source);
  }
  const seen = new Set<string>();
  const parts: string[][] = [];
  for (const id of [...ids].sort()) {
    if (seen.has(id)) continue;
    const part = [id];
    seen.add(id);
    for (let i = 0; i < part.length; i++) {
      for (const other of next.get(part[i])!) {
        if (!seen.has(other)) {
          seen.add(other);
          part.push(other);
        }
      }
    }
    parts.push(part);
  }
  return parts.sort((a, b) => b.length - a.length || (a[0] < b[0] ? -1 : 1));
}

/**
 * Shelves: the top-left corner of each block, in rows about as wide as keeps the
 * whole close to the canvas's shape, `gap` between two blocks.
 */
function packShelves(sizes: readonly Size[], gap: number, aspect = 16 / 9): { x: number; y: number }[] {
  const area = sizes.reduce((sum, size) => sum + (size.width + gap) * (size.height + gap), 0);
  const rowWidth = Math.max(Math.sqrt(area * aspect), ...sizes.map((size) => size.width));
  const origins: { x: number; y: number }[] = [];
  let x = 0;
  let y = 0;
  let rowHeight = 0;
  for (const size of sizes) {
    if (x > 0 && x + size.width > rowWidth) {
      x = 0;
      y += rowHeight + gap;
      rowHeight = 0;
    }
    origins.push({ x, y });
    x += size.width + gap;
    rowHeight = Math.max(rowHeight, size.height);
  }
  return origins;
}

/**
 * Where a floating edge meets two cards (force and radial, UX r2 B1 on #1067):
 * the points where the line between their centres leaves each box, so the edge
 * runs between the sides that face each other and never through its own cards.
 */
export function floatingEndpoints(source: Box, target: Box): { source: { x: number; y: number }; target: { x: number; y: number } } {
  const border = (box: Box, toward: Box) => {
    const cx = box.x + box.width / 2;
    const cy = box.y + box.height / 2;
    const dx = toward.x + toward.width / 2 - cx;
    const dy = toward.y + toward.height / 2 - cy;
    if (dx === 0 && dy === 0) return { x: cx, y: cy };
    const scale = Math.min(dx === 0 ? Infinity : box.width / 2 / Math.abs(dx), dy === 0 ? Infinity : box.height / 2 / Math.abs(dy));
    return { x: cx + dx * scale, y: cy + dy * scale };
  };
  return { source: border(source, target), target: border(target, source) };
}

export interface Point {
  x: number;
  y: number;
}

/** Room an edge keeps from a card it goes around, and the straight run it leaves and enters a card with. */
const EDGE_CLEARANCE = 10;
const EDGE_STUB = 16;
/** What a bend costs a route, in pixels of length: a longer route with fewer bends reads better. */
const BEND_COST = 40;
/** The spacing of parallel lanes: two unrelated edges never run closer than this along one line. */
const EDGE_LANE = 10;
/** What each pixel run alongside another edge or a zone frame costs a route. */
const SHARED_RUN_COST = 25;

/**
 * A stretch a route should not run along: another edge's axis-aligned segment or
 * a zone frame's side. `horizontal` stretches lie at y = `at` from `from` to `to`.
 */
interface RouteBand {
  horizontal: boolean;
  at: number;
  from: number;
  to: number;
  /** How close counts as running along it. */
  within: number;
}

/** The axis-aligned segments of a route, as bands other routes keep a lane from. */
function routeBands(points: readonly Point[], within = EDGE_LANE - 2): RouteBand[] {
  const bands: RouteBand[] = [];
  for (let i = 1; i < points.length; i++) {
    const [a, b] = [points[i - 1], points[i]];
    if (a.y === b.y && a.x !== b.x) bands.push({ horizontal: true, at: a.y, from: Math.min(a.x, b.x), to: Math.max(a.x, b.x), within });
    else if (a.x === b.x && a.y !== b.y) bands.push({ horizontal: false, at: a.x, from: Math.min(a.y, b.y), to: Math.max(a.y, b.y), within });
  }
  return bands;
}

/** The four sides of a zone frame, as bands a route keeps off. */
function frameBands(box: Box, within = EDGE_LANE): RouteBand[] {
  const right = box.x + box.width;
  const bottom = box.y + box.height;
  return [
    { horizontal: true, at: box.y, from: box.x, to: right, within },
    { horizontal: true, at: bottom, from: box.x, to: right, within },
    { horizontal: false, at: box.x, from: box.y, to: bottom, within },
    { horizontal: false, at: right, from: box.y, to: bottom, within },
  ];
}

/** The length a segment on line `at` (from `a` to `b`) runs along the bands of its direction. */
function alongBands(bands: readonly RouteBand[], at: number, a: number, b: number): number {
  const lo = Math.min(a, b);
  const hi = Math.max(a, b);
  let run = 0;
  for (const band of bands) {
    if (Math.abs(band.at - at) >= band.within) continue;
    const overlap = Math.min(hi, band.to) - Math.max(lo, band.from);
    if (overlap > 0) run += overlap;
  }
  return run;
}

/** The length a route runs along any of `bands`. */
function runAlong(points: readonly Point[], bands: readonly RouteBand[]): number {
  const horizontal = bands.filter((b) => b.horizontal);
  const vertical = bands.filter((b) => !b.horizontal);
  let run = 0;
  for (let i = 1; i < points.length; i++) {
    const [a, b] = [points[i - 1], points[i]];
    if (a.y === b.y) run += alongBands(horizontal, a.y, a.x, b.x);
    else if (a.x === b.x) run += alongBands(vertical, a.x, a.y, b.y);
  }
  return run;
}

/** Whether the segment a-b runs inside `box` (its border excluded): Liang-Barsky clipping. */
export function segmentCrossesBox(a: Point, b: Point, box: Box): boolean {
  const eps = 1e-6;
  const x0 = box.x + eps;
  const x1 = box.x + box.width - eps;
  const y0 = box.y + eps;
  const y1 = box.y + box.height - eps;
  if (x0 >= x1 || y0 >= y1) return false;
  let t0 = 0;
  let t1 = 1;
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const clip = (p: number, q: number) => {
    if (p === 0) return q > 0;
    const r = q / p;
    if (p < 0) {
      if (r > t1) return false;
      if (r > t0) t0 = r;
    } else {
      if (r < t0) return false;
      if (r < t1) t1 = r;
    }
    return true;
  };
  return clip(-dx, a.x - x0) && clip(dx, x1 - a.x) && clip(-dy, a.y - y0) && clip(dy, y1 - a.y) && t1 - t0 > eps;
}

function routeIsClear(points: readonly Point[], obstacles: readonly Box[]): boolean {
  for (let i = 1; i < points.length; i++) {
    for (const box of obstacles) if (segmentCrossesBox(points[i - 1], points[i], box)) return false;
  }
  return true;
}

function inflate(box: Box, by: number): Box {
  return { x: box.x - by, y: box.y - by, width: box.width + 2 * by, height: box.height + 2 * by };
}

type Side = "left" | "right" | "top" | "bottom";
const OUTWARD: Record<Side, Point> = { left: { x: -1, y: 0 }, right: { x: 1, y: 0 }, top: { x: 0, y: -1 }, bottom: { x: 0, y: 1 } };
/** Grid directions: +x, -x, +y, -y. */
const STEPS: readonly Point[] = [{ x: 1, y: 0 }, { x: -1, y: 0 }, { x: 0, y: 1 }, { x: 0, y: -1 }];
const stepOf = (d: Point) => STEPS.findIndex((s) => s.x === d.x && s.y === d.y);

function sideOfPoint(point: Point, box: Box): Side {
  const distances: [Side, number][] = [
    ["left", Math.abs(point.x - box.x)],
    ["right", Math.abs(point.x - (box.x + box.width))],
    ["top", Math.abs(point.y - box.y)],
    ["bottom", Math.abs(point.y - (box.y + box.height))],
  ];
  return distances.reduce((best, entry) => (entry[1] < best[1] ? entry : best))[0];
}

function uniqueSorted(values: number[]): number[] {
  return [...new Set(values)].sort((a, b) => a - b);
}

/** The index of `value` in the sorted `values` (it is one of them). */
function indexOf(values: readonly number[], value: number): number {
  let lo = 0;
  let hi = values.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (values[mid] < value) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * The shortest orthogonal route from `from` to `to` inside `bounds` that runs
 * inside none of `obstacles`, bends counted: Dijkstra on the grid of the
 * obstacles' sides, where a grid segment is either wholly inside a box or wholly
 * outside it. `null` when `bounds` holds no such route.
 */
function gridRoute(
  from: Point,
  to: Point,
  startStep: number,
  endStep: number,
  obstacles: readonly Box[],
  bounds: Box,
  bands: readonly RouteBand[] = [],
): Point[] | null {
  const right = bounds.x + bounds.width;
  const bottom = bounds.y + bounds.height;
  const within = obstacles.filter((b) => b.x < right && b.x + b.width > bounds.x && b.y < bottom && b.y + b.height > bounds.y);
  // The lanes beside each band: where a route runs next to another edge or a frame, not on it.
  const near = bands.filter((b) =>
    b.horizontal ? b.at > bounds.y && b.at < bottom && b.to > bounds.x && b.from < right : b.at > bounds.x && b.at < right && b.to > bounds.y && b.from < bottom,
  );
  const lanes = (horizontal: boolean, lo: number, hi: number) =>
    near
      .filter((b) => b.horizontal === horizontal)
      .flatMap((b) => [1, 2, 3].flatMap((k) => [b.at - k * EDGE_LANE - (b.within - EDGE_LANE + 2), b.at + k * EDGE_LANE + (b.within - EDGE_LANE + 2)]))
      .filter((v) => v > lo && v < hi);
  const xs = uniqueSorted([
    bounds.x,
    right,
    from.x,
    to.x,
    ...within.flatMap((b) => [Math.max(b.x, bounds.x), Math.min(b.x + b.width, right)]),
    ...lanes(false, bounds.x, right),
  ]);
  const ys = uniqueSorted([
    bounds.y,
    bottom,
    from.y,
    to.y,
    ...within.flatMap((b) => [Math.max(b.y, bounds.y), Math.min(b.y + b.height, bottom)]),
    ...lanes(true, bounds.y, bottom),
  ]);
  // The bands each grid line runs near, so a step's shared run is read off a short list.
  const nearRow = ys.map((y) => near.filter((b) => b.horizontal && Math.abs(b.at - y) < b.within));
  const nearColumn = xs.map((x) => near.filter((b) => !b.horizontal && Math.abs(b.at - x) < b.within));
  const nx = xs.length;
  const ny = ys.length;
  const cell = (i: number, j: number) => j * nx + i;
  const blockedNode = new Uint8Array(nx * ny);
  const blockedH = new Uint8Array(nx * ny); // (i, j) -> (i + 1, j)
  const blockedV = new Uint8Array(nx * ny); // (i, j) -> (i, j + 1)
  for (const b of within) {
    const L = Math.max(b.x, bounds.x);
    const R = Math.min(b.x + b.width, right);
    const T = Math.max(b.y, bounds.y);
    const B = Math.min(b.y + b.height, bottom);
    const li = indexOf(xs, L);
    const ri = indexOf(xs, R);
    const tj = indexOf(ys, T);
    const bj = indexOf(ys, B);
    // A side the bounds cut is inside the box: the grid line on it is blocked too.
    const i0 = b.x < L ? li : li + 1;
    const i1 = R < b.x + b.width ? ri : ri - 1;
    const j0 = b.y < T ? tj : tj + 1;
    const j1 = B < b.y + b.height ? bj : bj - 1;
    for (let j = j0; j <= j1; j++) {
      for (let i = i0; i <= i1; i++) blockedNode[cell(i, j)] = 1;
      for (let i = li; i < ri; i++) blockedH[cell(i, j)] = 1;
    }
    for (let i = i0; i <= i1; i++) for (let j = tj; j < bj; j++) blockedV[cell(i, j)] = 1;
  }
  const start = cell(indexOf(xs, from.x), indexOf(ys, from.y));
  const goal = cell(indexOf(xs, to.x), indexOf(ys, to.y));
  if (blockedNode[start] || blockedNode[goal]) return null;

  const states = nx * ny * 4;
  const cost = new Float64Array(states).fill(Infinity);
  const prev = new Int32Array(states).fill(-1);
  const heap: [number, number][] = [];
  const push = (c: number, s: number) => {
    heap.push([c, s]);
    let k = heap.length - 1;
    while (k > 0) {
      const parent = (k - 1) >> 1;
      if (heap[parent][0] <= heap[k][0]) break;
      [heap[parent], heap[k]] = [heap[k], heap[parent]];
      k = parent;
    }
  };
  const pop = (): [number, number] => {
    const top = heap[0];
    const last = heap.pop()!;
    if (heap.length) {
      heap[0] = last;
      let k = 0;
      for (;;) {
        const l = 2 * k + 1;
        const r = l + 1;
        let m = k;
        if (l < heap.length && heap[l][0] < heap[m][0]) m = l;
        if (r < heap.length && heap[r][0] < heap[m][0]) m = r;
        if (m === k) break;
        [heap[m], heap[k]] = [heap[k], heap[m]];
        k = m;
      }
    }
    return top;
  };
  for (let d = 0; d < 4; d++) {
    const c = d === startStep ? 0 : BEND_COST;
    cost[start * 4 + d] = c;
    push(c, start * 4 + d);
  }
  let best = Infinity;
  let bestState = -1;
  while (heap.length) {
    const [c, state] = pop();
    if (c > cost[state] || c >= best) continue;
    const at = state >> 2;
    const dir = state & 3;
    if (at === goal) {
      const total = c + (dir === endStep ? 0 : BEND_COST);
      if (total < best) {
        best = total;
        bestState = state;
      }
      continue;
    }
    const i = at % nx;
    const j = (at - i) / nx;
    for (let d = 0; d < 4; d++) {
      const ni = i + STEPS[d].x;
      const nj = j + STEPS[d].y;
      if (ni < 0 || nj < 0 || ni >= nx || nj >= ny) continue;
      const next = cell(ni, nj);
      if (blockedNode[next]) continue;
      const through = d < 2 ? blockedH[cell(Math.min(i, ni), j)] : blockedV[cell(i, Math.min(j, nj))];
      if (through) continue;
      const length = d < 2 ? Math.abs(xs[ni] - xs[i]) : Math.abs(ys[nj] - ys[j]);
      const shared = d < 2 ? alongBands(nearRow[j], ys[j], xs[i], xs[ni]) : alongBands(nearColumn[i], xs[i], ys[j], ys[nj]);
      const nc = c + length + SHARED_RUN_COST * shared + (d === dir ? 0 : BEND_COST);
      const ns = next * 4 + d;
      if (nc < cost[ns]) {
        cost[ns] = nc;
        prev[ns] = state;
        push(nc, ns);
      }
    }
  }
  if (bestState < 0) return null;
  const path: Point[] = [];
  for (let s = bestState; s >= 0; s = prev[s]) {
    const at = s >> 2;
    const i = at % nx;
    const point = { x: xs[i], y: ys[(at - i) / nx] };
    const last = path[path.length - 1];
    if (!last || last.x !== point.x || last.y !== point.y) path.push(point);
  }
  return path.reverse();
}

/** Drops the points that sit on a straight run between their neighbours. */
function simplify(points: readonly Point[]): Point[] {
  const out: Point[] = [];
  for (const p of points) {
    const last = out[out.length - 1];
    if (last && last.x === p.x && last.y === p.y) continue;
    const before = out[out.length - 2];
    if (before && last && ((before.x === last.x && last.x === p.x) || (before.y === last.y && last.y === p.y))) out[out.length - 1] = p;
    else out.push(p);
  }
  return out;
}

function union(boxes: readonly Box[]): Box {
  const x = Math.min(...boxes.map((b) => b.x));
  const y = Math.min(...boxes.map((b) => b.y));
  return { x, y, width: Math.max(...boxes.map((b) => b.x + b.width)) - x, height: Math.max(...boxes.map((b) => b.y + b.height)) - y };
}

/**
 * The route of one edge from `source` to `target` among `cards` (every card
 * drawn, both ends included). Ranked layouts leave a card on its trailing side
 * and enter on its leading one (`lr`: right to left, `tb`: bottom to top);
 * force and radial float, on the sides facing each other. The direct route is
 * kept when it clears every other card and runs along none of `bands` (the
 * routes already drawn, the zone frames); else the edge goes around, in a lane
 * of its own.
 */
export function routeEdge(
  source: Box,
  target: Box,
  ranked: "lr" | "tb" | null,
  cards: readonly Box[],
  extent: Box,
  bands: readonly RouteBand[] = [],
): Point[] {
  let p1: Point;
  let p2: Point;
  // The direct candidates: the ranked step with its middle run shifted lane by lane, or the floating line.
  const directs: Point[][] = [];
  if (ranked === "lr") {
    p1 = { x: source.x + source.width, y: source.y + source.height / 2 };
    p2 = { x: target.x, y: target.y + target.height / 2 };
    const midX = (p1.x + p2.x) / 2;
    for (const shift of [0, 1, -1, 2, -2, 3, -3, 4, -4]) {
      const x = Math.round(midX + shift * EDGE_LANE);
      if (x - p1.x >= EDGE_STUB && p2.x - x >= EDGE_STUB) directs.push([p1, { x, y: p1.y }, { x, y: p2.y }, p2]);
    }
  } else if (ranked === "tb") {
    p1 = { x: source.x + source.width / 2, y: source.y + source.height };
    p2 = { x: target.x + target.width / 2, y: target.y };
    const midY = (p1.y + p2.y) / 2;
    for (const shift of [0, 1, -1, 2, -2, 3, -3, 4, -4]) {
      const y = Math.round(midY + shift * EDGE_LANE);
      if (y - p1.y >= EDGE_STUB && p2.y - y >= EDGE_STUB) directs.push([p1, { x: p1.x, y }, { x: p2.x, y }, p2]);
    }
  } else {
    const ends = floatingEndpoints(source, target);
    p1 = ends.source;
    p2 = ends.target;
    directs.push([p1, p2]);
  }
  const direct = directs[0] ?? [];
  const others = cards.filter((card) => card !== source && card !== target);
  const kept = others.map((card) => inflate(card, EDGE_CLEARANCE));
  for (const candidate of directs) {
    const route = simplify(candidate);
    if (routeIsClear(route, kept) && runAlong(route, bands) === 0) return route;
  }

  const sourceSide = ranked === "lr" ? "right" : ranked === "tb" ? "bottom" : sideOfPoint(p1, source);
  const targetSide = ranked === "lr" ? "left" : ranked === "tb" ? "top" : sideOfPoint(p2, target);
  const out = OUTWARD[sourceSide];
  const inward = OUTWARD[targetSide];
  const s1 = { x: p1.x + out.x * EDGE_STUB, y: p1.y + out.y * EDGE_STUB };
  const s2 = { x: p2.x + inward.x * EDGE_STUB, y: p2.y + inward.y * EDGE_STUB };
  const startStep = stepOf(out);
  const endStep = stepOf({ x: -inward.x, y: -inward.y });
  const whole = inflate(union([extent, { x: Math.min(s1.x, s2.x), y: Math.min(s1.y, s2.y), width: Math.abs(s1.x - s2.x), height: Math.abs(s1.y - s2.y) }]), 4 * EDGE_CLEARANCE);
  // Room to go around: grown from the two ends until it holds the whole drawing.
  // The ends' own cards stay obstacles beyond their stubs; the other cards are kept 8 px off at least.
  for (const clearance of [EDGE_CLEARANCE, 8, 1]) {
    const obstacles = cards.map((card) => inflate(card, clearance));
    for (let margin = 200; ; margin *= 2) {
      const local = inflate({ x: Math.min(s1.x, s2.x), y: Math.min(s1.y, s2.y), width: Math.abs(s1.x - s2.x), height: Math.abs(s1.y - s2.y) }, margin);
      const covers = local.x <= whole.x && local.y <= whole.y && local.x + local.width >= whole.x + whole.width && local.y + local.height >= whole.y + whole.height;
      const route = gridRoute(s1, s2, startStep, endStep, obstacles, covers ? whole : local, bands);
      if (route) return simplify([p1, ...route, p2]);
      if (covers) break;
    }
  }
  return simplify(direct.length ? direct : [p1, p2]);
}

/** The SVG path of a route, its bends rounded. */
export function edgePath(points: readonly Point[], radius = 8): string {
  if (points.length < 2) return "";
  const towards = (from: Point, to: Point, r: number) => {
    const length = Math.hypot(to.x - from.x, to.y - from.y) || 1;
    return { x: from.x + ((to.x - from.x) * r) / length, y: from.y + ((to.y - from.y) * r) / length };
  };
  let d = `M ${points[0].x} ${points[0].y}`;
  for (let i = 1; i < points.length - 1; i++) {
    const [p, c, n] = [points[i - 1], points[i], points[i + 1]];
    const r = Math.min(radius, Math.hypot(c.x - p.x, c.y - p.y) / 2, Math.hypot(n.x - c.x, n.y - c.y) / 2);
    const a = towards(c, p, r);
    const b = towards(c, n, r);
    d += ` L ${a.x} ${a.y} Q ${c.x} ${c.y} ${b.x} ${b.y}`;
  }
  const last = points[points.length - 1];
  return `${d} L ${last.x} ${last.y}`;
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
  /**
   * The drawn edges, prerequisite to what it releases, moved onto collapsed groups
   * and deduplicated; `points` is the route, inside no card but its two ends.
   */
  edges: (WorkItemGraphEdge & { drawnSource: string; drawnTarget: string; points: Point[] })[];
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
  const edges: (WorkItemGraphEdge & { drawnSource: string; drawnTarget: string })[] = [];
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

  // Each edge is routed in turn; a later one keeps a lane off the routes drawn
  // before it (unless they share an end: a fan in or out may merge) and off the zone frames.
  const routed = (positions: Map<string, { x: number; y: number }>, zones: readonly Box[]): LaidOutGraph["edges"] => {
    const boxes = new Map([...positions].map(([id, p]) => [id, { x: p.x, y: p.y, width: size(id).width, height: size(id).height }] as const));
    const cards = [...boxes.values()];
    if (cards.length === 0) return [];
    const extent = union(zones.length ? [...cards, ...zones] : cards);
    const ranked = layout === "hierarchy-lr" ? "lr" : layout === "hierarchy-tb" ? "tb" : null;
    const frames = zones.flatMap((zone) => frameBands(zone));
    const drawnRoutes: { ends: [string, string]; bands: RouteBand[] }[] = [];
    return edges.flatMap((edge) => {
      const source = boxes.get(edge.drawnSource);
      const target = boxes.get(edge.drawnTarget);
      if (!source || !target) return [];
      const ends = [edge.drawnSource, edge.drawnTarget];
      const bands = [...frames, ...drawnRoutes.filter((r) => !r.ends.some((id) => ends.includes(id))).flatMap((r) => r.bands)];
      const points = routeEdge(source, target, ranked, cards, extent, bands);
      drawnRoutes.push({ ends: [edge.drawnSource, edge.drawnTarget], bands: routeBands(points) });
      return [{ ...edge, points }];
    });
  };

  if (groups.length === 0) {
    const positions = layoutPart(layout, nodes.map((node) => node.id), layoutEdges, size);
    return { positions, zones: [], edges: routed(positions, []), collapsed: [] };
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
  const origins = packShelves(parts.map((part) => part.size), ZONE_GAP, aspect);
  const positions = new Map<string, { x: number; y: number }>();
  const zones: LaidOutGraph["zones"] = [];
  parts.forEach((part, i) => {
    const { x, y } = origins[i];
    const inset = part.group ? { x: ZONE_PAD, y: ZONE_HEADER } : { x: 0, y: 0 };
    for (const [id, p] of part.points) positions.set(id, { x: x + inset.x + p.x, y: y + inset.y + p.y });
    if (part.group) zones.push({ x, y, width: part.size.width, height: part.size.height, group: part.group });
  });
  return { positions, zones, edges: routed(positions, zones), collapsed };
}
