/**
 * Moving and resizing a widget of a dashboard grid with the keyboard
 * (`DashboardGrid`), with the same outcome as the pointer: the
 * grid's own `moveElement` and vertical compaction decide where the other
 * widgets go.
 */
import { cloneLayout, moveElement, verticalCompactor, type Layout, type LayoutItem } from "react-grid-layout";
import { GRID_COLUMNS, MAX_WIDGET_HEIGHT } from "./layout";

const samePlaces = (a: Layout, b: Layout) => {
  const place = (layout: Layout) =>
    JSON.stringify([...layout].sort((p, q) => p.i.localeCompare(q.i)).map(({ i, x, y, w, h }) => [i, x, y, w, h]));
  return place(a) === place(b);
};

/**
 * The grid after moving widget `id` with the keyboard, the way a drag moves it:
 * the grid's own `moveElement` pushes the widgets in the way, then the grid is
 * compacted as it is after a drag. A one-row step the compaction undoes (a
 * widget moved down into a gap that closes again) is not a move, so the step
 * grows until the arrangement changes; `null` when it never does. The
 * arrangement is compared with the grid compacted, so compaction closing a gap
 * the grid already had (a saved layout, a default whose sizes changed) is not
 * taken for the move.
 */
export function keyboardMove(grid: Layout, id: string, dx: number, dy: number): Layout | null {
  const start = grid.find((item) => item.i === id);
  if (!start) return null;
  const bottom = grid.reduce((max, item) => Math.max(max, item.y + item.h), 0);
  const settled = verticalCompactor.compact(cloneLayout(grid) as LayoutItem[], GRID_COLUMNS);
  for (let step = 1; ; step += 1) {
    const x = start.x + dx * step;
    const y = start.y + dy * step;
    if (x < 0 || x + start.w > GRID_COLUMNS || y < 0 || y > bottom) return null;
    const layout = cloneLayout(grid) as LayoutItem[];
    const item = layout.find((candidate) => candidate.i === id) as LayoutItem;
    const moved = moveElement(layout, item, x, y, true, false, "vertical", GRID_COLUMNS);
    const next = verticalCompactor.compact(moved, GRID_COLUMNS);
    if (!samePlaces(next, settled)) return next;
  }
}

/** The grid after resizing widget `id` with the keyboard, within its minimum
 *  (the `minW` / `minH` the grid carries), the grid's columns and
 *  `MAX_WIDGET_HEIGHT`; `null` at a bound. */
export function keyboardResize(grid: Layout, id: string, dw: number, dh: number): Layout | null {
  const layout = cloneLayout(grid) as LayoutItem[];
  const item = layout.find((candidate) => candidate.i === id);
  if (!item) return null;
  const w = item.w + dw;
  const h = item.h + dh;
  if (w < (item.minW ?? 1) || item.x + w > GRID_COLUMNS || h < (item.minH ?? 1) || h > MAX_WIDGET_HEIGHT) return null;
  item.w = w;
  item.h = h;
  // Compaction places the widgets in reading order, pushing down whatever the
  // resized widget now covers, as the grid does after a resize.
  return verticalCompactor.compact(layout, GRID_COLUMNS);
}
