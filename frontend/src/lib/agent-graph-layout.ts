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
 * Layouts of the agent graph (Agentic Flow map, fleet views).
 *
 * Pure, deterministic functions: the same nodes and edges always give the
 * same positions, whatever order they arrive in (ids are sorted first), so a
 * poll that refetches the agents never moves a node. Four layouts, modelled
 * on the OpenCTI graph (opencti-front `components/graph/utils/graphLayouts.ts`):
 *
 * - `force`: a seeded Fruchterman-Reingold run with a fixed iteration count;
 * - `hierarchy-tb` / `hierarchy-lr`: a layered (Sugiyama) drawing with
 *   deterministic cycle breaking (depth-first from the roots, sorted ids),
 *   longest-path layering and barycentre ordering sweeps; the edges given are
 *   the hierarchy (handovers from the roots in the playground, the declared
 *   tree in a fleet);
 * - `radial`: rings of breadth-first distance around one node (the selection);
 * - `orgchart`: a top-down tree, one horizontal bus per parent and
 *   children centred under it, a parent's leaves folded into columns when a
 *   row gets too wide; its edges are routed orthogonally (`orgChartRoutes`).
 *
 * Every layout ends with `separate`: no two nodes closer than `minDistance`,
 * so nothing overlaps at 7 or 150 nodes (unit tests in agent-graph-layout.test.ts).
 *
 * With a canvas `aspect` (the map) every layout packs its handover
 * components in shelves shaped for the canvas (`packShelves`) and puts the
 * agents with no handover in a grid beside or under them (`isolatedCluster`):
 * one drawing per component, never one tall drawing of all of them.
 */

export type GraphLayout = "force" | "hierarchy-tb" | "hierarchy-lr" | "radial" | "orgchart";

export const GRAPH_LAYOUTS: readonly GraphLayout[] = ["force", "hierarchy-tb", "hierarchy-lr", "radial", "orgchart"];

export interface LayoutEdge {
  sourceId: string;
  targetId: string;
}

export interface Point {
  x: number;
  y: number;
}

export interface LayoutOptions {
  /** Smallest distance between two node centres (node diameter plus its label room). */
  minDistance?: number;
  /** Distance between two layers (hierarchy) or two rings (radial). */
  layerDistance?: number;
  /** Centre of the radial layout; the first node in id order when absent or unknown. */
  centerId?: string | null;
  /** Nodes per row of the strip that holds the agents with no edge (hierarchy). */
  isolatedPerRow?: number;
  /**
   * Room a node's name takes across (hierarchy): two nodes side by side on the
   * same row are at least this far apart, so their labels never overlap.
   */
  labelWidth?: number;
  /**
   * Width over height of the canvas the drawing is scaled into. When
   * set, the agents with no edge form a grid cluster beside or under the
   * connected drawing, with as many columns as gives the whole drawing the
   * largest scale in that canvas, instead of a strip of fixed rows: at 150
   * agents a strip set the drawing's width alone and every name was cut.
   */
  aspect?: number;
  /** Ideal edge length of the force layout (default: 1.4 x `minDistance`). */
  linkDistance?: number;
  /** Multiplier of the force layout's repulsion between two nodes (default 1). */
  charge?: number;
  /**
   * The group of each node (its fleet), with `aspect` only:
   * the agents with no handover of a group of two or more form their own
   * grid, packed beside the handover drawings, instead of being mixed into
   * the one grid of lone agents. A node with no group stays in that grid.
   */
  groups?: ReadonlyMap<string, string>;
  /**
   * Most nodes on one row of a top-down hierarchy: a wider layer wraps onto
   * several rows, a layer distance apart, so a wide and short frame (the
   * fleet About mini graph) spreads siblings across instead of shrinking
   * them into one row of cut names. Unset: a layer is one row.
   */
  layerWrap?: number;
  /**
   * Layer each node at its shortest handover depth from a root instead of its
   * longest (hierarchy): a cross-link between two nodes of one depth (a hunt
   * creator handing to a rule converter) keeps them on one row instead of
   * adding a row per link. The fleet About mini graph sets it: its frame is
   * short, and a chain of rows there left the drawing over its height.
   */
  shallowLayers?: boolean;
}

/** The force controls of the graph toolbar, in layout units. */
export interface GraphForces {
  /** Multiplier of the repulsion between two nodes. */
  charge: number;
  /** Ideal edge length. */
  linkDistance: number;
  /** Smallest distance between two node centres. */
  collide: number;
}

/**
 * Smallest distance between two node centres for `n` nodes drawn: a
 * handful sit 115 units apart, 150 get 175, so that once the drawing is scaled
 * into the canvas a node keeps a name's room around it; never past 175. A
 * quarter more than the first cut, which left names touching their
 * neighbours on a 45-agent map at 1440 px.
 */
export function spacingFor(n: number): number {
  if (n <= 20) return 115;
  return Math.min(175, Math.round(115 + 25 * Math.log2(n / 20)));
}

/** Bounds of the force controls of the graph toolbar. */
export const FORCE_LIMITS = {
  charge: { min: 0.2, max: 3, step: 0.1, default: 1 },
  linkDistance: { min: 60, max: 360, step: 10, default: 160 },
  collide: { min: 60, max: 240, step: 10, default: 120 },
} as const;

// Automatic forces of the map: an ideal edge as long as the node
// distance and half the default repulsion. With 1.4 node distances and the
// full repulsion the 93-agent lab map came out 7791 x 3464 units, and once
// scaled into the canvas its nodes sat 19 px apart on screen whatever the
// node distance: the looser layout only made the fit smaller.
const AUTO_LINK_RATIO = 1;
const AUTO_CHARGE = 0.5;

/** The forces the map uses for `n` nodes until the user moves a Forces slider. */
export function automaticForces(n: number): GraphForces {
  const snap = (v: number, step: number) => Math.round(v / step) * step;
  const collide = snap(spacingFor(n), FORCE_LIMITS.collide.step);
  return {
    charge: AUTO_CHARGE,
    linkDistance: Math.max(FORCE_LIMITS.linkDistance.min, snap(collide * AUTO_LINK_RATIO, FORCE_LIMITS.linkDistance.step)),
    collide,
  };
}

// A name is drawn across, under its node: two nodes side by side need more
// room than two stacked ones. The force drawing separates nodes on an ellipse
// up to this many node distances wide (the label box), never a circle.
const LABEL_BOX_ACROSS_MAX = 1.6;

const DEFAULTS = { minDistance: 90, layerDistance: 150, isolatedPerRow: 12, labelWidth: 150 };
// Fewest agents in a row of the isolated strip, however narrow the hierarchy.
const MIN_STRIP_ROW = 4;
// Width of a cell of the lone-agent grid, in node distances: wide,
// short cells, so a name of about a dozen characters fits beside its
// neighbours once the drawing is scaled into the canvas, where square cells
// left no name drawn at 150 agents.
const LONE_CELL_ACROSS = 2.4;

/** Width of a cell of the lone-agent grid for this node distance and label room. */
export function loneCellWidth(minDistance: number, labelWidth: number): number {
  return Math.max(labelWidth, minDistance * LONE_CELL_ACROSS);
}

function sortedUnique(ids: Iterable<string>): string[] {
  return [...new Set(ids)].sort();
}

