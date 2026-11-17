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
 * Animated layout changes of the agent graph (Agentic Flow map).
 *
 * The layouts in `agent-graph-layout.ts` are deterministic: they give the
 * final position of every node. When they change (a layout switch, a force
 * setting, the Fleets toggle, a focus, a quick filter, an agent added or
 * re-parented) the map moves each node from where it is drawn to its new
 * place in `LAYOUT_TWEEN_MS`, eased cubic in-out:
 *
 * - a node that stays slides to its new place;
 * - a node that appears fades in and grows from half its radius where it lands;
 * - a node that goes away fades out where it was, and takes no pointer events;
 * - every edge stays attached to its two nodes on each frame, and fades in or
 *   out with them.
 *
 * Pure functions, unit tested in agent-graph-tween.test.ts; the frame loop is
 * `useAnimatedLayout` (hooks/useAnimatedLayout.ts), which writes each frame on
 * the svg without a React render (150 nodes at 60 fps).
 */

/** Length of one layout change on screen (ms). */
export const LAYOUT_TWEEN_MS = 700;

interface Point {
  x: number;
  y: number;
}

interface Arc {
  a: Point;
  control: Point;
  b: Point;
}

export interface TweenNode {
  id: string;
  x: number;
  y: number;
  r: number;
}

export interface TweenEdge {
  id: string;
  sourceId: string;
  targetId: string;
  path: string;
  midX: number;
  midY: number;
  arc?: Arc;
}

/** What a frame adds to a node or an edge: its opacity factor and whether it is going away. */
export interface Faded {
  fade: number;
  leaving?: boolean;
}

/** Cubic ease in-out of a progress between 0 and 1. */
export function easeCubicInOut(t: number): number {
  const p = Math.min(1, Math.max(0, t));
  return p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
}

function lerp(from: number, to: number, t: number): number {
  return from + (to - from) * t;
}

/** The SVG path of a quadratic arc. */
export function arcPath(arc: Arc): string {
  return `M ${arc.a.x},${arc.a.y} Q ${arc.control.x},${arc.control.y} ${arc.b.x},${arc.b.y}`;
}

/**
 * The arc moved with its two nodes: its start by the source's move, its end
 * by the target's, its control point by the mean of both.
 */
function shiftArc(arc: Arc, source: Point, target: Point): Arc {
  return {
    a: { x: arc.a.x + source.x, y: arc.a.y + source.y },
    control: { x: arc.control.x + (source.x + target.x) / 2, y: arc.control.y + (source.y + target.y) / 2 },
    b: { x: arc.b.x + target.x, y: arc.b.y + target.y },
  };
}

/**
 * A stable identity per edge, whatever its index: source, target and its rank
 * among the edges of the same ordered pair.
 */
export function edgeKeys(edges: readonly { sourceId: string; targetId: string }[]): string[] {
  const seen = new Map<string, number>();
  return edges.map((edge) => {
    const pair = `${edge.sourceId}>${edge.targetId}`;
    const rank = seen.get(pair) ?? 0;
    seen.set(pair, rank + 1);
    return `${pair}#${rank}`;
  });
}

/** Whether two drawings place the same nodes at the same points and draw the same edges. */
export function samePlacement(
  a: { nodes: readonly TweenNode[]; edges: readonly TweenEdge[] },
  b: { nodes: readonly TweenNode[]; edges: readonly TweenEdge[] },
): boolean {
  if (a.nodes.length !== b.nodes.length || a.edges.length !== b.edges.length) return false;
  const at = new Map(a.nodes.map((n) => [n.id, n]));
  for (const node of b.nodes) {
    const other = at.get(node.id);
    if (!other || other.x !== node.x || other.y !== node.y) return false;
  }
  const keysA = edgeKeys(a.edges);
  const keysB = new Set(edgeKeys(b.edges));
  return keysA.every((key) => keysB.has(key));
}

/** Where each node of the last frame was drawn, and how opaque. */
export interface DrawnState {
  positions: ReadonlyMap<string, Point>;
  fades: ReadonlyMap<string, number>;
  edgeFades: ReadonlyMap<string, number>;
}

export function drawnState<E extends TweenEdge>(
  nodes: readonly (TweenNode & Partial<Faded>)[],
  edges: readonly (E & Partial<Faded>)[],
  keyOf: (edge: E) => string,
): DrawnState {
  return {
    positions: new Map(nodes.map((n) => [n.id, { x: n.x, y: n.y }])),
    fades: new Map(nodes.map((n) => [n.id, n.fade ?? 1])),
    edgeFades: new Map(edges.map((e) => [keyOf(e), e.fade ?? 1])),
  };
}

