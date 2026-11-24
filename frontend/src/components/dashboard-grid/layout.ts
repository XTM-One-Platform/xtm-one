/**
 * A dashboard built on the grid of widgets: the grid's bounds, and how a saved
 * layout becomes the one drawn.
 *
 * Each dashboard built this way (the CTEM command center, the Copilot
 * dashboard) declares its widgets and their default places in a registry
 * (`CTEM_WIDGETS`, `COPILOT_WIDGETS`); every function here takes it. The
 * backend accepts the widget ids and grid bounds of
 * `services/dashboard_layouts.py` only, the same as these.
 */
import type { DashboardLayout, DashboardLayoutItem } from "@/types";

/** Columns of the grid. */
export const GRID_COLUMNS = 12;
/** Height of one grid row, in pixels (gaps excluded). */
export const GRID_ROW_HEIGHT = 20;
/** Gap between widgets, in pixels. */
export const GRID_MARGIN = 16;
/** Tallest a widget may be, in rows. */
export const MAX_WIDGET_HEIGHT = 40;

/** A grid's rows: their height and the gap between widgets, in pixels. */
export interface GridRows {
  rowHeight: number;
  margin: number;
}
/** The platform's own dashboards (the CTEM command center, the Copilot dashboard). */
export const DEFAULT_GRID_ROWS: GridRows = { rowHeight: GRID_ROW_HEIGHT, margin: GRID_MARGIN };
/**
 * OpenCTI's grid (`DASHBOARD_GRID_CONFIG`: 62 px rows, 20 px margins), the one
 * custom dashboards use (`backend/app/services/dashboard_grid.py`), so an
 * OpenCTI widget sits where it sat in OpenCTI.
 */
export const OPENCTI_GRID_ROWS: GridRows = { rowHeight: 62, margin: 20 };

export interface WidgetSpec {
  /** Default place and size, in grid units. */
  x: number;
  y: number;
  w: number;
  h: number;
  /** Smallest size the widget still reads at. */
  minW: number;
  minH: number;
  /** Off the default layout: offered from the catalog (Add a widget) only.
   *  A widget a newer default replaced keeps its id, so no saved layout loses it. */
  hidden?: boolean;
  /** On the default layout, but a saved layout that does not name it (saved
   *  before it existed) gets it in the catalog, so that layout draws exactly as
   *  saved. */
  catalogWhenSkipped?: boolean;
}

/** A dashboard's widgets with their default places: its default layout is
 *  the dashboard as it was before it could be rearranged. */
export type WidgetRegistry<Id extends string> = Record<Id, WidgetSpec>;

/** Every widget id of `registry`, in default reading order. */
export function widgetIds<Id extends string>(registry: WidgetRegistry<Id>): Id[] {
  return Object.keys(registry) as Id[];
}

/** Lowest row a widget may start on (the backend's bound): every widget
 *  stacked at its tallest fits. */
export function maxGridRow<Id extends string>(registry: WidgetRegistry<Id>): number {
  return widgetIds(registry).length * MAX_WIDGET_HEIGHT;
}

function isWidget<Id extends string>(registry: WidgetRegistry<Id>, id: unknown): id is Id {
  return typeof id === "string" && Object.prototype.hasOwnProperty.call(registry, id);
}

/** A plain object, kept as saved (a widget's `settings`, the layout's
 *  `context`): the dashboard that owns it reads its values; anything else
 *  is dropped. */
function plainObject<T extends object>(value: unknown): T | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as T) : undefined;
}

const clampInt = (value: unknown, min: number, max: number, fallback: number): number => {
  const n = typeof value === "number" && Number.isFinite(value) ? Math.round(value) : fallback;
  return Math.max(min, Math.min(max, n));
};

/** A widget at its default size, placed in its default column at `y`. */
function defaultItem<Id extends string>(registry: WidgetRegistry<Id>, id: Id, y?: number): DashboardLayoutItem<Id> {
  const spec = registry[id];
  return { i: id, x: spec.x, y: Math.min(y ?? spec.y, maxGridRow(registry)), w: spec.w, h: spec.h };
}

/** The first row below every item. */
function bottom(items: readonly DashboardLayoutItem<string>[]): number {
  return items.reduce((max, item) => Math.max(max, item.y + item.h), 0);
}

/** The default layout: every widget at its default place, the ones marked
 *  `hidden` in the catalog. */
export function defaultLayout<Id extends string>(registry: WidgetRegistry<Id>): DashboardLayout<Id> {
  const ids = widgetIds(registry);
  return {
    items: ids.filter((id) => !registry[id].hidden).map((id) => defaultItem(registry, id)),
    hidden: ids.filter((id) => registry[id].hidden),
  };
}

/**
 * The layout to draw from a saved one: every known widget exactly once, shown
 * or hidden, each inside the grid and no smaller than its minimum.
 *
 * A saved layout can be stale or partial: it may name a widget this build no
 * longer has (dropped), name one twice (the first wins), or skip one added
 * after it was saved (shown at its default size below the others, the skipped
 * widgets keeping their default arrangement among themselves). A widget's
 * `settings` and the layout's `context` (CTEM command center) are kept when
 * saved and absent otherwise, so a layout saved without them draws unchanged.
 */