/** The edges between known nodes, self loops and duplicates dropped, in a stable order. */
function cleanEdges(nodeIds: Set<string>, edges: readonly LayoutEdge[]): LayoutEdge[] {
  const seen = new Set<string>();
  const out: LayoutEdge[] = [];
  for (const e of edges) {
    if (e.sourceId === e.targetId || !nodeIds.has(e.sourceId) || !nodeIds.has(e.targetId)) continue;
    const key = `${e.sourceId}\u0000${e.targetId}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ sourceId: e.sourceId, targetId: e.targetId });
  }
  return out.sort((a, b) => a.sourceId.localeCompare(b.sourceId) || a.targetId.localeCompare(b.targetId));
}

/**
 * Edges reversed to make the graph acyclic: a depth-first walk from the roots
 * (no inbound edge), then from the remaining nodes, all in id order; an edge
 * back to a node on the current path is a back edge and is reversed.
 */
export function breakCycles(ids: readonly string[], edges: readonly LayoutEdge[]): LayoutEdge[] {
  const nodeIds = sortedUnique(ids);
  const clean = cleanEdges(new Set(nodeIds), edges);
  const out = new Map<string, string[]>();
  const inbound = new Map<string, number>();
  for (const id of nodeIds) { out.set(id, []); inbound.set(id, 0); }
  for (const e of clean) {
    out.get(e.sourceId)!.push(e.targetId);
    inbound.set(e.targetId, inbound.get(e.targetId)! + 1);
  }
  const state = new Map<string, 1 | 2>(); // 1 on the path, 2 done
  const reversed = new Set<string>();
  const visit = (start: string) => {
    // Iterative DFS so a long chain never overflows the stack.
    const stack: Array<{ id: string; next: number }> = [{ id: start, next: 0 }];
    state.set(start, 1);
    while (stack.length > 0) {
      const top = stack[stack.length - 1];
      const targets = out.get(top.id)!;
      if (top.next >= targets.length) {
        state.set(top.id, 2);
        stack.pop();
        continue;
      }
      const t = targets[top.next++];
      const s = state.get(t);
      if (s === 1) reversed.add(`${top.id}\u0000${t}`);
      else if (s === undefined) {
        state.set(t, 1);
        stack.push({ id: t, next: 0 });
      }
    }
  };
  for (const id of nodeIds) if (inbound.get(id) === 0 && !state.has(id)) visit(id);
  for (const id of nodeIds) if (!state.has(id)) visit(id);
  return clean.map((e) =>
    reversed.has(`${e.sourceId}\u0000${e.targetId}`) ? { sourceId: e.targetId, targetId: e.sourceId } : e,
  );
}

/** Longest-path layering of an acyclic graph: a node sits one layer below its deepest parent. */
export function longestPathLayers(ids: readonly string[], acyclic: readonly LayoutEdge[]): Map<string, number> {
  const nodeIds = sortedUnique(ids);
  const parents = new Map<string, string[]>();
  const children = new Map<string, string[]>();
  const pending = new Map<string, number>();
  for (const id of nodeIds) { parents.set(id, []); children.set(id, []); pending.set(id, 0); }
  for (const e of cleanEdges(new Set(nodeIds), acyclic)) {
    parents.get(e.targetId)!.push(e.sourceId);
    children.get(e.sourceId)!.push(e.targetId);
    pending.set(e.targetId, pending.get(e.targetId)! + 1);
  }
  const layer = new Map<string, number>();
  const queue = nodeIds.filter((id) => pending.get(id) === 0);
  for (let i = 0; i < queue.length; i++) {
    const id = queue[i];
    layer.set(id, Math.max(0, ...parents.get(id)!.map((p) => (layer.get(p) ?? 0) + 1)));
    for (const c of children.get(id)!) {
      pending.set(c, pending.get(c)! - 1);
      if (pending.get(c) === 0) queue.push(c);
    }
  }
  // A cycle the caller left in: its nodes go under everything already placed.
  const bottom = Math.max(0, ...layer.values()) + 1;
  for (const id of nodeIds) if (!layer.has(id)) layer.set(id, bottom);
  return layer;
}

/**
 * Each node's layer: its shortest distance from a root (a node with no parent
 * in `acyclic`), breadth first; a node no root reaches goes under the rest.
 */
function shortestPathLayers(ids: readonly string[], acyclic: readonly LayoutEdge[]): Map<string, number> {
  const nodeIds = sortedUnique(ids);
  const children = new Map<string, string[]>();
  const hasParent = new Set<string>();
  for (const id of nodeIds) children.set(id, []);
  for (const e of cleanEdges(new Set(nodeIds), acyclic)) {
    children.get(e.sourceId)!.push(e.targetId);
    hasParent.add(e.targetId);
  }
  const layer = new Map<string, number>();
  const queue = nodeIds.filter((id) => !hasParent.has(id));
  for (const id of queue) layer.set(id, 0);
  for (let i = 0; i < queue.length; i++) {
    const id = queue[i];
    for (const c of children.get(id)!) {
      if (layer.has(c)) continue;
      layer.set(c, layer.get(id)! + 1);
      queue.push(c);
    }
  }
  const bottom = Math.max(0, ...layer.values()) + 1;
  for (const id of nodeIds) if (!layer.has(id)) layer.set(id, bottom);
  return layer;
}

/**
 * Order every layer by the barycentre of its neighbours in the layer above,
 * then the layer below, a fixed number of sweeps; ties keep the id order.
 */
export function orderLayers(layers: string[][], edges: readonly LayoutEdge[], sweeps = 4): string[][] {
  const result = layers.map((l) => [...l].sort());
  const up = new Map<string, string[]>();
  const down = new Map<string, string[]>();
  for (const e of edges) {
    (down.get(e.sourceId) ?? down.set(e.sourceId, []).get(e.sourceId)!).push(e.targetId);
    (up.get(e.targetId) ?? up.set(e.targetId, []).get(e.targetId)!).push(e.sourceId);
  }
  const reorder = (layerIdx: number, ref: string[], neighbours: Map<string, string[]>) => {
    const pos = new Map(ref.map((id, i) => [id, i]));
    const current = result[layerIdx];
    const keyed = current.map((id, i) => {
      const ns = (neighbours.get(id) ?? []).filter((n) => pos.has(n));
      const bary = ns.length > 0 ? ns.reduce((s, n) => s + pos.get(n)!, 0) / ns.length : i;
      return { id, bary, i };
    });
    keyed.sort((a, b) => a.bary - b.bary || a.i - b.i);
    result[layerIdx] = keyed.map((k) => k.id);
  };
  for (let s = 0; s < sweeps; s++) {
    for (let l = 1; l < result.length; l++) reorder(l, result[l - 1], up);
    for (let l = result.length - 2; l >= 0; l--) reorder(l, result[l + 1], down);
  }
  return result;
}

/** Layered drawing; `vertical` puts the roots on top, otherwise on the left. */
export function layeredLayout(
  ids: readonly string[],
  edges: readonly LayoutEdge[],
  vertical: boolean,
  options: LayoutOptions = {},
): Map<string, Point> {
  const { minDistance, isolatedPerRow, labelWidth } = { ...DEFAULTS, ...options };
  // Two layers are never closer than two nodes.
  const layerDistance = Math.max(options.layerDistance ?? DEFAULTS.layerDistance, minDistance);
  // Horizontal neighbours need the room of a label, vertical ones a node's.
  const rowGap = Math.max(minDistance, labelWidth);
  const acrossGap = vertical ? rowGap : minDistance;
  const alongGap = vertical ? layerDistance : Math.max(layerDistance, rowGap);
  const nodeIds = sortedUnique(ids);
  const acyclic = breakCycles(nodeIds, edges);
  const linked = new Set<string>();
  for (const e of acyclic) { linked.add(e.sourceId); linked.add(e.targetId); }
  const connected = nodeIds.filter((id) => linked.has(id));
  const isolated = nodeIds.filter((id) => !linked.has(id));

  if (options.aspect) {
    // Each handover component layered on its own and the components packed
    // in shelves shaped for the canvas: layered together, a hub of
    // twenty and six small trees made one tall drawing that, scaled into the
    // canvas, put 34 px glyphs 7 px apart.
    const blocks = connectedComponents(connected, acyclic).map((comp) => {
      const own = new Set(comp);
      const compEdges = acyclic.filter((e) => own.has(e.sourceId) && own.has(e.targetId));
      return layeredLayout(comp, compEdges, vertical, { ...options, aspect: undefined });
    });
    return packForCanvas(blocks, isolated, rowGap, minDistance, loneCellWidth(minDistance, labelWidth), options.aspect, options.groups);
  }

  const layerOf = options.shallowLayers ? shortestPathLayers(connected, acyclic) : longestPathLayers(connected, acyclic);
  const depth = connected.length > 0 ? Math.max(...layerOf.values()) + 1 : 0;
  const layers: string[][] = Array.from({ length: depth }, () => []);
  for (const id of connected) layers[layerOf.get(id)!].push(id);
  const ordered = orderLayers(layers, acyclic);

  // A top-down layer wider than `layerWrap` takes several rows.
  const wrap = vertical && options.layerWrap && options.layerWrap > 0 ? Math.floor(options.layerWrap) : 0;
  const rows = wrap
    ? ordered.flatMap((layer) => Array.from({ length: Math.max(1, Math.ceil(layer.length / wrap)) }, (_, i) => layer.slice(i * wrap, (i + 1) * wrap)))
    : ordered;
  // A wrapped row is staggered by half a slot against the row above it, so
  // each of its nodes sits under a gap between two names of that row: an
  // edge from the layer above drops through the gap, never through a name
  // (#9760 UX round 7, B1).
  const shiftOf = new Map<number, number>();
  if (wrap) {
    let rowIdx = 0;
    for (const layer of ordered) {
      const count = Math.max(1, Math.ceil(layer.length / wrap));
      for (let i = 1; i < count; i++) {
        const above = rows[rowIdx + i - 1].length;
        const here = rows[rowIdx + i].length;
        const aboveShift = shiftOf.get(rowIdx + i - 1) ?? 0;
        // Centred rows of the same parity line up: shift this one by half a slot.
        const aligned = (above - here) % 2 === 0;
        shiftOf.set(rowIdx + i, aligned ? (aboveShift === 0 ? acrossGap / 2 : 0) : aboveShift);
      }
      rowIdx += count;
    }
  }
  const widest = Math.max(1, ...rows.map((l) => l.length));
  const positions = new Map<string, Point>();
  // Every layer and every strip row is centred on the same axis (across = 0),
  // so the hierarchy is never pushed to one side by a wider strip.
  rows.forEach((layer, li) => {
    layer.forEach((id, oi) => {
      const along = li * alongGap;
      const across = (oi - (layer.length - 1) / 2) * acrossGap + (shiftOf.get(li) ?? 0);
      positions.set(id, vertical ? { x: across, y: along } : { x: along, y: across });
    });
  });

  // The agents with no edge: a strip after the last layer, wrapped in rows no
  // wider than one and a half times the widest layer, so the strip does not
  // set the drawing's width (and with it the scale) on its own.
  const perRow = connected.length > 0
    ? Math.max(MIN_STRIP_ROW, Math.min(isolatedPerRow, Math.ceil(widest * 1.5)))
    : isolatedPerRow;
  const stripStart = rows.length * alongGap;
  isolated.forEach((id, i) => {
    const row = Math.floor(i / perRow);
    const inRow = Math.min(perRow, isolated.length - row * perRow);
    const col = i % perRow;
    // Rows of the strip run across the flow: in a left-to-right drawing a
    // strip row is a column, so its rows sit a label apart.
    const along = stripStart + row * (vertical ? minDistance : rowGap);
    const across = (col - (inRow - 1) / 2) * acrossGap;
    positions.set(id, vertical ? { x: across, y: along } : { x: along, y: across });
  });
  return positions;
}

/** Rings of breadth-first distance (edges taken both ways) around `centerId`. */
export function radialLayout(
  ids: readonly string[],
  edges: readonly LayoutEdge[],
  options: LayoutOptions = {},
): Map<string, Point> {
  const { minDistance } = { ...DEFAULTS, ...options };
  // Two rings are never closer than two nodes.
  const layerDistance = Math.max(options.layerDistance ?? DEFAULTS.layerDistance, minDistance);
  const nodeIds = sortedUnique(ids);
  const positions = new Map<string, Point>();
  if (nodeIds.length === 0) return positions;
  const center = options.centerId && nodeIds.includes(options.centerId) ? options.centerId : nodeIds[0];
  const adj = new Map<string, string[]>(nodeIds.map((id) => [id, []]));
  const clean = cleanEdges(new Set(nodeIds), edges);
  for (const e of clean) {
    adj.get(e.sourceId)!.push(e.targetId);
    adj.get(e.targetId)!.push(e.sourceId);
  }
  for (const list of adj.values()) list.sort();
  const level = new Map<string, number>([[center, 0]]);
  const queue = [center];
  for (let i = 0; i < queue.length; i++) {
    for (const n of adj.get(queue[i])!) {
      if (!level.has(n)) { level.set(n, level.get(queue[i])! + 1); queue.push(n); }
    }
  }
  // Unreached nodes sit in rows under the rings: as an outer ring they would
  // widen the drawing so much that, scaled into the canvas, the rings around
  // the selection would squeeze their names together.
  const rings = new Map<number, string[]>();
  for (const id of queue) (rings.get(level.get(id)!) ?? rings.set(level.get(id)!, []).get(level.get(id)!)!).push(id);
  const unreached = nodeIds.filter((id) => !level.has(id));

  let radius = 0;
  for (const ringLevel of [...rings.keys()].sort((a, b) => a - b)) {
    const ring = rings.get(ringLevel)!;
    if (ringLevel === 0) { positions.set(ring[0], { x: 0, y: 0 }); continue; }
    // Far enough from the previous ring, and wide enough that neighbours on
    // the ring are `minDistance` apart (chord = 2 r sin(pi / n)).
    const chordRadius = ring.length > 1 ? minDistance / (2 * Math.sin(Math.PI / ring.length)) : 0;
    radius = Math.max(radius + layerDistance, chordRadius);
    ring.forEach((id, i) => {
      const angle = -Math.PI / 2 + (2 * Math.PI * i) / ring.length;
      positions.set(id, { x: radius * Math.cos(angle), y: radius * Math.sin(angle) });
    });
  }
  const { isolatedPerRow, labelWidth } = { ...DEFAULTS, ...options };
  // The rows start one node distance under the lowest ring node, not a ring
  // distance under the outer radius: no dead band between rings and rows.
  let lowest = 0;
  for (const p of positions.values()) lowest = Math.max(lowest, p.y);
  if (options.aspect) {
    // The handover components the rings do not reach get a force drawing
    // each, packed in shelves with the rings, the lone agents in a
    // grid beside or under them.
    const colGap = Math.max(minDistance, labelWidth);
    const unreachedSet = new Set(unreached);
    const rest = clean.filter((e) => unreachedSet.has(e.sourceId) && unreachedSet.has(e.targetId));
    const comps = connectedComponents(unreached, rest);
    const inComp = new Set(comps.flat());
    const blocks = [positions, ...comps.map((comp) => {
      const own = new Set(comp);
      return forceComponent(
        comp,
        rest.filter((e) => own.has(e.sourceId)),
        minDistance,
        options.linkDistance ?? minDistance * 1.4,
        options.charge ?? 1,
        labelBoxAcross(minDistance, labelWidth),
      );
    })];
    return packForCanvas(blocks, unreached.filter((id) => !inComp.has(id)), colGap, minDistance, loneCellWidth(minDistance, labelWidth), options.aspect, options.groups);
  }
  return isolatedRows(unreached, positions, 0, lowest + minDistance, Math.max(minDistance, labelWidth), minDistance, isolatedPerRow);
}

/** A small seeded generator (mulberry32): the force layout starts from the same place every time. */
function seeded(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * The force drawing: Fruchterman-Reingold run on each connected component of
 * the agents that have a handover, the components packed side by side in
 * shelves (largest first), and the agents with no handover in rows under
 * them. Run on the whole graph, repulsion pushes the lone agents and the
 * small components far out, and the drawing, scaled into the canvas to fit
 * them, squeezes the main component together in its middle.
 */
function forceLayout(
  ids: readonly string[],
  edges: readonly LayoutEdge[],
  options: LayoutOptions = {},
): Map<string, Point> {
  const { minDistance, isolatedPerRow, labelWidth } = { ...DEFAULTS, ...options };
  const allIds = sortedUnique(ids);
  const clean = cleanEdges(new Set(allIds), edges);
  const adj = new Map<string, string[]>();
  for (const e of clean) {
    (adj.get(e.sourceId) ?? adj.set(e.sourceId, []).get(e.sourceId)!).push(e.targetId);
    (adj.get(e.targetId) ?? adj.set(e.targetId, []).get(e.targetId)!).push(e.sourceId);
  }
  const isolated = allIds.filter((id) => !adj.has(id));
  const positions = new Map<string, Point>();
  if (allIds.length === 0) return positions;
  // Hold a label's room across, as the hierarchy rows do.
  const rowGap = Math.max(minDistance, labelWidth);
  const components = connectedComponents(allIds, clean);
  const blocks = components.map((comp) => {
    const own = new Set(comp);
    return forceComponent(
      comp,
      clean.filter((e) => own.has(e.sourceId)),
      minDistance,
      options.linkDistance ?? minDistance * 1.4,
      options.charge ?? 1,
      labelBoxAcross(minDistance, labelWidth),
    );
  });
  if (options.aspect) return packForCanvas(blocks, isolated, rowGap, minDistance, loneCellWidth(minDistance, labelWidth), options.aspect, options.groups);
  const { right, bottom } = packShelves(blocks, positions, rowGap, minDistance);
  const top = components.length > 0 ? bottom + minDistance * 1.5 : 0;
  return isolatedRows(isolated, positions, right / 2, top, rowGap, minDistance, isolatedPerRow);
}

/**
 * The connected components of two nodes or more, each in id order, the
 * largest first (ties by smallest id); nodes with no edge are left out.
 */
export function connectedComponents(ids: readonly string[], edges: readonly LayoutEdge[]): string[][] {
  const known = new Set(ids);
  const adj = new Map<string, string[]>();
  for (const e of edges) {
    if (!known.has(e.sourceId) || !known.has(e.targetId) || e.sourceId === e.targetId) continue;
    (adj.get(e.sourceId) ?? adj.set(e.sourceId, []).get(e.sourceId)!).push(e.targetId);
    (adj.get(e.targetId) ?? adj.set(e.targetId, []).get(e.targetId)!).push(e.sourceId);
  }
  const seen = new Set<string>();
  const components: string[][] = [];
  for (const id of sortedUnique(ids)) {
    if (!adj.has(id) || seen.has(id)) continue;
    const comp = [id];
    seen.add(id);
    for (let i = 0; i < comp.length; i++) {
      for (const m of adj.get(comp[i])!) if (!seen.has(m)) { seen.add(m); comp.push(m); }
    }
    components.push(comp.sort());
  }
  return components.sort((a, b) => b.length - a.length || a[0].localeCompare(b[0]));
}

/**
 * Drawings (`blocks`, each in its own coordinates) packed left to right in
 * shelves, in the order given, into `positions`: `colGap` between two blocks
 * of a shelf, one and a half `rowGap` between two shelves. A shelf is
 * as wide as the widest block or, with a canvas `aspect`, the width that
 * gives the packed drawing the canvas's proportions (a near-square without).
 * A block in `margins` keeps that much more room on each side (a fleet's
 * grid), against every neighbour: two neighbours stand at
 * least the gap plus twice the larger of their margins apart, so a fleet grid
 * is as far from a handover drawing as from another fleet grid. Returns the right and bottom edges of the packing.
 */
export function packShelves(
  blocks: readonly Map<string, Point>[],
  positions: Map<string, Point>,
  colGap: number,
  rowGap: number,
  aspect?: number,
  width?: number,
  margins?: ReadonlyMap<Map<string, Point>, number>,
): { right: number; bottom: number } {
  const drawn = blocks.filter((b) => b.size > 0).map((local) => {
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const p of local.values()) {
      x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x); y0 = Math.min(y0, p.y); y1 = Math.max(y1, p.y);
    }
    const m = margins?.get(local) ?? 0;
    return { local, m, x0: x0 - m, y0: y0 - m, w: x1 - x0 + 2 * m, h: y1 - y0 + 2 * m };
  });
  const area = drawn.reduce((s, d) => s + (d.w + colGap) * (d.h + rowGap), 0);
  const widest = drawn.reduce((m, d) => Math.max(m, d.w), 0);
  const shelfWidth = Math.max(widest, width ?? (aspect ? Math.sqrt(area * aspect) : Math.sqrt(area) * 1.6));
  // The room two neighbours with margins `a` and `b` add to the plain gap so
  // they stand the larger margin twice apart.
  const extra = (a: number, b: number) => Math.max(0, 2 * Math.max(a, b) - a - b);
  // Shelves first (left to right, wrapped at the shelf width), then rows.
  type Placed = (typeof drawn)[number] & { x: number };
  const shelves: Placed[][] = [];
  let cx = 0, right = 0;
  let prev: Placed | null = null;
  for (const d of drawn) {
    const lead = prev ? extra(prev.m, d.m) : 0;
    if (prev && cx + lead + d.w > shelfWidth) { shelves.push([]); cx = 0; prev = null; }
    if (shelves.length === 0) shelves.push([]);
    const x: number = cx + (prev ? lead : 0);
    const placed: Placed = { ...d, x };
    shelves[shelves.length - 1].push(placed);
    right = Math.max(right, x + d.w);
    cx = x + d.w + colGap;
    prev = placed;
  }
  let cy = 0, bottom = 0;
  let above: Placed[] | null = null;
  for (const shelf of shelves) {
    if (above) {
      const ms = (s: Placed[]) => s.map((d) => d.m);
      const all = [...ms(above), ...ms(shelf)];
      cy += Math.max(0, 2 * Math.max(...all) - Math.min(...ms(above)) - Math.min(...ms(shelf)));
    }
    let shelfH = 0;
    for (const d of shelf) {
      for (const [id, p] of d.local) positions.set(id, { x: d.x + p.x - d.x0, y: cy + p.y - d.y0 });
      shelfH = Math.max(shelfH, d.h);
    }
    bottom = cy + shelfH;
    cy = bottom + rowGap * 1.5;
    above = shelf;
  }
  return { right, bottom };
}

// Shelf widths tried by `packForCanvas`, as multiples of the area-based width.
const SHELF_WIDTH_FACTORS = [0.6, 0.7, 0.8, 0.9, 1, 1.15, 1.3, 1.5, 1.75, 2, 2.4, 2.8, 3.4, 4];

/**
 * The handover drawings (`blocks`) packed in shelves and the agents with no
 * handover (`isolated`) in a grid beside or under them, at the shelf width
 * that lets the whole drawing be scaled largest into a canvas of width /
 * height `aspect`. The area-based width alone ignored the lone-agent
 * grid: at 93 agents the drawing came out 1.46 wide for a 2.33 canvas, the fit
 * was bound by its height and nodes sat 24 px apart on screen.
 *
 * With `groups`, the lone agents of a group of two or more
 * are a grid block of their own (`groupBlocks`), packed with the handover
 * drawings, so a fleet's agents sit together. Each such grid keeps half a
 * cell and half a row of extra room around it: two fleet grids stand at
 * least a cell and a row apart, a row more than two columns of one grid,
 * so the eye reads them as two groups.
 */
export function packForCanvas(
  blocks: readonly Map<string, Point>[],
  isolatedIds: readonly string[],
  colGap: number,
  rowGap: number,
  cellWidth: number,
  aspect: number,
  groups?: ReadonlyMap<string, string>,
  blockMargins?: ReadonlyMap<Map<string, Point>, number>,
): Map<string, Point> {
  const grouped = groups ? groupBlocks(isolatedIds, groups, cellWidth, rowGap) : { blocks: [], rest: isolatedIds };
  const isolated = grouped.rest;
  const drawn = [...blocks, ...grouped.blocks].filter((b) => b.size > 0);
  // A block may ask for room on each side (the org chart's return lanes).
  const margins = new Map<Map<string, Point>, number>([
    ...(blockMargins ?? new Map<Map<string, Point>, number>()),
    ...grouped.blocks.map((b) => [b, (cellWidth + rowGap) / 2] as [Map<string, Point>, number]),
  ]);
  const pack = (width?: number) => {
    const positions = new Map<string, Point>();
    packShelves(drawn, positions, colGap, rowGap, aspect, width, margins);
    return isolatedCluster(isolated, positions, cellWidth, rowGap, aspect);
  };
  const base = pack();
  if (drawn.length < 2) return base;
  let area = 0;
  for (const b of drawn) {
    const { w, h } = extent(b.values());
    const m = 2 * (margins.get(b) ?? 0);
    area += (w + m + colGap) * (h + m + rowGap);
  }
  const scaleOf = (p: Map<string, Point>) => {
    const { w, h } = extent(p.values());
    return Math.min(aspect / (w + rowGap), 1 / (h + rowGap));
  };
  let best = base;
  let bestScale = scaleOf(base);
  for (const f of SHELF_WIDTH_FACTORS) {
    const candidate = pack(Math.sqrt(area * aspect) * f);
    const s = scaleOf(candidate);
    if (s > bestScale + 1e-12) { best = candidate; bestScale = s; }
  }
  return best;
}

/**
 * The ids of each group of two or more, by group key, as near-square grids of
 * `cellWidth` x `rowGap` cells (each in its own coordinates), in group key
 * order; `rest` keeps the ids with no group or alone in theirs, in the order
 * given.
 */
export function groupBlocks(
  ids: readonly string[],
  groups: ReadonlyMap<string, string>,
  cellWidth: number,
  rowGap: number,
): { blocks: Map<string, Point>[]; rest: string[] } {
  const byGroup = new Map<string, string[]>();
  for (const id of ids) {
    const key = groups.get(id);
    if (key === undefined) continue;
    (byGroup.get(key) ?? byGroup.set(key, []).get(key)!).push(id);
  }
  const blocks: Map<string, Point>[] = [];
  const placed = new Set<string>();
  for (const key of [...byGroup.keys()].sort()) {
    const members = [...byGroup.get(key)!].sort();
    if (members.length < 2) continue;
    const cols = Math.ceil(Math.sqrt(members.length));
    blocks.push(new Map(members.map((id, i) => [id, { x: (i % cols) * cellWidth, y: Math.floor(i / cols) * rowGap }])));
    for (const id of members) placed.add(id);
  }
  return { blocks, rest: ids.filter((id) => !placed.has(id)) };
}

function extent(points: Iterable<Point>): { w: number; h: number } {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const p of points) {
    x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x); y0 = Math.min(y0, p.y); y1 = Math.max(y1, p.y);
  }
  return x0 === Infinity ? { w: 0, h: 0 } : { w: x1 - x0, h: y1 - y0 };
}

// ─── Org chart ─────────────────────────────────────────────────────

/** A parent with more children than this folds its leaves into columns. */
const ORG_FOLD_AT = 6;
// Canvas proportions the org chart packs for when the caller gives none.
const ORG_DEFAULT_ASPECT = 1400 / 600;

/** Spacing of the org chart, in layout units, shared by the layout and its edge router. */
export interface OrgChartMetrics {
  /** Room across one card and its name: two cards on a row are at least this far apart. */
  unit: number;
  /** Vertical pitch: every card sits on a multiple of it, a level is two pitches. */
  rowPitch: number;
  levelHeight: number;
  /** Room left of each folded column for its vertical spine. */
  spineRoom: number;
  /** Width of the band right of a component that holds its return lanes. */
  laneRoom: number;
}

export function orgChartMetrics(options: LayoutOptions = {}): OrgChartMetrics {
  const { minDistance, labelWidth } = { ...DEFAULTS, ...options };
  const unit = Math.max(minDistance, labelWidth);
  const layerDistance = Math.max(options.layerDistance ?? DEFAULTS.layerDistance, minDistance);
  const rowPitch = Math.max(minDistance, Math.ceil(layerDistance / 2));
  return { unit, rowPitch, levelHeight: 2 * rowPitch, spineRoom: Math.round(unit * 0.3), laneRoom: unit };
}

/**
 * Width of the band right of a component holding its `lanes` return lanes
 * (layout units): the lane room, widened by an eighth of a unit per lane so
 * that many lanes stay an arrowhead's width apart at the 1024 fit instead of
 * running beside the face drops of the rightmost cards.
 */
function orgLaneBand(m: OrgChartMetrics, lanes: number): number {
  return Math.max(m.laneRoom, ((lanes + 2) * m.unit) / 8);
}

/** The spanning tree the org chart draws, with the parents whose leaves fold into columns. */
export interface OrgChartTree {
  parent: Map<string, string>;
  children: Map<string, string[]>;
  roots: string[];
  folded: Set<string>;
}

/**
 * The org chart's tree: a breadth-first walk along the handovers as declared,
 * from the roots first (no inbound handover), then from the agent that hands
 * over the most more than it receives (ties by id), so a fleet's orchestrator
 * heads its chart even when its desks hand back to it. Each node hangs under
 * the first parent that reaches it at the smallest depth; the walk never
 * revisits a node, so the tree has no cycle. Every other edge (a hand-back,
 * a cross edge) is drawn as a lane, not a branch.
 */
export function orgChartTree(ids: readonly string[], edges: readonly LayoutEdge[]): OrgChartTree {
  const nodeIds = sortedUnique(ids);
  const out = new Map<string, string[]>();
  const inbound = new Map<string, number>();
  for (const id of nodeIds) { out.set(id, []); inbound.set(id, 0); }
  for (const e of cleanEdges(new Set(nodeIds), edges)) {
    const targets = out.get(e.sourceId)!;
    if (targets.includes(e.targetId)) continue;
    targets.push(e.targetId);
    inbound.set(e.targetId, inbound.get(e.targetId)! + 1);
  }
  for (const targets of out.values()) targets.sort();
  const balance = (id: string) => out.get(id)!.length - inbound.get(id)!;
  const rootOrder = [...nodeIds].sort((a, b) =>
    Number(inbound.get(b) === 0) - Number(inbound.get(a) === 0) || balance(b) - balance(a) || a.localeCompare(b));
  const parent = new Map<string, string>();
  const children = new Map<string, string[]>(nodeIds.map((id) => [id, []]));
  const roots: string[] = [];
  const seen = new Set<string>();
  const walk = (root: string) => {
    seen.add(root);
    roots.push(root);
    const queue = [root];
    for (let i = 0; i < queue.length; i++) {
      for (const t of out.get(queue[i])!) {
        if (seen.has(t)) continue;
        seen.add(t);
        parent.set(t, queue[i]);
        children.get(queue[i])!.push(t);
        queue.push(t);
      }
    }
  };
  for (const id of rootOrder) if (!seen.has(id)) walk(id);
  const folded = new Set<string>();
  for (const id of nodeIds) {
    const kids = children.get(id)!;
    const leaves = kids.filter((k) => children.get(k)!.length === 0);
    if (kids.length > ORG_FOLD_AT && leaves.length >= 2) folded.add(id);
  }
  return { parent, children, roots, folded };
}

interface OrgBlock {
  pos: Map<string, Point>;
  left: number;
  right: number;
}

function shiftBlock(block: OrgBlock, dx: number, dy: number): OrgBlock {
  const pos = new Map<string, Point>();
  for (const [id, p] of block.pos) pos.set(id, { x: p.x + dx, y: p.y + dy });
  return { pos, left: block.left + dx, right: block.right + dx };
}

/** Blocks side by side, extent against extent, the row centred on x = 0. */
function rowOfBlocks(blocks: readonly OrgBlock[]): OrgBlock {
  const pos = new Map<string, Point>();
  let cursor = 0;
  const placed = blocks.map((b) => {
    const s = shiftBlock(b, cursor - b.left, 0);
    cursor = s.right;
    return s;
  });
  const half = cursor / 2;
  for (const b of placed) for (const [id, p] of b.pos) pos.set(id, { x: p.x - half, y: p.y });
  return { pos, left: -half, right: half };
}

/**
 * A folded parent's leaves, column after column (each column filled top
 * down), a spine room left of each column for the vertical line its cards
 * hang from; the first row sits one level under the parent. Rows are a
 * level apart, not a row pitch: a leaf's name then has the room under its
 * card before the next card of its column, and the chart takes more of a wide
 * canvas's height. Every other column stands
 * one row pitch lower, so two leaves sharing a row are two columns apart: a
 * name's room is the gap across to the nearest card on its row, and one
 * column pitch is too narrow for a name at the 1024 fit.
 */
function leafGrid(leaves: readonly string[], m: OrgChartMetrics): OrgBlock {
  const rows = Math.ceil(Math.sqrt(leaves.length));
  const cols = Math.ceil(leaves.length / rows);
  const pitch = m.unit + m.spineRoom;
  const pos = new Map<string, Point>();
  leaves.forEach((id, i) => {
    const col = Math.floor(i / rows);
    pos.set(id, {
      x: col * pitch + m.spineRoom + m.unit / 2,
      y: m.levelHeight + (col % 2) * m.rowPitch + (i % rows) * m.levelHeight,
    });
  });
  return { pos, left: 0, right: cols * pitch };
}

function orgSubtree(id: string, tree: OrgChartTree, m: OrgChartMetrics): OrgBlock {
  const kids = tree.children.get(id) ?? [];
  if (kids.length === 0) return { pos: new Map([[id, { x: 0, y: 0 }]]), left: -m.unit / 2, right: m.unit / 2 };
  const isLeaf = (k: string) => (tree.children.get(k) ?? []).length === 0;
  const leaves = tree.folded.has(id) ? kids.filter(isLeaf) : [];
  const folded = new Set(leaves);
  const parts = kids.filter((k) => !folded.has(k)).map((k) => shiftBlock(orgSubtree(k, tree, m), 0, m.levelHeight));
  if (leaves.length > 0) parts.splice(Math.floor(parts.length / 2), 0, leafGrid(leaves, m));
  const row = rowOfBlocks(parts);
  row.pos.set(id, { x: 0, y: 0 });
  return { pos: row.pos, left: Math.min(row.left, -m.unit / 2), right: Math.max(row.right, m.unit / 2) };
}

/** The edges of each component that are drawn as return lanes (neither a branch nor its reverse, across rows). */
function laneEdges(tree: OrgChartTree, clean: readonly LayoutEdge[], rowOf: (id: string) => number): LayoutEdge[] {
  return clean.filter((e) =>
    tree.parent.get(e.targetId) !== e.sourceId
    && tree.parent.get(e.sourceId) !== e.targetId
    && rowOf(e.sourceId) !== rowOf(e.targetId));
}

/**
 * Org chart: each handover component a top-down tree (`orgChartTree`),
 * one level per depth, children centred under their parent, a parent with
 * more than six children folding its leaves into columns. Subtrees never
 * share an x range, every card sits on a multiple of the row pitch, so a
 * branch, a bus or a lane drawn between rows never crosses a card. The
 * components keep a band on their side for their return lanes and are packed
 * for the canvas like the other layouts, the agents with no handover in a grid.
 */
export function orgChartLayout(
  ids: readonly string[],
  edges: readonly LayoutEdge[],
  options: LayoutOptions = {},
): Map<string, Point> {
  const { minDistance, labelWidth } = { ...DEFAULTS, ...options };
  const m = orgChartMetrics(options);
  const nodeIds = sortedUnique(ids);
  const clean = cleanEdges(new Set(nodeIds), edges);
  const linked = new Set<string>();
  for (const e of clean) { linked.add(e.sourceId); linked.add(e.targetId); }
  const connected = nodeIds.filter((id) => linked.has(id));
  const isolated = nodeIds.filter((id) => !linked.has(id));
  const tree = orgChartTree(connected, clean);
  const margins = new Map<Map<string, Point>, number>();
  const blocks = connectedComponents(connected, clean).map((comp) => {
    const own = new Set(comp);
    const block = rowOfBlocks(tree.roots.filter((r) => own.has(r)).map((r) => orgSubtree(r, tree, m))).pos;
    const lanes = laneEdges(tree, clean.filter((e) => own.has(e.sourceId)), (id) => block.get(id)!.y);
    if (lanes.length > 0) margins.set(block, Math.max(orgLaneBand(m, lanes.length), m.rowPitch));
    return block;
  });
  return packForCanvas(
    blocks, isolated, m.unit, m.rowPitch, loneCellWidth(minDistance, labelWidth),
    options.aspect ?? ORG_DEFAULT_ASPECT, options.groups, margins,
  );
}

/** A node as drawn on the canvas, for the org chart's edge router. */
export interface OrgRouteNode {
  x: number;
  y: number;
  r: number;
}

/** One orthogonal edge of the org chart, in canvas units. */
export interface OrgRoute {
  points: Point[];
  path: string;
  /** Halfway along the polyline: where its label sits. */
  mid: Point;
  /** Direction the edge leaves its source (radians, y down). */
  sourceAngle: number;
  /** Direction from the target back along the edge's last segment (radians, y down). */
  targetAngle: number;
}

const r2 = (v: number) => Math.round(v * 100) / 100;

/** Half the chord of a card of radius `node.r` at `off` from its centre: where an end at that offset meets the ring. */
const chordHalf = (node: OrgRouteNode, off: number) => Math.sqrt(Math.max(0, node.r * node.r - off * off));

/** One edge end on a side of a card: its offset from the centre, and its flank level (-1 on the face). */
interface SideSlot {
  off: number;
  level: number;
}

/**
 * Offsets from a card's centre of `n` edge ends on one side of it, innermost
 * first: spread over 0.25r to 0.85r as long as two
 * ends and the trunk stay `gap` apart (an arrowhead's width plus a stroke, so
 * no arrowhead lands on a neighbour), otherwise `gap` apart from the trunk
 * outwards; an end that no longer fits on the face meets the card on its
 * flank (level 0 the highest), past the ring, so no segment crosses the card.
 * The first flank end is `flankRoom` (the arrow gap and an arrowhead) plus a
 * gap out from the ring, so a vertical run beside it never touches the
 * arrowhead of the flank end above it.
 */
function sideSlots(n: number, r: number, gap: number, flankRoom = 0, maxOff = Infinity): SideSlot[] {
  const spread = Array.from({ length: n }, (_, i) => r * (0.25 + (0.6 * (i + 1)) / (n + 1)));
  if (n === 0 || (spread[0] >= gap && (n < 2 || spread[1] - spread[0] >= gap))) {
    return spread.map((off) => ({ off, level: -1 }));
  }
  const first = Math.max(0.25 * r, gap);
  const step = n > 1 ? Math.max(gap, (0.85 * r - first) / (n - 1)) : 0;
  const slots: SideSlot[] = [];
  let level = 0;
  let last = -Infinity;
  for (let i = 0; i < n; i++) {
    let off = Math.max(first + i * step, last + gap);
    if (off > 0.9 * r) {
      off = Math.max(off, r + flankRoom + gap);
      slots.push({ off, level: level++ });
    } else {
      slots.push({ off, level: -1 });
    }
    last = off;
  }
  // Flank risers carry no arrowhead (it sits on the row, by the ring), so a
  // side whose risers would pass `maxOff` (half way to the next card on the
  // row) packs them closer, in the same order, rather than run beside that
  // card's face drops, never closer than half a
  // gap, which reads as one doubled line.
  const flank = slots.filter((slot) => slot.level >= 0);
  if (flank.length > 1 && flank[flank.length - 1].off > maxOff) {
    const a = flank[0].off;
    const b = Math.max(maxOff, a + (flank.length - 1) * (gap / 2));
    flank.forEach((slot, i) => { slot.off = a + ((b - a) * i) / (flank.length - 1); });
  }
  return slots;
}

/**
 * The points of an edge end on a card, from the ring out to the point at
 * `off` across from the centre where the route goes on vertically. `side` is
 * -1 for the card's top, 1 for its bottom. On the face, one point on the ring
 * (`ringGap` off it); on the flank, a horizontal run at `flankDy` from the
 * centre, from the ring out to `off`. A target end is the same points
 * reversed, with `ringGap` the arrow gap. A flank end stops `clear` across
 * from the centre at the latest, outside the face ends its row would cross.
 */
function cardEnd(
  node: OrgRouteNode, slot: SideSlot, sign: number, side: -1 | 1, ringGap: number, flankDy: number, clear = 0,
): Point[] {
  const off = sign * slot.off;
  if (slot.level < 0) return [{ x: node.x + off, y: node.y + side * (chordHalf(node, off) + ringGap) }];
  const y = node.y + flankDy;
  return [{ x: node.x + sign * Math.max(chordHalf(node, flankDy) + ringGap, clear), y }, { x: node.x + off, y }];
}

function polylineRoute(raw: Point[]): OrgRoute {
  const points = raw
    .map((p) => ({ x: r2(p.x), y: r2(p.y) }))
    .filter((p, i, all) => i === 0 || p.x !== all[i - 1].x || p.y !== all[i - 1].y);
  const lengths = points.slice(1).map((p, i) => Math.hypot(p.x - points[i].x, p.y - points[i].y));
  const half = lengths.reduce((s, l) => s + l, 0) / 2;
  let mid = points[0];
  let walked = 0;
  for (let i = 0; i < lengths.length; i++) {
    if (walked + lengths[i] >= half) {
      const t = lengths[i] > 0 ? (half - walked) / lengths[i] : 0;
      mid = { x: points[i].x + t * (points[i + 1].x - points[i].x), y: points[i].y + t * (points[i + 1].y - points[i].y) };
      break;
    }
    walked += lengths[i];
  }
  const n = points.length;
  return {
    points,
    path: points.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x},${p.y}`).join(" "),
    mid,
    sourceAngle: Math.atan2(points[1].y - points[0].y, points[1].x - points[0].x),
    targetAngle: Math.atan2(points[n - 2].y - points[n - 1].y, points[n - 2].x - points[n - 1].x),
  };
}

