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
 * Force-directed layout of the graph visualization: the drawing the agent map
 * used before it moved to its own layout, kept here for the dashboard graph.
 */

export interface ForceNode {
  x: number;
  y: number;
  vx: number;
  vy: number;
}

interface ForceEdge {
  s: number;
  t: number;
}

interface ForceLayoutOptions {
  /** Repulsion strength between all node pairs. */
  repulsion?: number;
  /** Attraction strength along edges. */
  attraction?: number;
  /** Pull towards canvas center. */
  centerPull?: number;
  /** Velocity damping per tick. */
  damping?: number;
  /** Minimum inter-node distance for boosted repulsion. */
  minDistance?: number;
  /** Initial ellipse radius multiplier (0–1 of available space). */
  radiusScale?: number;
  /** Post-layout overlap separation passes (0 = disabled). */
  separationPasses?: number;
  /** Edge-crossing repulsion strength (0 = disabled). */
  crossingRepulsion?: number;
}

const DEFAULT_OPTIONS: Required<ForceLayoutOptions> = {
  repulsion: 6000,
  attraction: 0.008,
  centerPull: 0.006,
  damping: 0.85,
  minDistance: 50,
  radiusScale: 0.36,
  separationPasses: 0,
  crossingRepulsion: 0,
};

/** Check if segments (p1–p2) and (p3–p4) cross. */
function segsCross(
  x1: number, y1: number, x2: number, y2: number,
  x3: number, y3: number, x4: number, y4: number,
): boolean {
  const d1x = x2 - x1, d1y = y2 - y1;
  const d2x = x4 - x3, d2y = y4 - y3;
  const denom = d1x * d2y - d1y * d2x;
  if (Math.abs(denom) < 1e-10) return false;
  const t = ((x3 - x1) * d2y - (y3 - y1) * d2x) / denom;
  const u = ((x3 - x1) * d1y - (y3 - y1) * d1x) / denom;
  return t > 0.01 && t < 0.99 && u > 0.01 && u < 0.99;
}

/**
 * Run a force-directed layout on the given nodes and edges.
 *
 * Mutates node positions in-place. Uses BFS-ordered initial placement
 * (starting from the highest-degree node) to reduce initial crossings,
 * then iterates repulsion/attraction/centering/crossing forces.
 */
