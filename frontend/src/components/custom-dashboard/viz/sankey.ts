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
 * The sankey: flows between nodes as bands as wide as their value, nodes in
 * columns from the sources (left) to the sinks (right, justified), each as
 * tall as the larger of what enters and what leaves it. Written here, like
 * d3-sankey's justified layout and a few relaxation passes, without the
 * dependency.
 *
 * A flow must go forward: a self-loop, a flow of no positive value, and the
 * flows closing a cycle (found depth-first, in the order given) are left out
 * and counted, so the view can say so.
 *
 * Pure, type-only imports: runs on `node --test`.
 */
import type { WidgetGraphData } from "@/types";

interface SankeyNode {
  id: string;
  label: string;
  column: number;
  value: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

interface SankeyLink {
  source: string;
  target: string;
  value: number;
  /** The band's width (px). */
  width: number;
  /** The band's centre line: a cubic curve from the source's right side to the target's left. */
  path: string;
}

export interface SankeyLayout {
  nodes: SankeyNode[];
  links: SankeyLink[];
  /** Flows left out: a self-loop, no positive value, or one closing a cycle. */
  dropped: number;
}

export const NODE_WIDTH = 12;
const NODE_PADDING = 10;
const ITERATIONS = 6;

const round = (value: number) => Math.round(value * 100) / 100;

export function sankeyLayout(graph: WidgetGraphData, width: number, height: number): SankeyLayout {
  const labels = new Map(graph.nodes.map((node) => [node.id, node.label]));
  let dropped = 0;
  // Forward flows only: no loop, a positive value.
  const candidates = graph.edges.filter((edge) => {
    const ok = edge.source !== edge.target && (edge.value ?? 1) > 0 && labels.has(edge.source) && labels.has(edge.target);
    if (!ok) dropped += 1;
    return ok;
  });
  // Drop the flows that close a cycle: a depth-first walk keeps a flow unless it reaches a node on the current path.
  const out = new Map<string, typeof candidates>();
  for (const edge of candidates) out.set(edge.source, [...(out.get(edge.source) ?? []), edge]);
  const state = new Map<string, 1 | 2>();
  const kept = new Set<(typeof candidates)[number]>();
  const visit = (id: string) => {
    state.set(id, 1);
    for (const edge of out.get(id) ?? []) {
      const seen = state.get(edge.target);
      if (seen === 1) continue;
      kept.add(edge);
      if (seen === undefined) visit(edge.target);
    }
    state.set(id, 2);
  };
  for (const node of graph.nodes) if (!state.has(node.id)) visit(node.id);
  const edges = candidates.filter((edge) => kept.has(edge));
  dropped += candidates.length - edges.length;

  const used = new Set(edges.flatMap((edge) => [edge.source, edge.target]));
  const ids = graph.nodes.map((node) => node.id).filter((id) => used.has(id));
  if (ids.length === 0) return { nodes: [], links: [], dropped };
  const inflow = new Map<string, number>();
  const outflow = new Map<string, number>();
  for (const edge of edges) {
    const value = edge.value ?? 1;
    outflow.set(edge.source, (outflow.get(edge.source) ?? 0) + value);
    inflow.set(edge.target, (inflow.get(edge.target) ?? 0) + value);
  }
  // Columns: the longest path from a source; a sink goes to the last column.
  const column = new Map<string, number>();
  const depthOf = (id: string, trail = new Set<string>()): number => {
    const known = column.get(id);
    if (known !== undefined) return known;
    trail.add(id);
    const parents = edges.filter((edge) => edge.target === id && !trail.has(edge.source));
    const depth = parents.length ? Math.max(...parents.map((edge) => depthOf(edge.source, trail) + 1)) : 0;
    column.set(id, depth);
    return depth;
  };
  ids.forEach((id) => depthOf(id));
  const last = Math.max(...column.values());
  for (const id of ids) if (!outflow.has(id)) column.set(id, last);

  const columns: string[][] = Array.from({ length: last + 1 }, () => []);
  ids.forEach((id) => columns[column.get(id) ?? 0].push(id));
  const valueOf = (id: string) => Math.max(inflow.get(id) ?? 0, outflow.get(id) ?? 0);
  // One vertical scale for every column: the fullest one fills the height.
  const ky = Math.min(
    ...columns
      .filter((ids_) => ids_.length > 0)
      .map((ids_) => (height - (ids_.length - 1) * NODE_PADDING) / ids_.reduce((sum, id) => sum + valueOf(id), 0)),
  );
  const scale = Number.isFinite(ky) && ky > 0 ? ky : 0;
  const step = last > 0 ? (width - NODE_WIDTH) / last : 0;
  const nodes = new Map<string, SankeyNode>();
  columns.forEach((ids_, index) => {
    let y = 0;
    for (const id of ids_) {
      const h = Math.max(1, valueOf(id) * scale);
      nodes.set(id, { id, label: labels.get(id) ?? id, column: index, value: valueOf(id), x: index * step, y, w: NODE_WIDTH, h });
      y += h + NODE_PADDING;
    }
  });

  const centre = (node: SankeyNode) => node.y + node.h / 2;
  const resolve = (ids_: string[]) => {
    const list = ids_.map((id) => nodes.get(id)!).sort((a, b) => a.y - b.y);
    let y = 0;
    for (const node of list) {
      node.y = Math.max(node.y, y);
      y = node.y + node.h + NODE_PADDING;
    }
    // Pushed past the bottom: push back up from there.
    let bottom = height;
    for (const node of list.reverse()) {
      if (node.y + node.h > bottom) node.y = Math.max(0, bottom - node.h);
      bottom = node.y - NODE_PADDING;
    }
  };
  // Relax: each node toward the weighted centre of its neighbours, forward then back.
  for (let pass = 0; pass < ITERATIONS; pass += 1) {
    const order = pass % 2 === 0 ? columns : [...columns].reverse();
    for (const ids_ of order) {
      for (const id of ids_) {
        const node = nodes.get(id)!;
        let weight = 0;
        let sum = 0;
        for (const edge of edges) {
          const other = edge.source === id ? edge.target : edge.target === id ? edge.source : null;
          if (other === null) continue;
          const value = edge.value ?? 1;
          sum += centre(nodes.get(other)!) * value;
          weight += value;
        }
        if (weight > 0) node.y += (sum / weight - centre(node)) * 0.5;
      }
      resolve(ids_);
    }
  }

  // Bands leave and enter their nodes in the order of the other end, top to bottom.
  const sourceOffset = new Map<string, number>();
  const targetOffset = new Map<string, number>();
  const ordered = [...edges].sort(
    (a, b) => centre(nodes.get(a.target)!) - centre(nodes.get(b.target)!) || centre(nodes.get(a.source)!) - centre(nodes.get(b.source)!),
  );
  const links: SankeyLink[] = [];
  const bySourceOrder = new Map<(typeof edges)[number], number>();
  for (const edge of ordered) {
    const value = edge.value ?? 1;
    const band = value * scale;
    const offset = sourceOffset.get(edge.source) ?? 0;
    sourceOffset.set(edge.source, offset + band);
    bySourceOrder.set(edge, offset);
  }
  const byTarget = [...edges].sort((a, b) => centre(nodes.get(a.source)!) - centre(nodes.get(b.source)!));
  for (const edge of byTarget) {
    const value = edge.value ?? 1;
    const band = value * scale;
    const s = nodes.get(edge.source)!;
    const t = nodes.get(edge.target)!;
    const tOffset = targetOffset.get(edge.target) ?? 0;
    targetOffset.set(edge.target, tOffset + band);
    const x0 = s.x + s.w;
    const x1 = t.x;
    const y0 = s.y + (bySourceOrder.get(edge) ?? 0) + band / 2;
    const y1 = t.y + tOffset + band / 2;
    const mid = (x0 + x1) / 2;
    links.push({
      source: edge.source,
      target: edge.target,
      value,
      width: Math.max(1, round(band)),
      path: `M${round(x0)},${round(y0)}C${round(mid)},${round(y0)} ${round(mid)},${round(y1)} ${round(x1)},${round(y1)}`,
    });
  }
  return { nodes: [...nodes.values()], links, dropped };
}