/**
 * The orthogonal edges of an org chart drawn at `nodes` (canvas units, the
 * layout scaled by `scale`), one per edge in the order given, null for a self
 * loop or an unknown node. A branch leaves its parent's bottom, runs
 * along the parent's bus half a level down and drops onto the child's top
 * (a folded leaf: down its column's spine and into its left side). The
 * reverse of a branch is a parallel lane, offset on both cards and on a bus
 * of its own, arrow at the parent. Any other edge leaves upwards into the
 * free channel above its row, runs out to a lane right of its component and
 * back into the channel above its target: no segment crosses a card. Each
 * path ends `arrowGap` off the target's ring, where the arrowhead's tip is.
 *
 * Ports are allocated per card side across all of that card's edges, not per
 * pair: the branches of a parent share its trunk (the centre
 * of its bottom and of each child's top), every other edge end takes its own
 * x on the card, the reverse lanes outside the trunk, and every reverse bus
 * and row channel its own y. Two distinct edges never run on the same segment
 * except siblings on their parent's bus, and a folded column's branches and
 * hand-backs on that column's spine (one riser per trunk).
 * With `portGap` (canvas units: the arrowhead's width as drawn plus a stroke)
 * two ends on a card side are never closer than it, so an arrowhead never
 * lands on another edge; the ends that no longer fit
 * on the face meet the card on its flank.
 */
