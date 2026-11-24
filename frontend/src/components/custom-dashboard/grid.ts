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
 * Where a custom dashboard's widgets sit on the 12-column grid
 * (`components/dashboard-grid`).
 *
 * The server stores and returns every widget with its `layout`
 * (`backend/app/services/dashboard_grid.py`); a widget read without one (a row
 * a newer server has not placed, a hand-written payload) is placed here the
 * way the server would place it: below the placed widgets, in order, as wide
 * as its `width` and as tall as its kind needs. The grid then draws the
 * dashboard; a drag or a resize becomes one `layout` operation naming the
 * widgets that moved.
 */
// Pure, with relative imports only: `spec.ts` reads layouts through it and runs under `node --test`.
import type { WidgetRegistry } from "../dashboard-grid/layout";
import type { CustomDashboardOperation, DashboardLayoutItem } from "../../types";

import type { DashboardWidget, DashboardWidth } from "./spec";

/** The grid's columns and tallest widget (`dashboard-grid/layout.ts`). */
const GRID_COLUMNS = 12;
const MAX_WIDGET_HEIGHT = 40;

export interface GridCell {
  x: number;
  y: number;
  w: number;
  h: number;
}

const WIDTH_COLUMNS: Record<DashboardWidth, number> = { full: 12, half: 6, third: 4 };

/** Rows (OpenCTI's: 62 px, 20 px gaps) a widget of a kind takes without a layout (the server's `_KIND_ROWS`). */
const KIND_ROWS: Record<string, number> = {
  score: 3,
  stats: 2,
  stages: 2,
  decision: 3,
  feed: 4,
  callout: 2,
  ranked_list: 3,
  steps: 3,
  checklist: 3,
  progress: 2,
  prompts: 2,
  text: 2,
};
const VIEW_ROWS: Record<string, number> = {
  number: 3,
  list: 4,
  table: 4,
  map: 5,
  choroplethsvg: 5,
  linkgraph: 5,
  networkgraph: 5,
  sankey: 5,
  parallel: 4,
  timeline: 4,
  treemap: 4,
};
const SIZE_ROWS = { s: 3, m: 4, l: 5 } as const;

/** A stored `layout` when it fits the grid, else `undefined`. */
export function readCell(value: unknown): GridCell | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const { x, y, w, h } = value as Record<string, unknown>;
  const ints = [x, y, w, h].every((v) => typeof v === "number" && Number.isInteger(v));
  if (!ints) return undefined;
  const cell = { x, y, w, h } as GridCell;
  const fits =
    cell.x >= 0 && cell.y >= 0 && cell.w >= 1 && cell.x + cell.w <= GRID_COLUMNS && cell.h >= 1 && cell.h <= MAX_WIDGET_HEIGHT;
  return fits ? cell : undefined;
}

/** The rows `widget` takes without a layout. */
function defaultRows(widget: DashboardWidget): number {
  if (widget.kind === "platform") {
    if (!widget.view) return 2;
    return VIEW_ROWS[widget.view] ?? SIZE_ROWS[widget.size];
  }
  if (widget.kind === "data") return widget.view ? (VIEW_ROWS[widget.view] ?? SIZE_ROWS.m) : 2;
  if (widget.kind === "input") return 1;
  return KIND_ROWS[widget.kind] ?? 3;
}

/** The smallest a widget still reads at: a chart or a table needs room. */
function minimumSize(widget: DashboardWidget): { minW: number; minH: number } {
  if ((widget.kind === "platform" || widget.kind === "data") && widget.view && widget.view !== "number") return { minW: 3, minH: 2 };
  return { minW: 2, minH: 1 };
}

/** Every widget with a place: the placed ones keep theirs, the others go below them, in order. */
export function placeWidgets<W extends DashboardWidget>(widgets: readonly (W & { layout?: GridCell })[]): (W & { layout: GridCell })[] {
  let top = widgets.reduce((max, widget) => (widget.layout ? Math.max(max, widget.layout.y + widget.layout.h) : max), 0);
  let x = 0;
  let row = 0;
  return widgets.map((widget) => {
    if (widget.layout) return widget as W & { layout: GridCell };
    const w = WIDTH_COLUMNS[widget.width] ?? GRID_COLUMNS;
    const h = defaultRows(widget);
    if (x + w > GRID_COLUMNS) {
      top += row;
      x = 0;
      row = 0;
    }
    const layout = { x, y: top, w, h };
    x += w;
    row = Math.max(row, h);
    return { ...widget, layout };
  });
}

/** Pixels from the top of a cell `rows` high to its bottom (OpenCTI's 62 px rows, 20 px gaps). */
function cellHeight(rows: number): number {
  return rows * 62 + (rows - 1) * 20;
}

/** The height of a platform widget's chart: its cell less the heading, padding and footer; stacked, by `size`. */
export function chartHeight(size: "s" | "m" | "l", cell: { rows: number } | null): number {
  if (!cell) return { s: 160, m: 240, l: 320 }[size];
  return Math.max(96, cellHeight(cell.rows) - 104);
}

/** The grid's registry for these widgets: each one where it is, with its minimum size. */
export function widgetRegistry(widgets: readonly DashboardWidget[]): WidgetRegistry<string> {
  return Object.fromEntries(widgets.map((widget) => [widget.id, { ...widget.layout, ...minimumSize(widget) }]));
}

/** The `layout` operation saving the grid's new arrangement: the widgets whose place changed, or `null`. */
export function layoutOperation(
  widgets: readonly DashboardWidget[],
  items: readonly DashboardLayoutItem<string>[],
): CustomDashboardOperation | null {
  const before = new Map(widgets.map((widget) => [widget.id, widget.layout]));
  const moved = items
    .filter((item) => {
      const cell = before.get(item.i);
      return cell && (cell.x !== item.x || cell.y !== item.y || cell.w !== item.w || cell.h !== item.h);
    })
    .map(({ i, x, y, w, h }) => ({ id: i, x, y, w, h }));
  return moved.length > 0 ? { op: "layout", items: moved } : null;
}