export function resolveLayout<Id extends string>(
  saved: { items?: unknown; hidden?: unknown; context?: unknown } | null | undefined,
  registry: WidgetRegistry<Id>,
): DashboardLayout<Id> {
  if (!saved) return defaultLayout(registry);
  const maxRow = maxGridRow(registry);
  const seen = new Set<Id>();
  const items: DashboardLayoutItem<Id>[] = [];
  for (const raw of Array.isArray(saved.items) ? saved.items : []) {
    const item = raw as Partial<Record<keyof DashboardLayoutItem, unknown>> | null;
    if (!item || !isWidget(registry, item.i) || seen.has(item.i)) continue;
    const spec = registry[item.i];
    const w = clampInt(item.w, spec.minW, GRID_COLUMNS, spec.w);
    const settings = plainObject<NonNullable<DashboardLayoutItem["settings"]>>(item.settings);
    items.push({
      i: item.i,
      x: clampInt(item.x, 0, GRID_COLUMNS - w, spec.x),
      y: clampInt(item.y, 0, maxRow, spec.y),
      w,
      h: clampInt(item.h, spec.minH, MAX_WIDGET_HEIGHT, spec.h),
      ...(settings ? { settings } : {}),
    });
    seen.add(item.i);
  }
  const hidden: Id[] = [];
  for (const id of Array.isArray(saved.hidden) ? saved.hidden : []) {
    if (!isWidget(registry, id) || seen.has(id)) continue;
    hidden.push(id);
    seen.add(id);
  }
  // The widgets it skips keep their default arrangement among themselves (KPI
  // cards side by side), moved down below the widgets it places; one off the
  // default layout goes to the catalog.
  const toCatalog = (id: Id) => Boolean(registry[id].hidden || registry[id].catalogWhenSkipped);
  for (const id of widgetIds(registry)) if (!seen.has(id) && toCatalog(id)) hidden.push(id);
  const missing = widgetIds(registry).filter((id) => !seen.has(id) && !toCatalog(id));
  if (missing.length > 0) {
    const offset = bottom(items) - Math.min(...missing.map((id) => registry[id].y));
    for (const id of missing) items.push(defaultItem(registry, id, registry[id].y + offset));
  }
  const context = plainObject<NonNullable<DashboardLayout["context"]>>(saved.context);
  return context ? { items, hidden, context } : { items, hidden };
}

/** `layout` with the grid's arrangement of the shown widgets (positions and
 *  sizes the grid reports, whole grid units, unknown ids dropped). */
export function withGridItems<Id extends string>(
  layout: DashboardLayout<Id>,
  grid: readonly { i: string; x: number; y: number; w: number; h: number }[],
  registry: WidgetRegistry<Id>,
): DashboardLayout<Id> {
  // The grid reports places only: each widget keeps its settings, the layout its context.
  const settings = new Map(layout.items.map((item) => [item.i as string, item.settings]));
  return resolveLayout(
    {
      items: grid.map(({ i, x, y, w, h }) => ({ i, x, y, w, h, settings: settings.get(i) })),
      hidden: layout.hidden,
      context: layout.context,
    },
    registry,
  );
}

/** `layout` without `id`, which goes to the hidden widgets. */
export function removeWidget<Id extends string>(layout: DashboardLayout<Id>, id: Id): DashboardLayout<Id> {
  if (layout.hidden.includes(id)) return layout;
  return { ...layout, items: layout.items.filter((item) => item.i !== id), hidden: [...layout.hidden, id] };
}

/** `layout` with the hidden widget `id` shown again, at its default size
 *  below the others. */
export function addWidget<Id extends string>(
  layout: DashboardLayout<Id>,
  id: Id,
  registry: WidgetRegistry<Id>,
): DashboardLayout<Id> {
  if (!layout.hidden.includes(id)) return layout;
  return {
    ...layout,
    items: [...layout.items, defaultItem(registry, id, bottom(layout.items))],
    hidden: layout.hidden.filter((hidden) => hidden !== id),
  };
}

/** The shown widgets in reading order: top to bottom, then left to right. */
export function readingOrder<Item extends DashboardLayoutItem<string>>(items: readonly Item[]): Item[] {
  return [...items].sort((a, b) => a.y - b.y || a.x - b.x);
}

/** Whether `layout` draws the dashboard's default layout (nothing to reset). */
export function isDefaultLayout<Id extends string>(layout: DashboardLayout<Id>, registry: WidgetRegistry<Id>): boolean {
  const base = defaultLayout(registry);
  if (layout.hidden.length !== base.hidden.length || layout.hidden.some((id) => !base.hidden.includes(id))) return false;
  if (layout.items.length !== base.items.length) return false;
  return layout.items.every((item) => {
    const spec = registry[item.i];
    return !spec.hidden && item.x === spec.x && item.y === spec.y && item.w === spec.w && item.h === spec.h;
  });
}