export function orgChartRoutes(
  ids: readonly string[],
  edges: readonly LayoutEdge[],
  nodes: ReadonlyMap<string, OrgRouteNode>,
  metrics: OrgChartMetrics,
  scale: number,
  arrowGap: number,
  portGap = 0,
): (OrgRoute | null)[] {
  const nodeIds = sortedUnique(ids).filter((id) => nodes.has(id));
  const clean = cleanEdges(new Set(nodeIds), edges);
  const linked = nodeIds.filter((id) => clean.some((e) => e.sourceId === id || e.targetId === id));
  const tree = orgChartTree(linked, clean);
  const pairs = new Set(clean.map((e) => `${e.sourceId}\u0000${e.targetId}`));
  const pitch = metrics.rowPitch * scale;
  const spineOffset = (metrics.unit / 2 + metrics.spineRoom / 2) * scale;
  const sameRow = (a: OrgRouteNode, b: OrgRouteNode) => Math.abs(a.y - b.y) < pitch / 4;
  const isFoldedLeaf = (parent: string, child: string) =>
    tree.folded.has(parent) && (tree.children.get(child) ?? []).length === 0;
  const keyOf = (a: string, b: string) => `${a}\u0000${b}`;
  // Room an arrowhead takes off a ring: the arrow gap and its length (about
  // 1.5 port gaps, the gap being its width plus a stroke).
  const flankRoom = arrowGap + 1.5 * portGap;
  // Lanes: one x per lane edge of a component, right of its rightmost card
  // and of every end on that card's right (a lane
  // packed 3.6 units off the rightmost card ran beside its face drop), and a
  // port gap apart at least.
  const laneX = new Map<string, number>();
  const placeLanes = (reachRight: (id: string) => number) => {
    for (const comp of connectedComponents(linked, clean)) {
      const own = new Set(comp);
      const maxX = Math.max(...comp.map((id) => nodes.get(id)!.x));
      const right = Math.max(...comp.map((id) => nodes.get(id)!.x + reachRight(id)));
      const lanes = laneEdges(tree, clean.filter((e) => own.has(e.sourceId)), (id) => Math.round(nodes.get(id)!.y / Math.max(1e-9, pitch / 4)));
      // Never past the lane band the layout keeps right of the component
      // (the next block starts there): when the ends' reach leaves too little
      // of it, the lanes start further in, a port gap apart.
      const bandEnd = maxX + orgLaneBand(metrics, lanes.length) * scale;
      // Never inside the face-drop arrowheads of the rightmost cards: half an
      // arrowhead's width and half a port gap past the outermost face end
      // (a lane 0.67 px from a drop's wing at 1024).
      const faceClear = portGap > 0
        ? Math.max(...comp.map((id) => nodes.get(id)!.x + 0.9 * nodes.get(id)!.r)) + portGap - 0.6 - maxX
        : 0;
      // Never inside the reach of the flank ends either: a lane started left
      // of `right` crosses the stub of an edge entering a card's right flank,
      // past its arrowhead (1.12 px at 1024 when the
      // packing gave the band less than the ends' reach).
      const start = Math.max(maxX + faceClear, portGap > 0 ? right : Math.min(right, bandEnd - (lanes.length + 1) * portGap));
      const gap = Math.max((bandEnd - start) / (lanes.length + 1), portGap / 2);
      lanes.forEach((e, j) => laneX.set(keyOf(e.sourceId, e.targetId), start + (j + 1) * gap));
    }
  };
  const laneOf = (e: LayoutEdge) => {
    const s = nodes.get(e.sourceId)!, t = nodes.get(e.targetId)!;
    return laneX.get(keyOf(e.sourceId, e.targetId)) ?? Math.max(s.x, t.x) + (metrics.laneRoom * scale) / 2;
  };
  // How far out a card's flank risers may reach on one side: half way to the
  // next card on its row, less half a port gap.
  const roomBeside = (id: string, sign: number) => {
    const n = nodes.get(id)!;
    let room = Infinity;
    for (const other of nodeIds) {
      const o = nodes.get(other)!;
      const dx = sign * (o.x - n.x);
      if (other !== id && dx > 0 && sameRow(n, o)) room = Math.min(room, dx / 2 - portGap / 2);
    }
    return room;
  };
  // The side of the parent its child sits on (a child right under it counts as left).
  const sideOf = (parent: OrgRouteNode, child: OrgRouteNode) => Math.sign(child.x - parent.x) || -1;
  // The nearest card on a card's row, on one side.
  const nextOnRow = (id: string, sign: number) => {
    const n = nodes.get(id)!;
    let best: string | null = null, bestDx = Infinity;
    for (const other of nodeIds) {
      const o = nodes.get(other)!;
      const dx = sign * (o.x - n.x);
      if (other !== id && dx > 0 && sameRow(n, o) && dx < bestDx) { best = other; bestDx = dx; }
    }
    return best;
  };
  const kindOf = (e: LayoutEdge): "branch" | "reverse" | "direct" | "row" | "lane" => {
    if (tree.parent.get(e.targetId) === e.sourceId) return "branch";
    if (tree.parent.get(e.sourceId) === e.targetId) return "reverse";
    const s = nodes.get(e.sourceId)!, t = nodes.get(e.targetId)!;
    if (!sameRow(s, t)) return "lane";
    // Neighbours on a row, nothing between them: straight across, flank to
    // flank, with no riser and no channel.
    return nextOnRow(e.sourceId, Math.sign(t.x - s.x) || 1) === e.targetId ? "direct" : "row";
  };
  // Direct edges: per pair of neighbours, one centre row each, a port gap
  // apart around the row's centre line; that card side's other flank rows
  // are pushed above and below them.
  const directDy = new Map<string, number>();
  const directCount = new Map<string, number>();
  const directPairs = new Map<string, LayoutEdge[]>();
  // Per card side with direct rows: the distance to the neighbour they reach.
  const directGap = new Map<string, number>();
  for (const e of clean) {
    if (kindOf(e) !== "direct") continue;
    const pair = e.sourceId < e.targetId ? keyOf(e.sourceId, e.targetId) : keyOf(e.targetId, e.sourceId);
    directPairs.set(pair, [...(directPairs.get(pair) ?? []), e]);
  }
  for (const group of directPairs.values()) {
    group.sort((a, b) => a.sourceId.localeCompare(b.sourceId));
    const s = nodes.get(group[0].sourceId)!, t = nodes.get(group[0].targetId)!;
    // Without a port gap (no arrowhead size known), a reciprocal pair still
    // takes two rows, as the reverse lanes do.
    const sep = portGap > 0 ? portGap : Math.min(s.r, t.r) * 0.45;
    group.forEach((e, i) => directDy.set(keyOf(e.sourceId, e.targetId), (i - (group.length - 1) / 2) * sep));
    const sign = Math.sign(t.x - s.x) || 1;
    directCount.set(keyOf(group[0].sourceId, String(sign)), group.length);
    directCount.set(keyOf(group[0].targetId, String(-sign)), group.length);
    directGap.set(keyOf(group[0].sourceId, String(sign)), Math.abs(t.x - s.x));
    directGap.set(keyOf(group[0].targetId, String(-sign)), Math.abs(t.x - s.x));
  }
  // Reverse lanes: per parent and side, the nearest child takes the innermost
  // port on the parent's bottom and the lowest bus, the farthest the outermost
  // port and the highest bus, so the hand-backs nest and never share a segment.
  const reverse = new Map<string, { dx: number; slot: SideSlot; sign: number; bus: number }>();
  const reverseGroups = new Map<string, LayoutEdge[]>();
  for (const e of clean) {
    if (kindOf(e) !== "reverse") continue;
    const group = keyOf(e.targetId, String(sideOf(nodes.get(e.targetId)!, nodes.get(e.sourceId)!)));
    reverseGroups.set(group, [...(reverseGroups.get(group) ?? []), e]);
  }
  for (const group of reverseGroups.values()) {
    const t = nodes.get(group[0].targetId)!;
    const away = (e: LayoutEdge) => Math.abs(nodes.get(e.sourceId)!.x - t.x);
    group.sort((a, b) => away(a) - away(b) || a.sourceId.localeCompare(b.sourceId));
    const n = group.length;
    const slots = sideSlots(n, t.r, portGap, flankRoom, roomBeside(group[0].targetId, sideOf(t, nodes.get(group[0].sourceId)!)));
    group.forEach((e, j) => {
      const side = sideOf(t, nodes.get(e.sourceId)!);
      reverse.set(keyOf(e.sourceId, e.targetId), {
        dx: side * slots[j].off,
        slot: slots[j],
        sign: side,
        bus: t.y + pitch * (n === 1 ? 0.8 : 0.85 - (0.4 * j) / (n - 1)),
      });
    });
  }
  // The ends of an edge on a card's top, but a branch's (the centre): the
  // card, the port key, the side its first horizontal run heads to, and the
  // y of that run (read once the channels are placed).
  const topEndsOf = (e: LayoutEdge): { node: string; key: string; dir: number; y: () => number }[] => {
    const kind = kindOf(e);
    const s = nodes.get(e.sourceId)!, t = nodes.get(e.targetId)!;
    const key = keyOf(e.sourceId, e.targetId);
    if (kind === "reverse" && !isFoldedLeaf(e.targetId, e.sourceId)) {
      const r = reverse.get(key)!;
      return [{ node: e.sourceId, key: `${key}\u0000s`, dir: Math.sign(t.x + r.dx - s.x) || -1, y: () => r.bus }];
    }
    if (kind === "row") {
      const y = () => Math.min(s.y, t.y) - pitch * channel.get(`${key}\u0000st`)!;
      return [
        { node: e.sourceId, key: `${key}\u0000s`, dir: Math.sign(t.x - s.x) || 1, y },
        { node: e.targetId, key: `${key}\u0000t`, dir: Math.sign(s.x - t.x) || -1, y },
      ];
    }
    if (kind === "lane") {
      return [
        { node: e.sourceId, key: `${key}\u0000s`, dir: 1, y: () => s.y - pitch * channel.get(`${key}\u0000s`)! },
        { node: e.targetId, key: `${key}\u0000t`, dir: 1, y: () => t.y - pitch * channel.get(`${key}\u0000t`)! },
      ];
    }
    return [];
  };
  // Row channels: per row, each horizontal run above the cards takes the
  // lowest channel no overlapping run on that row holds.
  const LIFTS = [0.5, 0.62, 0.38, 0.74, 0.26, 0.56, 0.44, 0.68, 0.32];
  const liftAt = (level: number) => (level < LIFTS.length ? LIFTS[level] : 0.26 + 0.48 * ((level * 0.618) % 1));
  // A channel runs above the arrowheads that land on a card's top:
  // when the lowest lift would cross them, the lifts are
  // spread over the room between their bases and 0.9 pitch, in the same order.
  // It also runs above the flank rows of the cards on its row:
  // a card side with m flank rows reaches (m - 1) / 2 port gaps
  // above its centre, and an arrowhead there needs one more port gap. The
  // number of ends on a side does not depend on the channels, so it is known.
  const maxR = Math.max(0, ...nodeIds.map((id) => nodes.get(id)!.r));
  const flankRows = new Map<string, number>();
  const sideEnds = new Map<string, number>();
  for (const e of clean) {
    for (const end of topEndsOf(e)) sideEnds.set(keyOf(end.node, String(end.dir)), (sideEnds.get(keyOf(end.node, String(end.dir))) ?? 0) + 1);
  }
  for (const [k, n] of sideEnds) {
    const r = nodes.get(k.split("\u0000")[0])!.r;
    const [id, sign] = k.split("\u0000");
    flankRows.set(k, sideSlots(n, r, portGap, flankRoom, roomBeside(id, Number(sign))).filter((slot) => slot.level >= 0).length);
  }
  const bottomRows = new Map<string, number>();
  for (const [key, rv] of reverse) {
    if (rv.slot.level < 0) continue;
    const k = keyOf(key.split("\u0000")[1], String(rv.sign));
    bottomRows.set(k, Math.max(bottomRows.get(k) ?? 0, rv.slot.level + 1));
  }
  // A card side whose flank risers reach the arrowhead of a direct row to its
  // neighbour (the neighbour's ring, the arrow gap, an arrowhead and half a
  // port gap): its stack opens with every top end above the direct rows and
  // every bottom end below, so no riser crosses a direct row by its
  // arrowhead. Elsewhere the stack opens at
  // its middle, which keeps it lower under the row channels.
  const splitAtTop = new Set<string>();
  for (const [k, gapTo] of directGap) {
    const [id, sign] = k.split("\u0000");
    const r = nodes.get(id)!.r;
    const offs = sideSlots(sideEnds.get(k) ?? 0, r, portGap, flankRoom, roomBeside(id, Number(sign)))
      .filter((slot) => slot.level >= 0).map((slot) => slot.off);
    for (const [key, rv] of reverse) {
      if (rv.slot.level >= 0 && keyOf(key.split("\u0000")[1], String(rv.sign)) === k) offs.push(rv.slot.off);
    }
    if (offs.length > 0 && Math.max(...offs) > gapTo - r - arrowGap - 2 * portGap) splitAtTop.add(k);
  }
  let flankReach = 0;
  for (const k of new Set([...flankRows.keys(), ...bottomRows.keys()])) {
    const m = (flankRows.get(k) ?? 0) + (bottomRows.get(k) ?? 0);
    // With direct rows, the stack opens around them (see flankDy).
    const d = directCount.get(k) ?? 0;
    const split = splitAtTop.has(k) ? (flankRows.get(k) ?? 0) : Math.floor(m / 2);
    const above = d > 0 ? ((d - 1) / 2 + split) * portGap : ((m - 1) / 2) * portGap;
    if (m > 0) flankReach = Math.max(flankReach, above + portGap);
  }
  const clearFrac = Math.min(0.8, Math.max(maxR + flankRoom + portGap / 2, flankReach) / Math.max(1e-9, pitch));
  const clearOfHeads = (lift: number) =>
    portGap > 0 && clearFrac > 0.26 ? clearFrac + ((lift - 0.26) / 0.48) * (0.9 - clearFrac) : lift;
  // A card's reach to its right: its ring, the arrowheads on it, and the
  // furthest end on its right side, top and bottom.
  const rightOff = new Map<string, number>();
  for (const [k, n] of sideEnds) {
    const [id, sign] = k.split("\u0000");
    if (Number(sign) !== 1) continue;
    const slots = sideSlots(n, nodes.get(id)!.r, portGap, flankRoom, roomBeside(id, 1));
    rightOff.set(id, Math.max(rightOff.get(id) ?? 0, ...slots.map((slot) => slot.off)));
  }
  for (const [key, rv] of reverse) {
    const id = key.split("\u0000")[1];
    if (rv.sign === 1) rightOff.set(id, Math.max(rightOff.get(id) ?? 0, rv.slot.off));
  }
  placeLanes((id) => Math.max(nodes.get(id)!.r + flankRoom, rightOff.get(id) ?? 0) + (portGap > 0 ? portGap : 0));
  const runs: { key: string; row: number; lo: number; hi: number; anchor: string }[] = [];
  const rowOf = (n: OrgRouteNode) => Math.round(n.y / Math.max(1e-9, pitch / 4));
  // How far a card's ends may reach across: its ring, plus the flank ends.
  const wide = (n: OrgRouteNode) => n.r + flankRoom + 3 * portGap;
  for (const e of clean) {
    const kind = kindOf(e);
    const s = nodes.get(e.sourceId)!, t = nodes.get(e.targetId)!;
    const key = keyOf(e.sourceId, e.targetId);
    if (kind === "row") {
      runs.push({ key: `${key}\u0000st`, row: rowOf(s), lo: Math.min(s.x, t.x) - wide(s), hi: Math.max(s.x, t.x) + wide(t), anchor: key });
    } else if (kind === "lane") {
      const lane = laneOf(e);
      runs.push({ key: `${key}\u0000s`, row: rowOf(s), lo: s.x - wide(s), hi: lane, anchor: `${e.sourceId}\u0000out` });
      runs.push({ key: `${key}\u0000t`, row: rowOf(t), lo: t.x - wide(t), hi: lane, anchor: `${e.targetId}\u0000in` });
    }
  }
  runs.sort((a, b) => a.row - b.row || a.lo - b.lo || a.key.localeCompare(b.key));
  const channel = new Map<string, number>();
  const placed: { row: number; lo: number; hi: number; level: number; anchor: string }[] = [];
  // Lane runs into one card (or out of one card) share a channel when the
  // row is short of room: they meet the card as one bus, as siblings share
  // their parent's, instead of a stack of lines a pixel apart.
  // With room for a channel each, each keeps its own.
  const rowLevels = new Map<number, number>();
  for (const run of runs) {
    const overlapping = runs.filter((p) => p.row === run.row && p.lo <= run.hi && run.lo <= p.hi);
    rowLevels.set(run.row, Math.max(rowLevels.get(run.row) ?? 0, overlapping.length));
  }
  const roomy = (row: number) =>
    portGap <= 0 || clearFrac <= 0.26 || (rowLevels.get(row) ?? 1) * (portGap / 2) <= (0.9 - clearFrac) * pitch;
  for (const run of runs) {
    const overlapping = placed.filter((p) => p.row === run.row && p.lo <= run.hi && run.lo <= p.hi);
    const shared = roomy(run.row) ? undefined : placed.find((p) => p.row === run.row && p.anchor === run.anchor);
    const taken = new Set(overlapping.filter((p) => p.anchor !== run.anchor || !shared).map((p) => p.level));
    let level = shared && !taken.has(shared.level) ? shared.level : 0;
    if (!(shared && level === shared.level)) while (taken.has(level)) level++;
    placed.push({ row: run.row, lo: run.lo, hi: run.hi, level, anchor: run.anchor });
    channel.set(run.key, clearOfHeads(liftAt(level)));
  }
  // Squeezed above the arrowheads, a row's channels share the room between
  // the heads and 0.9 pitch: spread the levels that row uses evenly over it,
  // in the same order, instead of the nine lifts of the table, so two
  // channels are never a fraction of a pixel apart.
  if (portGap > 0 && clearFrac > 0.26) {
    const byRow = new Map<number, Set<number>>();
    for (const p of placed) byRow.set(p.row, (byRow.get(p.row) ?? new Set()).add(p.level));
    const spread = new Map<string, number>();
    for (const [row, levels] of byRow) {
      const order = [...levels].sort((a, b) => liftAt(a) - liftAt(b));
      order.forEach((level, i) => spread.set(`${row}\u0000${level}`, clearFrac + ((i + 0.5) / order.length) * (0.9 - clearFrac)));
    }
    placed.forEach((p, i) => channel.set(runs[i].key, spread.get(`${p.row}\u0000${p.level}`)!));
  }
  // Top ports: every edge end on a card's top but a branch's (the centre) gets
  // its own x, on the side its first horizontal run heads to; on a side, the
  // end whose run is highest sits innermost, so the runs never cross there.
  const ports = clean.flatMap((e) => topEndsOf(e).map((end) => ({ node: end.node, key: end.key, dir: end.dir, y: end.y() })));
  const portDx = new Map<string, number>();
  const portSlot = new Map<string, { slot: SideSlot; sign: number }>();
  const portGroups = new Map<string, typeof ports>();
  for (const p of ports) {
    const group = keyOf(p.node, String(p.dir));
    portGroups.set(group, [...(portGroups.get(group) ?? []), p]);
  }
  for (const group of portGroups.values()) {
    group.sort((a, b) => a.y - b.y || a.key.localeCompare(b.key));
    const slots = sideSlots(group.length, nodes.get(group[0].node)!.r, portGap, flankRoom, roomBeside(group[0].node, group[0].dir));
    group.forEach((p, i) => {
      portDx.set(p.key, p.dir * slots[i].off);
      portSlot.set(p.key, { slot: slots[i], sign: p.dir });
    });
  }
  // Flank rows: the top ends and the bottom ends that overflow onto one flank
  // of a card share its rows, a port gap apart around the centre, the top
  // ones above; on each, the outer end is nearer the centre so the runs nest.
  const flankCount = new Map<string, { top: number; bottom: number }>();
  const countFlank = (node: string, sign: number, slot: SideSlot, top: boolean) => {
    if (slot.level < 0) return;
    const k = keyOf(node, String(sign));
    const c = flankCount.get(k) ?? { top: 0, bottom: 0 };
    if (top) c.top = Math.max(c.top, slot.level + 1);
    else c.bottom = Math.max(c.bottom, slot.level + 1);
    flankCount.set(k, c);
  };
  for (const p of ports) countFlank(p.node, p.dir, portSlot.get(p.key)!.slot, true);
  for (const [key, rv] of reverse) countFlank(key.split("\u0000")[1], rv.sign, rv.slot, false);
  const flankDy = (node: string, sign: number, slot: SideSlot, side: -1 | 1) => {
    if (slot.level < 0) return 0;
    const c = flankCount.get(keyOf(node, String(sign)))!;
    const m = c.top + c.bottom;
    const j = side < 0 ? slot.level : m - 1 - slot.level;
    const d = directCount.get(keyOf(node, String(sign))) ?? 0;
    if (d === 0) return (j - (m - 1) / 2) * portGap;
    // The same stack, in the same order, opened for the direct rows on the
    // centre line, a port gap clear of them: at its middle, or after its top
    // ends on a side whose risers reach a direct arrowhead (splitAtTop).
    const k = splitAtTop.has(keyOf(node, String(sign))) ? c.top : Math.floor(m / 2);
    return j < k ? -((d - 1) / 2 + k - j) * portGap : ((d - 1) / 2 + j - k + 1) * portGap;
  };
  // Face ends per card side: a flank row that passes above a top face end or
  // below a bottom one (its trunk included) would cross that end's vertical
  // run next to its arrowhead, so the row stops a port gap outside the
  // outermost of them (cards with many flank ends).
  const faceEnds = new Map<string, { off: number; side: -1 | 1 }[]>();
  const addFace = (node: string, sign: number, off: number, side: -1 | 1) => {
    const k = keyOf(node, String(sign));
    faceEnds.set(k, [...(faceEnds.get(k) ?? []), { off, side }]);
  };
  for (const p of ports) {
    const { slot, sign } = portSlot.get(p.key)!;
    if (slot.level < 0) addFace(p.node, sign, slot.off, -1);
  }
  for (const [key, rv] of reverse) if (rv.slot.level < 0) addFace(key.split("\u0000")[1], rv.sign, rv.slot.off, 1);
  for (const id of linked) {
    const parent = tree.parent.get(id);
    if (parent !== undefined && !isFoldedLeaf(parent, id)) for (const sign of [-1, 1]) addFace(id, sign, 0, -1);
    if ((tree.children.get(id) ?? []).length > 0) for (const sign of [-1, 1]) addFace(id, sign, 0, 1);
  }
  const flankClear = (id: string, sign: number, dy: number) => {
    const node = nodes.get(id)!;
    let clear = 0;
    for (const f of faceEnds.get(keyOf(id, String(sign))) ?? []) {
      const depth = chordHalf(node, f.off);
      const crosses = f.side < 0 ? dy - portGap / 2 <= -depth : dy + portGap / 2 >= depth;
      if (crosses) clear = Math.max(clear, f.off + portGap);
    }
    return clear;
  };
  const flankEnd = (id: string, slot: SideSlot, sign: number, side: -1 | 1, ringGap: number) => {
    const dy = flankDy(id, sign, slot, side);
    return cardEnd(nodes.get(id)!, slot, sign, side, ringGap, dy, slot.level < 0 ? 0 : flankClear(id, sign, dy));
  };
  const topEnd = (id: string, portKey: string, ringGap: number) => {
    const { slot, sign } = portSlot.get(portKey)!;
    return flankEnd(id, slot, sign, -1, ringGap);
  };
  return edges.map(({ sourceId, targetId }) => {
    const s = nodes.get(sourceId);
    const t = nodes.get(targetId);
    if (!s || !t || sourceId === targetId) return null;
    const key = keyOf(sourceId, targetId);
    const reciprocal = pairs.has(keyOf(targetId, sourceId));
    const d = Math.min(s.r, t.r) * 0.45;
    const reach = (node: OrgRouteNode, off: number) => Math.sqrt(Math.max(0, node.r * node.r - off * off));
    const kind = kindOf({ sourceId, targetId });
    if (kind === "branch") {
      // A branch, parent to child: down the parent's trunk to its bus half a level down.
      const bus = s.y + pitch;
      if (isFoldedLeaf(sourceId, targetId)) {
        // Into the leaf's left side, above its centre when a reverse lane runs under it.
        const dy = reciprocal ? -d : 0;
        const spine = t.x - spineOffset;
        return polylineRoute([
          { x: s.x, y: s.y + s.r }, { x: s.x, y: bus }, { x: spine, y: bus },
          { x: spine, y: t.y + dy }, { x: t.x - reach(t, dy) - arrowGap, y: t.y + dy },
        ]);
      }
      return polylineRoute([
        { x: s.x, y: s.y + s.r }, { x: s.x, y: bus }, { x: t.x, y: bus }, { x: t.x, y: t.y - t.r - arrowGap },
      ]);
    }
    if (kind === "reverse") {
      // The reverse of a branch, child to parent: its own port on both cards and its own bus.
      const { dx, slot, sign, bus } = reverse.get(key)!;
      const into = flankEnd(targetId, slot, sign, 1, arrowGap).reverse();
      if (isFoldedLeaf(targetId, sourceId)) {
        // Up its column's own spine, the riser its branch drops down: one
        // riser per trunk, not a second one a few px beside it.
        // It leaves the leaf under the branch's arrow (dy) and
        // turns off on its own bus above the parent's, so the two still part.
        const dy = d;
        const spine = s.x - spineOffset;
        return polylineRoute([
          { x: s.x - reach(s, dy), y: s.y + dy }, { x: spine, y: s.y + dy }, { x: spine, y: bus },
          { x: t.x + dx, y: bus }, ...into,
        ]);
      }
      const sx = portDx.get(`${key}\u0000s`)!;
      return polylineRoute([
        ...topEnd(sourceId, `${key}\u0000s`, 0), { x: s.x + sx, y: bus }, { x: t.x + dx, y: bus }, ...into,
      ]);
    }
    if (kind === "direct") {
      // Straight across to the neighbour's flank, on its own centre row.
      const dy = directDy.get(key)!;
      const sign = Math.sign(t.x - s.x) || 1;
      const from = { x: s.x + sign * chordHalf(s, dy), y: s.y + dy };
      const to = { x: t.x - sign * (chordHalf(t, dy) + arrowGap), y: t.y + dy };
      const midX = (from.x + to.x) / 2;
      return polylineRoute(from.y === to.y ? [from, to] : [from, { x: midX, y: from.y }, { x: midX, y: to.y }, to]);
    }
    // Any other edge: up into its own channel above its row, from its own port.
    const sx = portDx.get(`${key}\u0000s`)!;
    const tx = portDx.get(`${key}\u0000t`)!;
    const from = topEnd(sourceId, `${key}\u0000s`, 0);
    const into = topEnd(targetId, `${key}\u0000t`, arrowGap).reverse();
    const start = { x: s.x + sx, y: from[from.length - 1].y };
    const end = { x: t.x + tx, y: into[0].y };
    if (kind === "row") {
      const y = Math.min(s.y, t.y) - pitch * channel.get(`${key}\u0000st`)!;
      return polylineRoute([...from, { x: start.x, y }, { x: end.x, y }, ...into]);
    }
    // Across rows: out to the component's return lane, right of every card.
    const lane = laneOf({ sourceId, targetId });
    const fromChannel = s.y - pitch * channel.get(`${key}\u0000s`)!;
    const toChannel = t.y - pitch * channel.get(`${key}\u0000t`)!;
    return polylineRoute([
      ...from, { x: start.x, y: fromChannel }, { x: lane, y: fromChannel },
      { x: lane, y: toChannel }, { x: end.x, y: toChannel }, ...into,
    ]);
  });
}