export function forceLayout(
  nodes: ForceNode[],
  edges: ForceEdge[],
  w: number,
  h: number,
  padX: number,
  padY: number,
  iterations: number,
  opts?: ForceLayoutOptions,
): void {
  const n = nodes.length;
  if (n === 0) return;
  if (n === 1) { nodes[0].x = w / 2; nodes[0].y = h / 2; return; }

  const {
    repulsion, attraction, centerPull, damping,
    minDistance, radiusScale, separationPasses, crossingRepulsion,
  } = { ...DEFAULT_OPTIONS, ...opts };

  const cx = w / 2;
  const cy = h / 2;
  const rx = (w - 2 * padX) * radiusScale;
  const ry = (h - 2 * padY) * radiusScale;

  // ── BFS-ordered initial placement ──
  const adj: number[][] = Array.from({ length: n }, () => []);
  for (const { s, t } of edges) { adj[s].push(t); adj[t].push(s); }

  const order: number[] = [];
  const vis = new Uint8Array(n);
  let best = 0;
  for (let i = 1; i < n; i++) { if (adj[i].length > adj[best].length) best = i; }
  const q = [best];
  vis[best] = 1;
  while (q.length > 0) {
    const cur = q.shift()!;
    order.push(cur);
    const nbs = adj[cur].filter(nb => !vis[nb]);
    nbs.sort((a, b) => adj[b].length - adj[a].length);
    for (const nb of nbs) { if (!vis[nb]) { vis[nb] = 1; q.push(nb); } }
  }
  for (let i = 0; i < n; i++) { if (!vis[i]) order.push(i); }

  for (let idx = 0; idx < order.length; idx++) {
    const i = order[idx];
    const a = (idx / n) * 2 * Math.PI - Math.PI / 2;
    nodes[i].x = cx + rx * Math.cos(a);
    nodes[i].y = cy + ry * Math.sin(a);
    nodes[i].vx = 0;
    nodes[i].vy = 0;
  }

  const eLen = edges.length;

  for (let iter = 0; iter < iterations; iter++) {
    const cool = Math.max(0.01, 1 - iter / iterations);

    // Repulsion (all pairs)
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const dx = nodes[i].x - nodes[j].x;
        const dy = nodes[i].y - nodes[j].y;
        let d = Math.sqrt(dx * dx + dy * dy);
        if (d < 1) d = 1;
        const boost = d < minDistance ? minDistance / d : 1;
        const f = (repulsion / (d * d)) * cool * boost;
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        nodes[i].vx += fx; nodes[i].vy += fy;
        nodes[j].vx -= fx; nodes[j].vy -= fy;
      }
    }

    // Attraction along edges
    for (const { s, t } of edges) {
      const dx = nodes[t].x - nodes[s].x;
      const dy = nodes[t].y - nodes[s].y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = d * attraction * cool;
      const fx = (dx / d) * f;
      const fy = (dy / d) * f;
      nodes[s].vx += fx; nodes[s].vy += fy;
      nodes[t].vx -= fx; nodes[t].vy -= fy;
    }

    // Edge-crossing repulsion (every 3 iterations — O(E²))
    if (crossingRepulsion > 0 && eLen > 1 && iter % 3 === 0) {
      for (let a = 0; a < eLen; a++) {
        const ea = edges[a];
        for (let b = a + 1; b < eLen; b++) {
          const eb = edges[b];
          if (ea.s === eb.s || ea.s === eb.t || ea.t === eb.s || ea.t === eb.t) continue;
          if (!segsCross(
            nodes[ea.s].x, nodes[ea.s].y, nodes[ea.t].x, nodes[ea.t].y,
            nodes[eb.s].x, nodes[eb.s].y, nodes[eb.t].x, nodes[eb.t].y,
          )) continue;
          const mAx = (nodes[ea.s].x + nodes[ea.t].x) / 2;
          const mAy = (nodes[ea.s].y + nodes[ea.t].y) / 2;
          const mBx = (nodes[eb.s].x + nodes[eb.t].x) / 2;
          const mBy = (nodes[eb.s].y + nodes[eb.t].y) / 2;
          let dmx = mAx - mBx, dmy = mAy - mBy;
          const dm = Math.sqrt(dmx * dmx + dmy * dmy) || 1;
          dmx /= dm; dmy /= dm;
          const push = crossingRepulsion * cool / (dm + 1);
          nodes[ea.s].vx += dmx * push; nodes[ea.s].vy += dmy * push;
          nodes[ea.t].vx += dmx * push; nodes[ea.t].vy += dmy * push;
          nodes[eb.s].vx -= dmx * push; nodes[eb.s].vy -= dmy * push;
          nodes[eb.t].vx -= dmx * push; nodes[eb.t].vy -= dmy * push;
        }
      }
    }

    // Center pull
    for (let i = 0; i < n; i++) {
      nodes[i].vx += (cx - nodes[i].x) * centerPull * cool;
      nodes[i].vy += (cy - nodes[i].y) * centerPull * cool;
    }

    // Update & clamp
    for (let i = 0; i < n; i++) {
      nodes[i].vx *= damping;
      nodes[i].vy *= damping;
      nodes[i].x += nodes[i].vx;
      nodes[i].y += nodes[i].vy;
      nodes[i].x = Math.max(padX, Math.min(w - padX, nodes[i].x));
      nodes[i].y = Math.max(padY, Math.min(h - padY, nodes[i].y));
    }
  }

  // Post-layout overlap separation
  if (separationPasses > 0) {
    for (let pass = 0; pass < separationPasses; pass++) {
      let anyOverlap = false;
      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          let dx = nodes[j].x - nodes[i].x;
          let dy = nodes[j].y - nodes[i].y;
          const d = Math.sqrt(dx * dx + dy * dy);
          if (d >= minDistance) continue;
          anyOverlap = true;
          if (d < 0.1) {
            const hash = Math.imul(i + 1, 2654435761) ^ Math.imul(j + 1, 40503);
            dx = ((hash & 0xff) / 127.5 - 1) || 0.5;
            dy = (((hash >>> 8) & 0xff) / 127.5 - 1) || 0.5;
          }
          const len = Math.sqrt(dx * dx + dy * dy) || 1;
          const push = (minDistance - d) / 2 + 0.5;
          const ux = dx / len;
          const uy = dy / len;
          nodes[i].x -= ux * push; nodes[i].y -= uy * push;
          nodes[j].x += ux * push; nodes[j].y += uy * push;
          nodes[i].x = Math.max(padX, Math.min(w - padX, nodes[i].x));
          nodes[i].y = Math.max(padY, Math.min(h - padY, nodes[i].y));
          nodes[j].x = Math.max(padX, Math.min(w - padX, nodes[j].x));
          nodes[j].y = Math.max(padY, Math.min(h - padY, nodes[j].y));
        }
      }
      if (!anyOverlap) break;
    }
  }
}
