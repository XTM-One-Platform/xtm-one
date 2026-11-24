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
 * The camera of the Tasks > Work Items graph across a live refresh (#1158).
 * No React: `WorkItemGraphView.tsx` applies what this answers.
 *
 * The graph is read again every few seconds and the fleets write items all the
 * time, so a refresh never fits the view: it keeps the reader's zoom and moves
 * the pan by what the layout moved the node they look at (the drawn node nearest
 * the centre of the canvas), so that node stays under their eyes.
 */

export interface GraphPoint {
  x: number;
  y: number;
}

export interface GraphViewport extends GraphPoint {
  zoom: number;
}

/**
 * The reader's choices that redraw the graph from scratch and fit it again: the
 * layout, the grouping, the collapsed groups, the focus mode and its isolated
 * node, done and cancelled shown. Never a count or anything a refresh changes.
 */
export function graphFitKey(choices: {
  layout: string;
  groupBy: string;
  collapsedKeys: ReadonlySet<string>;
  focusMode: string;
  /** The selected node: it changes the drawing only when the focus isolates its chain. */
  selected: string | null;
  showDone: boolean;
}): string {
  const isolated = choices.focusMode === "isolate" ? (choices.selected ?? "") : "";
  return [
    choices.layout,
    choices.groupBy,
    [...choices.collapsedKeys].sort().join(","),
    choices.focusMode,
    isolated,
    choices.showDone ? "done" : "",
  ].join("|");
}

/**
 * The viewport after a refresh moved the drawn nodes from `before` to `after`
 * (top-left corners in flow coordinates): same zoom, the pan shifted so the node
 * nearest the canvas centre (`width` x `height` in screen pixels) and still drawn
 * keeps its screen place. Null when nothing has to move: no such node, or it did
 * not move.
 */
export function anchoredViewport(
  before: ReadonlyMap<string, GraphPoint>,
  after: ReadonlyMap<string, GraphPoint>,
  viewport: GraphViewport,
  canvas: { width: number; height: number },
): GraphViewport | null {
  if (viewport.zoom <= 0 || canvas.width <= 0 || canvas.height <= 0) return null;
  const centre = {
    x: (canvas.width / 2 - viewport.x) / viewport.zoom,
    y: (canvas.height / 2 - viewport.y) / viewport.zoom,
  };
  let anchor: { from: GraphPoint; to: GraphPoint } | null = null;
  let best = Number.POSITIVE_INFINITY;
  for (const [id, from] of before) {
    const to = after.get(id);
    if (!to) continue;
    const distance = (from.x - centre.x) ** 2 + (from.y - centre.y) ** 2;
    if (distance < best) {
      best = distance;
      anchor = { from, to };
    }
  }
  if (!anchor) return null;
  const dx = anchor.to.x - anchor.from.x;
  const dy = anchor.to.y - anchor.from.y;
  if (dx === 0 && dy === 0) return null;
  return { x: viewport.x - dx * viewport.zoom, y: viewport.y - dy * viewport.zoom, zoom: viewport.zoom };
}