/**
 * One frame of a layout change at eased progress `t` (0 = as drawn when the
 * change started, 1 = the target drawing).
 *
 * `leaving` holds the nodes and edges drawn when the change started that the
 * target no longer has, as they were drawn then. `keyOf` gives an edge's
 * stable identity (see `edgeKeys`).
 */
export function layoutFrame<N extends TweenNode, E extends TweenEdge>(
  start: DrawnState,
  target: { nodes: readonly N[]; edges: readonly E[] },
  leaving: { nodes: readonly N[]; edges: readonly E[] },
  t: number,
  keyOf: (edge: E) => string,
): { nodes: Array<N & Faded>; edges: Array<E & Faded> } {
  const grow = (r: number, fade: number) => r * (0.5 + 0.5 * fade);
  const moved = new Map<string, Point>();
  const fades = new Map<string, number>();
  const nodes: Array<N & Faded> = [];

  for (const node of target.nodes) {
    const from = start.positions.get(node.id) ?? node;
    const fade = lerp(start.fades.get(node.id) ?? 0, 1, t);
    const x = lerp(from.x, node.x, t);
    const y = lerp(from.y, node.y, t);
    moved.set(node.id, { x: x - node.x, y: y - node.y });
    fades.set(node.id, fade);
    nodes.push({ ...node, x, y, r: grow(node.r, fade), fade });
  }
  for (const node of leaving.nodes) {
    const fade = (start.fades.get(node.id) ?? 1) * (1 - t);
    moved.set(node.id, { x: 0, y: 0 });
    fades.set(node.id, fade);
    nodes.push({ ...node, r: grow(node.r, fade), fade, leaving: true });
  }

  const zero = { x: 0, y: 0 };
  const drawnNow = new Map(nodes.map((n) => [n.id, n]));
  const movedSince = (id: string): Point => {
    const from = start.positions.get(id);
    const now = drawnNow.get(id);
    return from && now ? { x: now.x - from.x, y: now.y - from.y } : zero;
  };
  const follow = (edge: E, fade: number, leavingEdge: boolean): E & Faded => {
    const nodeFade = Math.min(fades.get(edge.sourceId) ?? 1, fades.get(edge.targetId) ?? 1);
    const shown = Math.min(fade, nodeFade);
    if (!edge.arc) return { ...edge, fade: shown, ...(leavingEdge ? { leaving: true } : {}) };
    // A target edge's arc is drawn for the target places: shift it by how far
    // its nodes still are from them. A leaving edge was drawn where its nodes
    // were when the change started: shift it by how far they moved since.
    const source = leavingEdge ? movedSince(edge.sourceId) : moved.get(edge.sourceId) ?? zero;
    const targetMove = leavingEdge ? movedSince(edge.targetId) : moved.get(edge.targetId) ?? zero;
    const arc = shiftArc(edge.arc, source, targetMove);
    const mid = {
      x: 0.25 * arc.a.x + 0.5 * arc.control.x + 0.25 * arc.b.x,
      y: 0.25 * arc.a.y + 0.5 * arc.control.y + 0.25 * arc.b.y,
    };
    return {
      ...edge,
      arc,
      path: arcPath(arc),
      midX: mid.x,
      midY: mid.y,
      fade: shown,
      ...(leavingEdge ? { leaving: true } : {}),
    };
  };
  const edges: Array<E & Faded> = [
    ...target.edges.map((edge) => follow(edge, lerp(start.edgeFades.get(keyOf(edge)) ?? 0, 1, t), false)),
    ...leaving.edges.map((edge) => follow(edge, (start.edgeFades.get(keyOf(edge)) ?? 1) * (1 - t), true)),
  ];
  return { nodes, edges };
}

/**
 * The zoom and pan that frame `box` (drawing units) in a `width` x `height`
 * canvas with `margin` units around it, for a viewBox derived as
 * `vbW = width / zoom`, `vbX = (width - vbW) / 2 - pan.x / zoom` (same for y).
 */
export function fitTransform(
  box: { x0: number; y0: number; x1: number; y1: number },
  width: number,
  height: number,
  margin: number,
  minZoom: number,
  maxZoom: number,
): { zoom: number; pan: Point } {
  const w = Math.max(1, box.x1 - box.x0 + 2 * margin);
  const h = Math.max(1, box.y1 - box.y0 + 2 * margin);
  const zoom = Math.min(maxZoom, Math.max(minZoom, Math.min(width / w, height / h)));
  const cx = (box.x0 + box.x1) / 2;
  const cy = (box.y0 + box.y1) / 2;
  return { zoom, pan: { x: (width / 2 - cx) * zoom, y: (height / 2 - cy) * zoom } };
}

/** Frames per second of a run of `frames` frames over `ms` milliseconds, rounded. */
export function frameRate(frames: number, ms: number): number {
  return ms > 0 ? Math.round((frames * 1000) / ms) : 0;
}
