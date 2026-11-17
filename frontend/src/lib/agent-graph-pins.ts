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
 * Agents moved by hand on the agent map: a node dragged on
 * the map stays where it is dropped (pinned) until the layout changes or the
 * pins are released. The layout itself is never recomputed for a pin: the
 * node moves, and each arc touching it follows, its end on the moved node
 * shifted by the node's move and its control point by half of it, the way a
 * layout change moves arcs frame by frame (agent-graph-tween.ts).
 */
import { arcPath } from "./agent-graph-tween";

interface Point {
  x: number;
  y: number;
}

/** A name's box on the map, centred on x, y (drawing units). */
export interface NameBox extends Point {
  w: number;
  h: number;
}

/**
 * `box` grown by `pad` on every side (drawing units): a ghost's group line
 * counted in the drop room with the name clearance around it, so the moved
 * glyph's outer ring keeps off it, not only its disc.
 */
export function paddedNameBox<B extends NameBox>(box: B, pad: number): B {
  if (!(pad > 0)) return box;
  return { ...box, w: box.w + 2 * pad, h: box.h + 2 * pad };
}

interface PinNode extends Point {
  id: string;
}

interface PinEdge {
  sourceId: string;
  targetId: string;
  path: string;
  midX: number;
  midY: number;
  arc?: { a: Point; control: Point; b: Point };
}

/**
 * The drawing with every pinned node at its pin, and the arcs touching it
 * moved with it. The same arrays come back when nothing is pinned, and a
 * node or edge nothing moved keeps its object, so React skips it.
 */
export function pinDrawing<N extends PinNode, E extends PinEdge>(
  nodes: N[],
  edges: E[],
  pins: ReadonlyMap<string, Point>,
): { nodes: N[]; edges: E[] } {
  if (pins.size === 0) return { nodes, edges };
  const moves = new Map<string, Point>();
  const pinnedNodes = nodes.map((node) => {
    const pin = pins.get(node.id);
    if (!pin || (pin.x === node.x && pin.y === node.y)) return node;
    moves.set(node.id, { x: pin.x - node.x, y: pin.y - node.y });
    return { ...node, x: pin.x, y: pin.y };
  });
  if (moves.size === 0) return { nodes, edges };
  const still = { x: 0, y: 0 };
  const pinnedEdges = edges.map((edge) => {
    const s = moves.get(edge.sourceId);
    const t = moves.get(edge.targetId);
    if (!edge.arc || (!s && !t)) return edge;
    const ds = s ?? still;
    const dt = t ?? still;
    const arc = {
      a: { x: edge.arc.a.x + ds.x, y: edge.arc.a.y + ds.y },
      control: { x: edge.arc.control.x + (ds.x + dt.x) / 2, y: edge.arc.control.y + (ds.y + dt.y) / 2 },
      b: { x: edge.arc.b.x + dt.x, y: edge.arc.b.y + dt.y },
    };
    return {
      ...edge,
      arc,
      path: arcPath(arc),
      midX: Math.round(0.25 * arc.a.x + 0.5 * arc.control.x + 0.25 * arc.b.x),
      midY: Math.round(0.25 * arc.a.y + 0.5 * arc.control.y + 0.25 * arc.b.y),
    };
  });
  return { nodes: pinnedNodes, edges: pinnedEdges };
}

// Rings `freeSpot` tries around a drop before it keeps the drop as it is.
const FREE_SPOT_RINGS = 8;

/**
 * Where a node dropped at `at` sits:
 * `at` itself (the same object) when every other node is at least
 * `minDistance` away, else the nearest free point on rings around it, the
 * first ring half that distance out, tried first away from the nearest node
 * and then alternately to either side of that direction. A drop never lands
 * a glyph on another one; with no free point in reach, `at` is kept.
 *
 * With `names` (boxes centred on x, y, of size w x h), the dropped glyph of
 * radius `radius` also stays clear of every box: a drop under a neighbour
 * never covers that neighbour's name.
 */