/** Glyph radius bounds of a node drawn by a layout, in canvas units. */
const GLYPH_RADIUS = { min: 6, max: 12, share: 0.2 } as const;

/**
 * Radius of a node glyph for the `room` (canvas units) its layout keeps
 * around it: a fifth of that room, between 6 and 12, so 200 agents never draw
 * as a wall of rings edge to edge. The renderer adds a 12 px screen
 * floor on top, whatever the zoom.
 */
export function glyphRadius(room: number): number {
  return Math.max(GLYPH_RADIUS.min, Math.min(GLYPH_RADIUS.max, room * GLYPH_RADIUS.share));
}

/**
 * Bow of an edge's arc as a share of the straight distance between its ends:
 * `single` for the one edge of a pair, `step` per arc when a pair
 * holds several edges (A->B and B->A, or two handovers one way).
 */
const EDGE_CURVATURE = { single: 0.12, step: 0.28, reroute: 0.18, maxReroutes: 4 } as const;

const pairKeyOf = (edge: LayoutEdge) =>
  edge.sourceId < edge.targetId ? `${edge.sourceId}|${edge.targetId}` : `${edge.targetId}|${edge.sourceId}`;
const reversed = (edge: LayoutEdge) => edge.sourceId > edge.targetId;

/**
 * Curvature of each edge, in the order given: the bow of its arc as a
 * share of the straight distance between its two ends, positive to the left of
 * its own direction (screen coordinates). The one edge of a pair bows slightly,
 * on the same side of the pair whatever its direction. A pair with several
 * edges draws one arc per edge, stepped away from the straight line on
 * alternate sides: A->B and B->A get the same curvature, so each bows to its
 * own side and the labels at their apexes never meet, as the OpenCTI graph
 * draws reciprocal relationships. Deterministic: within a pair the edges from
 * the smaller id come first, then the order given. A self-loop gets 0.
 */
