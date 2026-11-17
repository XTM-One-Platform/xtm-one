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
 * The treemap: values on a hierarchy of up to three levels as nested
 * rectangles, each the area of its value (squarified, Bruls et al. 2000, the
 * algorithm d3-hierarchy uses; written here, a few dozen lines, rather than a
 * dependency). A group keeps a header strip for its name when it has the
 * room; a rectangle too small to read is still drawn, and its label left to
 * the hover.
 *
 * Pure, type-only imports: runs on `node --test`.
 */
import type { WidgetTreeData } from "@/types";

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface TreemapCell extends Rect {
  /** The path of names from the top: one name for a top-level cell. */
  path: string[];
  value: number;
  depth: number;
  /** A group holding children (drawn as a frame with a header) or a leaf. */
  leaf: boolean;
  /** The top-level group it belongs to, for its colour. */
  group: number;
}

/** The height of a group's header strip (px), and the least room a group needs to keep one. */
export const HEADER = 18;
const HEADER_MIN_H = HEADER * 2.5;
const HEADER_MIN_W = 48;
/** The gap between siblings (px). */
export const GAP = 2;

interface Node {
  name: string;
  value: number;
  children: Node[];
}

function hierarchy(data: WidgetTreeData): Node {
  const root: Node = { name: "", value: 0, children: [] };
  for (const item of data.items) {
    if (!(item.value > 0) || item.path.length === 0) continue;
    let node = root;
    node.value += item.value;
    for (const name of item.path) {
      let child = node.children.find((c) => c.name === name);
      if (!child) {
        child = { name, value: 0, children: [] };
        node.children.push(child);
      }
      child.value += item.value;
      node = child;
    }
  }
  const sort = (node: Node) => {
    node.children.sort((a, b) => b.value - a.value || a.name.localeCompare(b.name));
    node.children.forEach(sort);
  };
  sort(root);
  return root;
}

/** The worst aspect ratio of a row of areas laid along a side of length `side`. */
function worst(areas: number[], side: number): number {
  const sum = areas.reduce((a, b) => a + b, 0);
  if (sum <= 0 || side <= 0) return Infinity;
  const max = Math.max(...areas);
  const min = Math.min(...areas);
  return Math.max((side * side * max) / (sum * sum), (sum * sum) / (side * side * min));
}

/** Squarified rectangles for `values` (largest first) inside `rect`, in the same order. */
export function squarify(values: readonly number[], rect: Rect): Rect[] {
  const total = values.reduce((a, b) => a + Math.max(0, b), 0);
  const out: Rect[] = values.map(() => ({ x: rect.x, y: rect.y, w: 0, h: 0 }));
  if (total <= 0 || rect.w <= 0 || rect.h <= 0) return out;
  const scale = (rect.w * rect.h) / total;
  const areas = values.map((v) => Math.max(0, v) * scale);
  let { x, y, w, h } = rect;
  let start = 0;
  while (start < areas.length) {
    const side = Math.min(w, h);
    let end = start + 1;
    while (end < areas.length && worst(areas.slice(start, end + 1), side) <= worst(areas.slice(start, end), side)) end += 1;
    const row = areas.slice(start, end);
    const sum = row.reduce((a, b) => a + b, 0);
    if (w >= h) {
      // A column along the left side.
      const columnW = h > 0 ? sum / h : 0;
      let cy = y;
      row.forEach((area, i) => {
        const cellH = columnW > 0 ? area / columnW : 0;
        out[start + i] = { x, y: cy, w: columnW, h: cellH };
        cy += cellH;
      });
      x += columnW;
      w -= columnW;
    } else {
      const rowH = w > 0 ? sum / w : 0;
      let cx = x;
      row.forEach((area, i) => {
        const cellW = rowH > 0 ? area / rowH : 0;
        out[start + i] = { x: cx, y, w: cellW, h: rowH };
        cx += cellW;
      });
      y += rowH;
      h -= rowH;
    }
    start = end;
  }
  return out;
}

const inset = (rect: Rect, by: number): Rect => ({
  x: rect.x + by,
  y: rect.y + by,
  w: Math.max(0, rect.w - 2 * by),
  h: Math.max(0, rect.h - 2 * by),
});

/** Every cell of the treemap of `data` in a `width` x `height` box, groups before their children. */
export function treemapLayout(data: WidgetTreeData, width: number, height: number): TreemapCell[] {
  const root = hierarchy(data);
  const cells: TreemapCell[] = [];
  const place = (node: Node, rect: Rect, path: string[], depth: number, group: number) => {
    const rects = squarify(
      node.children.map((child) => child.value),
      rect,
    );
    node.children.forEach((child, index) => {
      const own = inset(rects[index], GAP / 2);
      const childPath = [...path, child.name];
      const top = depth === 0 ? index : group;
      const leaf = child.children.length === 0;
      cells.push({ ...own, path: childPath, value: child.value, depth, leaf, group: top });
      if (leaf) return;
      const header = own.h >= HEADER_MIN_H && own.w >= HEADER_MIN_W ? HEADER : 0;
      place(child, { x: own.x, y: own.y + header, w: own.w, h: Math.max(0, own.h - header) }, childPath, depth + 1, top);
    });
  };
  place(root, { x: 0, y: 0, w: width, h: height }, [], 0, 0);
  return cells;
}