export function freeSpot(
  at: Point,
  others: readonly Point[],
  minDistance: number,
  names: readonly NameBox[] = [],
  radius = 0,
): Point {
  if ((others.length === 0 && names.length === 0) || minDistance <= 0) return at;
  const offName = (p: Point) => names.every((b) => {
    const dx = Math.max(0, Math.abs(p.x - b.x) - b.w / 2);
    const dy = Math.max(0, Math.abs(p.y - b.y) - b.h / 2);
    return Math.hypot(dx, dy) >= radius;
  });
  const clear = (p: Point) => offName(p) && others.every((o) => Math.hypot(o.x - p.x, o.y - p.y) >= minDistance);
  if (clear(at)) return at;
  // Away from the nearest node, or from the nearest name when no node is near.
  const near = others.length > 0 ? others : names;
  let nearest = near[0];
  for (const o of near) {
    if (Math.hypot(o.x - at.x, o.y - at.y) < Math.hypot(nearest.x - at.x, nearest.y - at.y)) nearest = o;
  }
  const away = nearest.x === at.x && nearest.y === at.y ? -Math.PI / 2 : Math.atan2(at.y - nearest.y, at.x - nearest.x);
  for (let ring = 1; ring <= FREE_SPOT_RINGS; ring++) {
    const radius = (ring * minDistance) / 2;
    const steps = 8 * ring;
    for (let k = 0; k <= steps / 2; k++) {
      for (const side of k === 0 || k === steps / 2 ? [1] : [1, -1]) {
        const angle = away + (side * 2 * Math.PI * k) / steps;
        const p = { x: Math.round(at.x + radius * Math.cos(angle)), y: Math.round(at.y + radius * Math.sin(angle)) };
        if (clear(p)) return p;
      }
    }
  }
  return at;
}

const ARROW_STEPS: Record<string, [number, number]> = {
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
};

/**
 * Where an arrow key moves a node at `at` by `step` (a node moves from the
 * keyboard too), or null for any other key.
 */
export function nudged(at: Point, key: string, step: number): Point | null {
  const [dx, dy] = ARROW_STEPS[key] ?? [0, 0];
  if (dx === 0 && dy === 0) return null;
  return { x: at.x + dx * step, y: at.y + dy * step };
}

/**
 * Where an arrow key moves a node at `at`: the step of `nudged`, then the free spot of `freeSpot`, so a move
 * from the keyboard never lands a glyph on another one either. Null for any
 * key other than an arrow.
 */
export function keyboardMove(
  at: Point,
  key: string,
  step: number,
  others: readonly Point[],
  minDistance: number,
  names: readonly NameBox[] = [],
  radius = 0,
): Point | null {
  const to = nudged(at, key, step);
  return to ? freeSpot(to, others, minDistance, names, radius) : null;
}

/**
 * The agent the keyboard focus goes to from `from` on Alt+Arrow:
 * the closest of `others` inside the 90 degree cone
 * around the arrow's direction, distance weighted twice as much off the axis
 * as along it, ties broken by id so the answer never depends on the order of
 * `others`. Null for a key other than an arrow or when no agent lies that way.
 */
export function nearestInDirection<T extends Point & { id: string }>(
  from: Point,
  key: string,
  others: readonly T[],
): T | null {
  const [dx, dy] = ARROW_STEPS[key] ?? [0, 0];
  if (dx === 0 && dy === 0) return null;
  let best: T | null = null;
  let bestScore = Infinity;
  for (const o of others) {
    const along = (o.x - from.x) * dx + (o.y - from.y) * dy;
    const across = Math.abs((o.x - from.x) * dy - (o.y - from.y) * dx);
    if (along <= 0 || across > along) continue;
    const score = along + 2 * across;
    if (score < bestScore || (score === bestScore && best !== null && o.id < best.id)) {
      best = o;
      bestScore = score;
    }
  }
  return best;
}

/**
 * What Alt+`key` does on a focused agent: every arrow is
 * `handled`, so the caller blocks the browser default even when no agent lies
 * that way (Alt+Left / Alt+Right are Back / Forward on Windows and Linux), and
 * `next` is the agent the focus goes to, null to stay put. A key other than
 * an arrow is not handled and keeps its default.
 */
export function altArrowFocus<T extends Point & { id: string }>(
  from: Point,
  key: string,
  others: readonly T[],
): { handled: boolean; next: T | null } {
  if (!(key in ARROW_STEPS)) return { handled: false, next: null };
  return { handled: true, next: nearestInDirection(from, key, others) };
}

/** `pins` with `id` at `at`: a new map, the one given is left as it is. */
export function withPin(pins: ReadonlyMap<string, Point>, id: string, at: Point): Map<string, Point> {
  const next = new Map(pins);
  next.set(id, { x: Math.round(at.x), y: Math.round(at.y) });
  return next;
}