export function edgeCurvatures(edges: readonly LayoutEdge[]): number[] {
  const byPair = new Map<string, number[]>();
  edges.forEach((edge, i) => {
    if (edge.sourceId === edge.targetId) return;
    const key = pairKeyOf(edge);
    const list = byPair.get(key);
    if (list) list.push(i);
    else byPair.set(key, [i]);
  });
  const out = new Array<number>(edges.length).fill(0);
  for (const indices of byPair.values()) {
    const ordered = [...indices].sort((a, b) => Number(reversed(edges[a])) - Number(reversed(edges[b])) || a - b);
    ordered.forEach((edgeIndex, slot) => {
      // In the pair's own frame (smaller id to larger id): +1, -1, +2, -2... steps.
      const pairSide = ordered.length === 1
        ? EDGE_CURVATURE.single
        : EDGE_CURVATURE.step * (1 + Math.floor(slot / 2)) * (slot % 2 === 0 ? 1 : -1);
      out[edgeIndex] = reversed(edges[edgeIndex]) ? -pairSide : pairSide;
    });
  }
  return out;
}

/**
 * Control point of the quadratic arc from `a` to `b` bowing by `curvature`
 * (see `edgeCurvatures`): the arc's apex, where its label sits, is half way
 * between the chord's middle and this point.
 */
export function arcControlPoint(a: Point, b: Point, curvature: number): Point {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  // Left of the direction a->b with y pointing down: (dy, -dx).
  return { x: (a.x + b.x) / 2 + dy * curvature, y: (a.y + b.y) / 2 - dx * curvature };
}

/**
 * The control point `control` of the arc from `a` to `b`, moved so the arc's
 * apex (its point at t = 0.5, where its label sits) stays inside `box`
 * (a long Top down arc bowed out past the
 * top of the canvas and was cut). The apex moves by half of what the control
 * point moves; an apex already inside keeps the same object.
 */
export function keepArcInside(
  a: Point,
  control: Point,
  b: Point,
  box: { x0: number; y0: number; x1: number; y1: number },
): Point {
  const apex = quadraticPoint(a, control, b, 0.5);
  const dx = apex.x < box.x0 ? box.x0 - apex.x : apex.x > box.x1 ? box.x1 - apex.x : 0;
  const dy = apex.y < box.y0 ? box.y0 - apex.y : apex.y > box.y1 ? box.y1 - apex.y : 0;
  if (dx === 0 && dy === 0) return control;
  return { x: control.x + 2 * dx, y: control.y + 2 * dy };
}

/**
 * The same angles (radians) with every two of them at least `gap` apart
 * around the circle, each moved as little as possible:
 * the ends of a node's edges, so two arrowheads, or an arrowhead and a line
 * leaving the same ring, never lie on each other. Angles already `gap` apart
 * are returned unchanged; a crowded run is spread evenly around its mean, in
 * its own order. More ends than the circle holds at `gap` share it evenly.
 * Deterministic: ties keep the order given.
 */
export function spreadAngles(angles: readonly number[], gap: number): number[] {
  const n = angles.length;
  if (n < 2 || !(gap > 0)) return [...angles];
  const TAU = 2 * Math.PI;
  const norm = (a: number) => ((a % TAU) + TAU) % TAU;
  const order = angles.map((_, i) => i).sort((i, j) => norm(angles[i]) - norm(angles[j]) || i - j);
  const sorted = order.map((i) => norm(angles[i]));
  // The circle is cut in its widest free gap and laid out on a line.
  let cut = 0;
  let widest = -1;
  for (let k = 0; k < n; k++) {
    const free = k === n - 1 ? sorted[0] + TAU - sorted[k] : sorted[k + 1] - sorted[k];
    if (free > widest) {
      widest = free;
      cut = (k + 1) % n;
    }
  }
  const line = Array.from({ length: n }, (_, k) => sorted[(cut + k) % n] + (cut + k >= n ? TAU : 0));
  const step = Math.min(gap, TAU / n);
  // Runs closer than the step merge into one evenly spaced run on their mean.
  const runs: { count: number; sum: number }[] = [];
  const first = (run: { count: number; sum: number }) => run.sum / run.count - ((run.count - 1) * step) / 2;
  for (const angle of line) {
    runs.push({ count: 1, sum: angle });
    while (runs.length > 1) {
      const last = runs[runs.length - 1];
      const before = runs[runs.length - 2];
      if (first(last) - (first(before) + (before.count - 1) * step) >= step - 1e-9) break;
      before.count += last.count;
      before.sum += last.sum;
      runs.pop();
    }
  }
  // The two ends of the line meet again across the cut.
  const lastRun = runs[runs.length - 1];
  if (runs.length > 1 && first(lastRun) + (lastRun.count - 1) * step + step > first(runs[0]) + TAU + 1e-9) {
    runs.splice(0, runs.length, { count: n, sum: line.reduce((s, a) => s + a, 0) });
  }
  const out = new Array<number>(n);
  let k = 0;
  for (const run of runs) {
    const start = first(run);
    for (let j = 0; j < run.count; j++, k++) out[order[(cut + k) % n]] = start + j * step;
  }
  return out;
}

/** One edge drawn as a quadratic arc: its ends, its control point, the angle of each end. */
export interface ArcRoute {
  a: Point;
  control: Point;
  b: Point;
  /** Direction from the source's centre to the arc's start (radians, y down). */
  sourceAngle: number;
  /** Direction from the target's centre to the arc's end, where its arrow's tip is. */
  targetAngle: number;
}

export interface ArcRouteOptions {
  /** Curvature of each edge, in the order of the edges (`edgeCurvatures`). */
  curvatures: readonly number[];
  /** Bow an arc around a third node in its way (`clearCurvature`) this far from its centre. */
  clearance?: number;
  /** Box the apex of every arc stays in (`keepArcInside`). */
  box?: { x0: number; y0: number; x1: number; y1: number };
  /** Gap between an arrow's tip and the ring it points to. */
  arrowGap: number;
  /**
   * Length of an arrowhead and the room kept across between two ends of one
   * node, at the arrowhead's base: the ends of a node's edges are
   * spread around its ring so no arrowhead lies on another end. 0: the ends
   * stay where the arcs meet the ring.
   */
  head?: { length: number; room: number };
}

/**
 * The arcs of the layouts drawn with straight handovers (force, top down,
 * left to right, radial): each edge a quadratic arc with its pair's
 * curvature, bowed around third nodes, its apex inside the box; its start on
 * the source's ring, its end `arrowGap` off the target's ring. With `head`,
 * the ends of one node are spread around its ring (`spreadAngles`), so an
 * arrowhead never lands on another edge's end at that node.
 */
export function arcRoutes(
  edges: readonly LayoutEdge[],
  nodes: ReadonlyMap<string, Point & { r: number }>,
  options: ArcRouteOptions,
): (ArcRoute | null)[] {
  const obstacles = options.clearance ? [...nodes.values()].map((n) => ({ x: n.x, y: n.y })) : [];
  const controls = edges.map((edge, i) => {
    const a = nodes.get(edge.sourceId);
    const b = nodes.get(edge.targetId);
    if (!a || !b) return null;
    const curvature = options.clearance ? clearCurvature(a, b, options.curvatures[i] ?? 0, obstacles, options.clearance) : options.curvatures[i] ?? 0;
    const bowed = arcControlPoint(a, b, curvature);
    return options.box ? keepArcInside(a, bowed, b, options.box) : bowed;
  });
  // Where each end meets its ring, before any spreading: towards the control point.
  const angle = (from: Point, to: Point) => Math.atan2(to.y - from.y, to.x - from.x);
  const startAngle: number[] = [];
  const endAngle: number[] = [];
  edges.forEach((edge, i) => {
    const cp = controls[i];
    if (!cp) return;
    startAngle[i] = angle(nodes.get(edge.sourceId)!, cp);
    endAngle[i] = angle(nodes.get(edge.targetId)!, cp);
  });
  if (options.head && options.head.room > 0) {
    const ends = new Map<string, { edge: number; source: boolean }[]>();
    const add = (id: string, end: { edge: number; source: boolean }) => {
      const list = ends.get(id);
      if (list) list.push(end);
      else ends.set(id, [end]);
    };
    edges.forEach((edge, i) => {
      if (!controls[i] || edge.sourceId === edge.targetId) return;
      add(edge.sourceId, { edge: i, source: true });
      add(edge.targetId, { edge: i, source: false });
    });
    for (const [id, list] of ends) {
      if (list.length < 2) continue;
      const reach = nodes.get(id)!.r + options.arrowGap + options.head.length;
      const spread = spreadAngles(list.map((end) => (end.source ? startAngle : endAngle)[end.edge]), options.head.room / reach);
      list.forEach((end, k) => {
        (end.source ? startAngle : endAngle)[end.edge] = spread[k];
      });
    }
  }
  return edges.map((edge, i) => {
    const control = controls[i];
    if (!control) return null;
    const src = nodes.get(edge.sourceId)!;
    const tgt = nodes.get(edge.targetId)!;
    const sa = startAngle[i];
    const ta = endAngle[i];
    return {
      a: { x: src.x + Math.cos(sa) * src.r, y: src.y + Math.sin(sa) * src.r },
      control,
      b: { x: tgt.x + Math.cos(ta) * (tgt.r + options.arrowGap), y: tgt.y + Math.sin(ta) * (tgt.r + options.arrowGap) },
      sourceAngle: sa,
      targetAngle: ta,
    };
  });
}

/** The point at `t` of the quadratic Bezier a, control, b. */
export function quadraticPoint(a: Point, control: Point, b: Point, t: number): Point {
  const u = 1 - t;
  return {
    x: u * u * a.x + 2 * u * t * control.x + t * t * b.x,
    y: u * u * a.y + 2 * u * t * control.y + t * t * b.y,
  };
}

/** A box by its centre and size. */
export interface LabelBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

const boxesOverlap = (p: LabelBox, q: LabelBox) =>
  Math.abs(p.x - q.x) * 2 < p.w + q.w && Math.abs(p.y - q.y) * 2 < p.h + q.h;

/** Where along its arc a label may sit, by preference: the apex, then outwards. */
const LABEL_SLOTS = [0.5, 0.4, 0.6, 0.32, 0.68, 0.25, 0.75];

/** Area two boxes share (0 when apart). */
const overlapArea = (p: LabelBox, q: LabelBox) =>
  Math.max(0, Math.min(p.x + p.w / 2, q.x + q.w / 2) - Math.max(p.x - p.w / 2, q.x - q.w / 2))
  * Math.max(0, Math.min(p.y + p.h / 2, q.y + q.h / 2) - Math.max(p.y - p.h / 2, q.y - q.h / 2));

/** How far, in label heights, a forced label may step off its arc on either side. */
const FORCED_OFFSET_STEPS = 8;

/**
 * The place of a forced label when no slot on its arc is free: each slot
 * stepped off the arc along its normal, on either side, nearest first; the
 * first box that meets no obstacle and no placed label, else the one with the
 * smallest overlap (the apex on a tie). Never the apex over a name when a
 * clear place exists (#12347 UX r2 B2).
 */
function forcedLabelBox(
  arc: { a: Point; control: Point; b: Point; w: number; h: number },
  blockers: readonly LabelBox[],
): LabelBox {
  let best: LabelBox | null = null;
  let bestOverlap = Infinity;
  const step = arc.h + 2;
  for (let k = 0; k <= FORCED_OFFSET_STEPS; k++) {
    for (const t of LABEL_SLOTS) {
      const p = quadraticPoint(arc.a, arc.control, arc.b, t);
      // Tangent of the quadratic at t; its normal is the side step.
      const tx = 2 * (1 - t) * (arc.control.x - arc.a.x) + 2 * t * (arc.b.x - arc.control.x);
      const ty = 2 * (1 - t) * (arc.control.y - arc.a.y) + 2 * t * (arc.b.y - arc.control.y);
      const len = Math.hypot(tx, ty) || 1;
      const nx = -ty / len;
      const ny = tx / len;
      for (const side of k === 0 ? [0] : [1, -1]) {
        const box = { x: p.x + side * k * step * nx, y: p.y + side * k * step * ny, w: arc.w, h: arc.h };
        const overlap = blockers.reduce((sum, q) => sum + overlapArea(box, q), 0);
        if (overlap === 0) return box;
        // Strictly smaller by more than rounding: the apex keeps a tie.
        if (overlap < bestOverlap - 1e-6) {
          best = box;
          bestOverlap = overlap;
        }
      }
    }
  }
  return best!;
}

/**
 * Centre of each arc's label: at the apex of its own arc, or slid along
 * the arc to the first slot whose box meets no label already placed and no
 * obstacle (a node, a name). When no slot is free the label is left out
 * (`null`, as a name that does not fit: zooming in brings it back), unless the
 * arc is `forced` (the edge under the pointer), which steps off its arc to the
 * nearest clear place, else the least covered one (`forcedLabelBox`). Forced
 * arcs are placed first; then greedy in the order given, so the same arcs
 * always get the same places. `null` arcs are skipped.
 */
export function placeArcLabels(
  arcs: ReadonlyArray<{ a: Point; control: Point; b: Point; w: number; h: number; forced?: boolean } | null>,
  obstacles: readonly LabelBox[],
): Array<Point | null> {
  const placed: LabelBox[] = [];
  const out = new Array<Point | null>(arcs.length).fill(null);
  const order = arcs.map((_, i) => i).sort((i, j) => Number(!!arcs[j]?.forced) - Number(!!arcs[i]?.forced) || i - j);
  for (const i of order) {
    const arc = arcs[i];
    if (!arc) continue;
    let chosen: LabelBox | null = null;
    for (const t of LABEL_SLOTS) {
      const p = quadraticPoint(arc.a, arc.control, arc.b, t);
      const box = { x: p.x, y: p.y, w: arc.w, h: arc.h };
      if (!placed.some((q) => boxesOverlap(box, q)) && !obstacles.some((q) => boxesOverlap(box, q))) {
        chosen = box;
        break;
      }
    }
    if (!chosen && arc.forced) chosen = forcedLabelBox(arc, [...placed, ...obstacles]);
    if (!chosen) continue;
    placed.push(chosen);
    out[i] = { x: chosen.x, y: chosen.y };
  }
  return out;
}

/**
 * The ids whose name, drawn at rest, would cover another node's glyph.
 * Such a name is left out, as a name that does not fit, and
 * comes back on hover, on keyboard focus or once zoomed in. A node's own glyph
 * never hides its name.
 */
export function namesOverGlyphs(
  names: ReadonlyArray<{ id: string; box: LabelBox }>,
  glyphs: ReadonlyArray<{ id: string; box: LabelBox }>,
): Set<string> {
  const hidden = new Set<string>();
  for (const name of names) {
    if (glyphs.some((glyph) => glyph.id !== name.id && boxesOverlap(name.box, glyph.box))) hidden.add(name.id);
  }
  return hidden;
}

/** A node as the name-room pass reads it, in drawing units. */
export interface NameRoomNode {
  id: string;
  x: number;
  y: number;
  /** Radius as drawn (the glyph floor included). */
  r: number;
  /** How far its outermost ring reaches past `r` (agent-graph-view ringReach). */
  reach: number;
  /** Room across its layout guarantees; a node without it gets no room (its name is drawn whole). */
  labelRoom?: number;
}

/** Screen sizes the name-room pass draws with, in px. */
export interface NameRoomMetrics {
  pxPerUnit: number;
  /** Name font size. */
  namePx: number;
  /** Room of a name with no neighbour. */
  lonePx: number;
  /** Gap the renderer keeps out of a room before it fits characters. */
  gapPx: number;
  /** Room a name keeps from another node's ring (namesOverGlyphs). */
  clearancePx: number;
  /**
   * Width of the drawing's canvas (drawing units, x from 0): a name centred
   * under a node near an edge is cut to twice its distance to that edge, so
   * it stays inside the canvas after a layout switch.
   */
  canvasWidth?: number;
  /**
   * Screen px kept free at the canvas's right edge (the zoom controls docked
   * there): with `canvasWidth`, a name near the right edge is cut to twice its
   * distance to that strip, so it wraps or is cut instead of running under it.
   */
  rightReservePx?: number;
}

/**
 * The screen room across for each placed node's name (px): its layout's room
 * (at least `lonePx`), cut to the distance to the nearest node on its row
 * (two neighbours' names share that distance), and to the free span before
 * the glyph box of any node whose glyph reaches the name's band under its
 * card. That last cut is the rule namesOverGlyphs hides a name by, with the
 * same boxes: a name fitted to its room is never left out for covering a
 * neighbour's glyph (a folded org chart column one row
 * pitch lower than its neighbour).
 */
export function nameRoomsPx(nodes: readonly NameRoomNode[], m: NameRoomMetrics): Map<string, number> {
  const rooms = new Map<string, number>();
  const px = m.pxPerUnit;
  const nameH = (m.namePx * 1.2) / px;
  const clearance = (2 * m.clearancePx) / px;
  for (const node of nodes) {
    if (node.labelRoom === undefined) continue;
    let room = Math.max(node.labelRoom * px, m.lonePx);
    if (m.canvasWidth !== undefined) {
      const right = m.canvasWidth - (m.rightReservePx ?? 0) / px;
      room = Math.min(room, 2 * Math.max(0, Math.min(node.x, right - node.x)) * px);
    }
    const nameY = node.y + node.r + 3 + nameH / 2;
    for (const other of nodes) {
      if (other === node) continue;
      const dx = Math.abs(other.x - node.x);
      if (other.labelRoom !== undefined && Math.abs(other.y - node.y) * px < m.namePx + 2) room = Math.min(room, dx * px);
      const side = 2 * (other.r + other.reach) + clearance;
      if (Math.abs(other.y - nameY) * 2 < nameH + side) room = Math.min(room, Math.max(0, 2 * dx - side) * px + m.gapPx);
    }
    rooms.set(node.id, room);
  }
  return rooms;
}

/** What one node draws under its glyph: its name and a ghost's group line, each a box or null. */
export interface NameStack {
  id: string;
  name: LabelBox | null;
  sub: LabelBox | null;
}

/**
 * What each node may draw at rest under its glyph: its name and its group
 * line together when neither covers another node's glyph (`glyphs`, as
 * namesOverGlyphs reads them) nor what a node taken earlier draws; else the name alone, the group line left out
 * (`subless`, the tooltip and the accessible name still say it); else
 * nothing (`hidden`, the name comes back on hover, focus or zoom, as
 * namesOverGlyphs leaves it out). A two-line name and its group line make a
 * taller stack than one name line: at a narrow width a layout's row pitch
 * may not hold it, and the stack is then cut rather than drawn over a
 * neighbour. A node's group line is never checked against its own name,
 * which it sits under by construction. Nodes are taken in the order given.
 */
export function fitNameStacks(
  stacks: readonly NameStack[],
  glyphs: ReadonlyArray<{ id: string; box: LabelBox }>,
): { hidden: Set<string>; subless: Set<string> } {
  const hidden = new Set<string>();
  const subless = new Set<string>();
  const taken: LabelBox[] = [];
  const clear = (id: string, box: LabelBox) =>
    namesOverGlyphs([{ id, box }], glyphs).size === 0 && !taken.some((q) => boxesOverlap(box, q));
  for (const { id, name, sub } of stacks) {
    const nameFits = name === null || clear(id, name);
    if (!nameFits) {
      hidden.add(id);
      if (sub) subless.add(id);
      continue;
    }
    // The group line sits right under the node's own name, so its box
    // always meets that name's: it is checked against the glyphs and what
    // the nodes taken earlier draw, before the node's own name joins them
    // (checked after, every group line was dropped).
    const subFits = sub !== null && clear(id, sub);
    if (name) taken.push(name);
    if (!sub) continue;
    if (subFits) taken.push(sub);
    else subless.add(id);
  }
  return { hidden, subless };
}

/** A fleet grid as its name reads it, in drawing units: the name starts at its left edge, above it. */
export interface FleetNameGrid {
  id: string;
  x: number;
  y: number;
  width: number;
}

/**
 * The room across (drawing units) for each fleet grid's name:
 * the name sits in the band of height `rowHeight` above its grid (`y` is the
 * grid's top) from the grid's left edge, and runs to whichever comes first:
 * the next grid whose name shares that band, any glyph (`nodes`, centre and
 * radius) reaching into the band, each less `gap`, or the canvas edge. Never
 * shorter than its own grid: a name is cut only where something else starts,
 * not at the last glyph of a narrow grid.
 */
export function fleetNameRooms(
  grids: readonly FleetNameGrid[],
  nodes: ReadonlyArray<{ x: number; y: number; r: number }>,
  canvasWidth: number,
  rowHeight: number,
  gap: number,
): Map<string, number> {
  const rooms = new Map<string, number>();
  for (const grid of grids) {
    let end = canvasWidth;
    for (const other of grids) {
      if (other === grid || other.x <= grid.x || Math.abs(other.y - grid.y) >= rowHeight) continue;
      end = Math.min(end, other.x - gap);
    }
    const top = grid.y - rowHeight;
    for (const n of nodes) {
      if (n.x + n.r <= grid.x + grid.width || n.y + n.r <= top || n.y - n.r >= grid.y) continue;
      end = Math.min(end, n.x - n.r - gap);
    }
    rooms.set(grid.id, Math.max(grid.width, end - grid.x));
  }
  return rooms;
}

/**
 * The curvature, on the same side, that keeps the arc from `a` to `b` at least
 * `clearance` away from every node of `obstacles` (layered and radial
 * layouts, where an edge may cross a third node): the given curvature when it
 * is clear, else bowed further a step at a time, up to four steps; the last
 * try when none is clear. The arc's ends themselves are not obstacles.
 */
export function clearCurvature(
  a: Point,
  b: Point,
  curvature: number,
  obstacles: readonly Point[],
  clearance: number,
): number {
  const side = curvature < 0 ? -1 : 1;
  const near = obstacles.filter(
    (p) => Math.hypot(p.x - a.x, p.y - a.y) > 1e-6 && Math.hypot(p.x - b.x, p.y - b.y) > 1e-6,
  );
  let c = curvature;
  for (let step = 0; step <= EDGE_CURVATURE.maxReroutes; step++) {
    c = curvature + side * step * EDGE_CURVATURE.reroute;
    const control = arcControlPoint(a, b, c);
    let clear = true;
    for (let k = 1; k < 10 && clear; k++) {
      const q = quadraticPoint(a, control, b, k / 10);
      for (const p of near) {
        if (Math.hypot(p.x - q.x, p.y - q.y) < clearance) {
          clear = false;
          break;
        }
      }
    }
    if (clear) return c;
  }
  return c;
}

/**
 * Directions a node's badges may take, by preference: the upper diagonals, the
 * sides, the top, then shallow lower directions; never down, where the name is.
 */
const BADGE_DIRECTIONS = [-Math.PI / 4, (-3 * Math.PI) / 4, 0, Math.PI, -Math.PI / 2, Math.PI / 6, (5 * Math.PI) / 6];

const angularGap = (a: number, b: number) => {
  let d = (a - b) % (2 * Math.PI);
  if (d < 0) d += 2 * Math.PI;
  return Math.min(d, 2 * Math.PI - d);
};

/**
 * Directions (radians, y down) for `count` badges around a node whose edges
 * leave or arrive along `edgeAngles`: each badge takes the free
 * direction farthest from every edge end and every badge already placed, so a
 * badge never sits on an edge start; never straight down, where the name is.
 */
export function badgeAngles(edgeAngles: readonly number[], count: number): number[] {
  const chosen: number[] = [];
  for (let k = 0; k < Math.min(count, BADGE_DIRECTIONS.length); k++) {
    let best = BADGE_DIRECTIONS[0];
    let bestGap = -1;
    for (const candidate of BADGE_DIRECTIONS) {
      if (chosen.includes(candidate)) continue;
      let gap = Infinity;
      for (const taken of [...edgeAngles, ...chosen]) gap = Math.min(gap, angularGap(candidate, taken));
      if (gap > bestGap + 1e-9) {
        best = candidate;
        bestGap = gap;
      }
    }
    chosen.push(best);
  }
  return chosen;
}

/**
 * `name` on at most `maxLines` lines of `maxChars` characters, cut at spaces
 * when it can be, the last line ending in an ellipsis when the rest does not
 * fit: a fleet node's name is its only identity on the map, so it
 * wraps where an agent's name would be cut.
 */
export function wrapLabel(name: string, maxChars: number, maxLines = 2): string[] {
  if (maxChars < 1) return [];
  const words = name.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let current = "";
  let i = 0;
  while (i < words.length && lines.length < maxLines) {
    const word = words[i];
    const next = current ? `${current} ${word}` : word;
    if ([...next].length <= maxChars) {
      current = next;
      i++;
      continue;
    }
    if (current) {
      lines.push(current);
      current = "";
      continue;
    }
    // A single word longer than a line: cut it.
    lines.push(truncateLabel(word, maxChars));
    i++;
  }
  if (current && lines.length < maxLines) lines.push(current);
  if (i < words.length && lines.length > 0) {
    const last = lines.length - 1;
    const rest = [lines[last], ...words.slice(i)].join(" ");
    lines[last] = truncateLabel(rest, maxChars);
  }
  return lines;
}

/**
 * `name` wrapped on as many lines as it needs, never cut: a line holds
 * `maxChars` characters, or the longest word when a word is longer, so every
 * word reads whole (the read-only About flow, where no label is truncated).
 */
export function wrapLabelWhole(name: string, maxChars: number): string[] {
  const words = name.split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  const longest = Math.max(...words.map((w) => [...w].length));
  return wrapLabel(name, Math.max(1, maxChars, longest), words.length);
}

/**
 * Screen room (px) a static drawing keeps between a node at its edge and the
 * frame (#10147 UX r2 B3): half the widest label line a node draws, centred
 * under it, plus `edgePx`. A line is the name's longest word, or the room's
 * width when the name is longer (`wrapLabelWhole` wraps it there), so a long
 * name such as "corporate communications" never leaves the frame.
 */
export function staticLabelEdgePx(
  names: readonly string[],
  opts: { namePx: number; glyphAdvance: number; roomPx: number; edgePx: number },
): number {
  const charPx = opts.namePx * opts.glyphAdvance;
  let widest = 0;
  for (const name of names) {
    const words = name.split(/\s+/).filter(Boolean);
    if (words.length === 0) continue;
    const longestWord = Math.max(...words.map((w) => [...w].length)) * charPx;
    const whole = [...name].length * charPx;
    widest = Math.max(widest, Math.max(longestWord, Math.min(whole, opts.roomPx)));
  }
  return Math.ceil(widest / 2) + opts.edgePx;
}

/**
 * A static drawing's name on the lines it needs, never cut (#10147 UX r5:
 * "assista…" and "Threat-informed expos…" on connected nodes): on two lines
 * when they fit `maxPx` across (a line of `charPx` per character), else on
 * three, else on as many as the longest word needs. Each line is as narrow as
 * that line count allows, so the drawing keeps the room between its names.
 */
export function staticNameLines(name: string, charPx: number, maxPx: number): string[] {
  const words = name.split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  const longest = Math.max(...words.map((w) => [...w].length));
  const total = [...words.join(" ")].length;
  const narrowest = (lines: number): number => {
    for (let c = longest; c < total; c++) if (wrapLabelWhole(name, c).length <= lines) return c;
    return total;
  };
  const two = narrowest(2);
  if (two * charPx <= maxPx) return wrapLabelWhole(name, two);
  return wrapLabelWhole(name, narrowest(3));
}

/** A node and what it draws (screen px): its glyph above its centre, its name stack below, `w` across. */
export interface NameBlock {
  id: string;
  x: number;
  y: number;
  w: number;
  above: number;
  below: number;
}

/**
 * A frozen drawing's nodes moved apart (screen px) until no two name blocks
 * overlap with `gapPx` between them, each block kept inside `bounds`: the
 * spacing is read from the drawn names, not from the node distance, so no
 * name is cut and no two names run together (#10147 UX r5 B1: centres 46 to
 * 53 px apart under 60 px names at 1280). A pair is pushed apart along the
 * axis it overlaps least, half each; deterministic (the given order breaks
 * ties).
 */
export function spreadNameBlocks(
  blocks: readonly NameBlock[],
  bounds: { x0: number; x1: number; y0: number; y1: number },
  gapPx: number,
  iterations = 80,
): Map<string, Point> {
  const pos = blocks.map((b) => ({ x: b.x, y: b.y }));
  const clamp = (i: number) => {
    const b = blocks[i];
    const p = pos[i];
    const lo = bounds.x0 + b.w / 2;
    const hi = bounds.x1 - b.w / 2;
    p.x = lo > hi ? (bounds.x0 + bounds.x1) / 2 : Math.min(hi, Math.max(lo, p.x));
    const top = bounds.y0 + b.above;
    const bottom = bounds.y1 - b.below;
    p.y = top > bottom ? top : Math.min(bottom, Math.max(top, p.y));
  };
  blocks.forEach((_, i) => clamp(i));
  for (let it = 0; it < iterations; it++) {
    let moved = false;
    for (let i = 0; i < blocks.length; i++) {
      for (let j = i + 1; j < blocks.length; j++) {
        const a = blocks[i], b = blocks[j];
        const pa = pos[i], pb = pos[j];
        const dx = pb.x - pa.x;
        const overX = (a.w + b.w) / 2 + gapPx - Math.abs(dx);
        if (overX <= 0) continue;
        // Vertical extents: the glyph above the centre, the names below.
        const aTop = pa.y - a.above, aBottom = pa.y + a.below;
        const bTop = pb.y - b.above, bBottom = pb.y + b.below;
        const overY = Math.min(aBottom, bBottom) - Math.max(aTop, bTop) + gapPx;
        if (overY <= 0) continue;
        moved = true;
        if (overX <= overY) {
          const s = dx === 0 ? 1 : Math.sign(dx);
          pa.x -= (s * overX) / 2;
          pb.x += (s * overX) / 2;
        } else {
          const s = pb.y + (b.below - b.above) / 2 >= pa.y + (a.below - a.above) / 2 ? 1 : -1;
          pa.y -= (s * overY) / 2;
          pb.y += (s * overY) / 2;
        }
        clamp(i);
        clamp(j);
      }
    }
    if (!moved) break;
  }
  return new Map(blocks.map((b, i) => [b.id, pos[i]]));
}

/**
 * Room two nodes side by side keep between their centres in the force
 * drawing: the label's width, between one and LABEL_BOX_ACROSS_MAX node
 * distances.
 */
export function labelBoxAcross(minDistance: number, labelWidth: number): number {
  return Math.max(minDistance, Math.min(labelWidth, minDistance * LABEL_BOX_ACROSS_MAX));
}

/**
 * `separate` on an ellipse: two nodes are at least `across` apart side by
 * side and `minDistance` apart one above the other, so a name drawn under its
 * node keeps clear of its neighbours on the same row.
 */
export function separateLabelled(positions: Map<string, Point>, minDistance: number, across: number): Map<string, Point> {
  const k = minDistance / Math.max(minDistance, across);
  const squeezed = new Map([...positions].map(([id, p]) => [id, { x: p.x * k, y: p.y }]));
  const out = separate(squeezed, minDistance);
  return new Map([...out].map(([id, p]) => [id, { x: p.x / k, y: p.y }]));
}

/** Fruchterman-Reingold with a fixed seed, iteration count and cooling, on one connected component. */
function forceComponent(
  nodeIds: readonly string[],
  edges: readonly LayoutEdge[],
  minDistance: number,
  linkDistance: number,
  charge: number,
  across = minDistance,
): Map<string, Point> {
  const n = nodeIds.length;
  const positions = new Map<string, Point>();
  const k = Math.max(1, linkDistance); // ideal edge length
  const rand = seeded(n * 7919 + edges.length);
  const side = Math.sqrt(n) * k;
  const pts = nodeIds.map(() => ({ x: (rand() - 0.5) * side, y: (rand() - 0.5) * side }));
  const index = new Map(nodeIds.map((id, i) => [id, i]));
  const links = edges.map((e) => [index.get(e.sourceId)!, index.get(e.targetId)!]);
  const iterations = 300;
  let temperature = side / 4;
  const cooling = temperature / (iterations + 1);
  for (let it = 0; it < iterations; it++) {
    const disp = pts.map(() => ({ x: 0, y: 0 }));
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        let dx = pts[i].x - pts[j].x;
        let dy = pts[i].y - pts[j].y;
        let d = Math.hypot(dx, dy);
        if (d < 0.01) { dx = 0.01 * (i - j); dy = 0.01; d = Math.hypot(dx, dy); }
        const f = (charge * k * k) / d;
        disp[i].x += (dx / d) * f; disp[i].y += (dy / d) * f;
        disp[j].x -= (dx / d) * f; disp[j].y -= (dy / d) * f;
      }
      // A weak pull to the centre keeps the drawing compact.
      disp[i].x -= pts[i].x * 0.05; disp[i].y -= pts[i].y * 0.05;
    }
    for (const [a, b] of links) {
      const dx = pts[a].x - pts[b].x;
      const dy = pts[a].y - pts[b].y;
      const d = Math.max(0.01, Math.hypot(dx, dy));
      const f = (d * d) / k;
      disp[a].x -= (dx / d) * f; disp[a].y -= (dy / d) * f;
      disp[b].x += (dx / d) * f; disp[b].y += (dy / d) * f;
    }
    for (let i = 0; i < n; i++) {
      const d = Math.max(0.01, Math.hypot(disp[i].x, disp[i].y));
      const step = Math.min(d, temperature);
      pts[i].x += (disp[i].x / d) * step;
      pts[i].y += (disp[i].y / d) * step;
    }
    temperature -= cooling;
  }
  nodeIds.forEach((id, i) => positions.set(id, pts[i]));
  return separateLabelled(lyingFlat(positions), minDistance, across);
}

/**
 * `positions` turned about their centre so their long axis lies across:
 * the map's canvas is more than twice as wide as tall, and a
 * component left standing (a chain of hubs drawn top to bottom) made the whole
 * drawing height-bound, its nodes 28 px apart on a 1440 px screen whatever
 * the node distance. A drawing already wider than tall is left as it is.
 */
export function lyingFlat(positions: Map<string, Point>): Map<string, Point> {
  const pts = [...positions.values()];
  if (pts.length < 3) return positions;
  const mx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
  const my = pts.reduce((s, p) => s + p.y, 0) / pts.length;
  let sxx = 0, syy = 0, sxy = 0;
  for (const p of pts) {
    const dx = p.x - mx, dy = p.y - my;
    sxx += dx * dx; syy += dy * dy; sxy += dx * dy;
  }
  // Angle of the principal axis, turned back onto the x axis.
  const theta = 0.5 * Math.atan2(2 * sxy, sxx - syy);
  const cos = Math.cos(-theta), sin = Math.sin(-theta);
  const turned = new Map<string, Point>();
  for (const [id, p] of positions) {
    const dx = p.x - mx, dy = p.y - my;
    turned.set(id, { x: dx * cos - dy * sin, y: dx * sin + dy * cos });
  }
  const before = extent(positions.values()), after = extent(turned.values());
  return after.w - after.h > before.w - before.h + 1e-9 ? turned : positions;
}

/** `ids` in rows of `perRow`, `colGap` and `rowGap` apart, centred on `cx`, the first row at `top`. */
function isolatedRows(
  ids: readonly string[],
  positions: Map<string, Point>,
  cx: number,
  top: number,
  colGap: number,
  rowGap: number,
  perRow: number,
): Map<string, Point> {
  ids.forEach((id, i) => {
    const row = Math.floor(i / perRow);
    const inRow = Math.min(perRow, ids.length - row * perRow);
    const col = i % perRow;
    positions.set(id, { x: cx + (col - (inRow - 1) / 2) * colGap, y: top + row * rowGap });
  });
  return positions;
}

/**
 * The agents with no edge as one grid cluster, `colGap` x `rowGap` cells, put
 * beside (right) or under the drawing already in `positions`, whichever side
 * and column count let the whole drawing be scaled largest into a canvas of
 * width / height `aspect`. Alone, the grid is centred on the origin
 * with the canvas's proportions. Cells are at least `rowGap` apart and the
 * cluster a full cell away from the drawing, so nothing overlaps.
 */
export function isolatedCluster(
  ids: readonly string[],
  positions: Map<string, Point>,
  colGap: number,
  rowGap: number,
  aspect: number,
): Map<string, Point> {
  const n = ids.length;
  if (n === 0) return positions;
  const place = (cols: number, left: number, top: number) =>
    ids.forEach((id, i) => positions.set(id, { x: left + (i % cols) * colGap, y: top + Math.floor(i / cols) * rowGap }));
  // Scale of a w x h drawing in an `aspect` x 1 canvas, a node's room around it.
  const scaleOf = (w: number, h: number) => Math.min(aspect / (w + colGap), 1 / (h + rowGap));
  if (positions.size === 0) {
    let cols = 1;
    for (let c = 1; c <= n; c++) {
      const fit = scaleOf((c - 1) * colGap, (Math.ceil(n / c) - 1) * rowGap);
      if (fit > scaleOf((cols - 1) * colGap, (Math.ceil(n / cols) - 1) * rowGap) + 1e-12) cols = c;
    }
    const rows = Math.ceil(n / cols);
    place(cols, -((Math.min(cols, n) - 1) * colGap) / 2, -((rows - 1) * rowGap) / 2);
    return positions;
  }
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const p of positions.values()) {
    x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x); y0 = Math.min(y0, p.y); y1 = Math.max(y1, p.y);
  }
  const dw = x1 - x0, dh = y1 - y0;
  let best = { scale: -1, cols: 1, left: 0, top: 0 };
  for (let cols = 1; cols <= n; cols++) {
    const gw = (cols - 1) * colGap;
    const gh = (Math.ceil(n / cols) - 1) * rowGap;
    const beside = { scale: scaleOf(dw + colGap + gw, Math.max(dh, gh)), cols, left: x1 + colGap, top: (y0 + y1) / 2 - gh / 2 };
    const under = { scale: scaleOf(Math.max(dw, gw), dh + rowGap * 1.5 + gh), cols, left: (x0 + x1) / 2 - gw / 2, top: y1 + rowGap * 1.5 };
    for (const option of [beside, under]) if (option.scale > best.scale + 1e-12) best = option;
  }
  place(best.cols, best.left, best.top);
  return positions;
}

/**
 * Push apart every pair closer than `minDistance`, a bounded number of passes,
 * then settle whatever still collides on the nearest free cell of a
 * `minDistance` grid: the result never has two nodes closer than that.
 */
export function separate(positions: Map<string, Point>, minDistance: number): Map<string, Point> {
  const ids = [...positions.keys()].sort();
  const pts = ids.map((id) => ({ ...positions.get(id)! }));
  for (let pass = 0; pass < 50; pass++) {
    let moved = false;
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        const dx = pts[j].x - pts[i].x;
        const dy = pts[j].y - pts[i].y;
        const d = Math.hypot(dx, dy);
        if (d >= minDistance) continue;
        moved = true;
        const ux = d > 0.001 ? dx / d : 1;
        const uy = d > 0.001 ? dy / d : 0;
        const push = (minDistance - d) / 2 + 0.5;
        pts[i].x -= ux * push; pts[i].y -= uy * push;
        pts[j].x += ux * push; pts[j].y += uy * push;
      }
    }
    if (!moved) break;
  }
  if (minimumDistance(pts) < minDistance) {
    const taken = new Set<string>();
    for (const p of pts) {
      const cx = Math.round(p.x / minDistance);
      const cy = Math.round(p.y / minDistance);
      // Spiral out to the nearest free cell.
      let placed = false;
      for (let r = 0; !placed; r++) {
        for (let gx = cx - r; gx <= cx + r && !placed; gx++) {
          for (let gy = cy - r; gy <= cy + r && !placed; gy++) {
            if (Math.max(Math.abs(gx - cx), Math.abs(gy - cy)) !== r) continue;
            const key = `${gx},${gy}`;
            if (taken.has(key)) continue;
            taken.add(key);
            p.x = gx * minDistance; p.y = gy * minDistance;
            placed = true;
          }
        }
      }
    }
  }
  return new Map(ids.map((id, i) => [id, pts[i]]));
}

/** The smallest distance between two of the points (Infinity under two points). */
export function minimumDistance(points: Iterable<Point>): number {
  const pts = [...points];
  let best = Infinity;
  for (let i = 0; i < pts.length; i++) {
    for (let j = i + 1; j < pts.length; j++) {
      best = Math.min(best, Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y));
    }
  }
  return best;
}

/**
 * `name` cut to `maxChars` characters, an ellipsis standing for the rest: the
 * graph draws names at a fixed size while it scales positions into the
 * canvas, so a name wider than the room between two neighbours is shortened.
 */
export function truncateLabel(name: string, maxChars: number): string {
  const chars = [...name];
  if (chars.length <= maxChars) return name;
  return `${chars.slice(0, Math.max(1, maxChars - 1)).join("").trimEnd()}\u2026`;
}

export interface CanvasFit {
  /** Uniform scale from layout units to canvas units. */
  scale: number;
  /** Canvas height: the base height, or taller when the drawing needs it. */
  height: number;
  /**
   * Scale across alone: the width-bound fit, never under `scale`. A drawing
   * that may stretch across (a top-down hierarchy, narrow and tall) uses it
   * for x, so the canvas width a height-bound fit leaves unused gives its
   * row neighbours the room of their names.
   */
  across: number;
}

/**
 * The scale that fits a drawing of `drawingW` x `drawingH` layout units into a
 * canvas `width` wide, never enlarged past `maxScale`. A drawing taller than
 * the canvas grows the canvas down to `maxHeight` instead of shrinking the
 * whole drawing: fitting 48 agents of a left-to-right hierarchy into a
 * 1400 x 600 canvas made every name unreadable, while the page has room to
 * scroll. `padRight` (default `padX`) is the room kept on the right,
 * wider where controls sit over the canvas's right edge.
 */
export function fitToCanvas(
  drawingW: number,
  drawingH: number,
  width: number,
  height: number,
  maxHeight: number,
  padX: number,
  padY: number,
  maxScale = 1.5,
  padRight = padX,
): CanvasFit {
  const fitW = Math.min(maxScale, (width - padX - padRight) / Math.max(1, drawingW));
  const wanted = Math.ceil(Math.max(1, drawingH) * fitW + 2 * padY);
  const canvasH = Math.max(height, Math.min(maxHeight, wanted));
  const fitH = (canvasH - 2 * padY) / Math.max(1, drawingH);
  const scale = Math.min(fitW, fitH);
  return { scale, height: canvasH, across: Math.max(scale, fitW) };
}

/** Positions of `layout` for the nodes and edges given. */
export function layoutGraph(
  layout: GraphLayout,
  ids: readonly string[],
  edges: readonly LayoutEdge[],
  options: LayoutOptions = {},
): Map<string, Point> {
  switch (layout) {
    case "hierarchy-tb":
      return layeredLayout(ids, edges, true, options);
    case "hierarchy-lr":
      return layeredLayout(ids, edges, false, options);
    case "radial":
      return radialLayout(ids, edges, options);
    case "orgchart":
      return orgChartLayout(ids, edges, options);
    default:
      return forceLayout(ids, edges, options);
  }
}
