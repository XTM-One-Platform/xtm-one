/**
 * Unit tests for the dashboard grid layout - `yarn test:unit`.
 *
 * A saved layout is drawn with every known widget exactly once, shown or
 * hidden, inside the grid and no smaller than its minimum, whatever the saved
 * value holds; removing and adding a widget only moves it between the two.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { CTEM_WIDGETS } from "../../pages/xtm-one/ctem/dashboard-layout.ts";
import type { DashboardLayout } from "@/types";
import * as grid from "./layout.ts";

// The command center's widgets, every one shown by default, serve as the
// registry under test; the `hidden` flag is tested on its own below. The v1
// grid keeps its places (the briefing its v1 one, under the KPI row) and the
// v2 sections go below it, so no two overlap.
type Id = keyof typeof CTEM_WIDGETS;
const V2_SECTIONS = new Set<string>(["value_band", "act_now", "done_for_you", "lifecycle_band", "stats_coverage", "fleets_row", "prioritization_funnel", "cases"]);
const V1_PLACES: Partial<Record<string, Partial<grid.WidgetSpec>>> = { briefing: { y: 16, h: 8 } };
const registry = Object.fromEntries(
  Object.entries(CTEM_WIDGETS).map(([id, { hidden: _hidden, catalogWhenSkipped: _catalog, ...spec }]) => [
    id,
    V2_SECTIONS.has(id) ? { ...spec, y: spec.y + 111 } : { ...spec, ...V1_PLACES[id] },
  ]),
) as Record<Id, grid.WidgetSpec>;
const { GRID_COLUMNS, MAX_WIDGET_HEIGHT, readingOrder, removeWidget } = grid;
const CTEM_WIDGET_IDS = grid.widgetIds(registry);
const MAX_GRID_ROW = grid.maxGridRow(registry);
const defaultLayout = () => grid.defaultLayout(registry);
const resolveLayout = (saved: Parameters<typeof grid.resolveLayout>[0]) => grid.resolveLayout(saved, registry);
const addWidget = (layout: DashboardLayout<Id>, id: Id) => grid.addWidget(layout, id, registry);
const withGridItems = (layout: DashboardLayout<Id>, items: Parameters<typeof grid.withGridItems>[1]) =>
  grid.withGridItems(layout, items, registry);
const isDefaultLayout = (layout: DashboardLayout<Id>) => grid.isDefaultLayout(layout, registry);

const ids = (layout: { items: { i: string }[] }) => layout.items.map((item) => item.i);

test("the default layout shows every widget, in its default place, inside the grid", () => {
  const layout = defaultLayout();
  assert.equal(CTEM_WIDGET_IDS.length, 30);
  assert.deepEqual(ids(layout), CTEM_WIDGET_IDS);
  assert.deepEqual(layout.hidden, []);
  for (const item of layout.items) {
    const spec = registry[item.i];
    assert.deepEqual(item, { i: item.i, x: spec.x, y: spec.y, w: spec.w, h: spec.h });
    assert.ok(item.x + item.w <= GRID_COLUMNS, item.i);
    assert.ok(item.w >= spec.minW && item.h >= spec.minH, item.i);
  }
  assert.equal(isDefaultLayout(layout), true);
});

test("the default layout leaves no overlap", () => {
  const cells = new Set<string>();
  for (const item of defaultLayout().items) {
    for (let x = item.x; x < item.x + item.w; x += 1) {
      for (let y = item.y; y < item.y + item.h; y += 1) {
        const cell = `${x},${y}`;
        assert.equal(cells.has(cell), false, `${item.i} overlaps at ${cell}`);
        cells.add(cell);
      }
    }
  }
});

test("no saved layout draws the default one", () => {
  assert.deepEqual(resolveLayout(null), defaultLayout());
  assert.deepEqual(resolveLayout(undefined), defaultLayout());
});

test("a saved layout is drawn as saved, with the hidden widgets kept hidden", () => {
  const saved = removeWidget(defaultLayout(), "briefing");
  const moved = { ...saved, items: saved.items.map((item) => (item.i === "digests" ? { ...item, y: 0, w: 6 } : item)) };
  assert.deepEqual(resolveLayout(moved), moved);
});

test("unknown and repeated widgets are dropped, a widget shown and hidden stays shown", () => {
  const layout = resolveLayout({
    items: [
      { i: "hero", x: 0, y: 0, w: 12, h: 11 },
      { i: "retired", x: 0, y: 0, w: 4, h: 4 },
      { i: "hero", x: 0, y: 30, w: 6, h: 9 },
    ],
    hidden: [...CTEM_WIDGET_IDS.filter((id) => id !== "hero"), "hero", "retired"],
  });
  assert.deepEqual(layout.items, [{ i: "hero", x: 0, y: 0, w: 12, h: 11 }]);
  assert.deepEqual(layout.hidden, CTEM_WIDGET_IDS.filter((id) => id !== "hero"));
});

test("widgets the saved layout skips come back below the others, in their default arrangement", () => {
  const layout = resolveLayout({ items: [{ i: "digests", x: 0, y: 0, w: 12, h: 9 }], hidden: [] });
  const kpis = layout.items.filter((item) => item.i.startsWith("kpi_"));
  // The six KPI cards stay side by side on one row, the hero above them.
  assert.equal(new Set(kpis.map((item) => item.y)).size, 1);
  assert.deepEqual(kpis.map((item) => item.x), [0, 2, 4, 6, 8, 10]);
  const hero = layout.items.find((item) => item.i === "hero");
  assert.deepEqual(hero, { i: "hero", x: 0, y: 9, w: 12, h: CTEM_WIDGETS.hero.h });
  assert.equal(kpis[0].y, 9 + CTEM_WIDGETS.kpi_posture.y);
});

test("a single widget the saved layout skips comes back at its default size below the others", () => {
  const layout = resolveLayout({ items: [{ i: "hero", x: 0, y: 0, w: 12, h: 11 }], hidden: CTEM_WIDGET_IDS.filter((id) => id !== "hero" && id !== "digests") });
  const digests = layout.items.find((item) => item.i === "digests");
  assert.deepEqual(digests, { i: "digests", x: 0, y: 11, w: 12, h: CTEM_WIDGETS.digests.h });
});

test("a saved item out of the grid or under its minimum is brought back inside", () => {
  const layout = resolveLayout({
    items: [{ i: "hero", x: 9, y: -4, w: 2, h: 99 }],
    hidden: CTEM_WIDGET_IDS.filter((id) => id !== "hero"),
  });
  const { minW } = CTEM_WIDGETS.hero;
  assert.deepEqual(layout.items, [{ i: "hero", x: GRID_COLUMNS - minW, y: 0, w: minW, h: MAX_WIDGET_HEIGHT }]);
});

test("garbage in a saved layout falls back to defaults rather than throwing", () => {
  assert.deepEqual(resolveLayout({ items: "hero", hidden: 3 }), defaultLayout());
  const layout = resolveLayout({ items: [null, { i: "hero", x: "a", y: Number.NaN, w: null, h: undefined }], hidden: [] });
  const spec = CTEM_WIDGETS.hero;
  assert.deepEqual(layout.items[0], { i: "hero", x: spec.x, y: spec.y, w: spec.w, h: spec.h });
  assert.equal(layout.items.length, CTEM_WIDGET_IDS.length);
});

test("a row past the backend's bound is pulled back to it", () => {
  const layout = resolveLayout({ items: [{ i: "hero", x: 0, y: MAX_GRID_ROW + 50, w: 12, h: 11 }], hidden: [] });
  assert.equal(layout.items[0].y, MAX_GRID_ROW);
  assert.ok(layout.items.every((item) => item.y <= MAX_GRID_ROW));
});

test("removing a widget hides it, adding it back places it below the others", () => {
  const removed = removeWidget(defaultLayout(), "hero");
  assert.equal(ids(removed).includes("hero"), false);
  assert.deepEqual(removed.hidden, ["hero"]);
  assert.equal(isDefaultLayout(removed), false);

  const added = addWidget(removed, "hero");
  assert.deepEqual(added.hidden, []);
  const bottom = Math.max(...removed.items.map((item) => item.y + item.h));
  assert.deepEqual(added.items[added.items.length - 1], { i: "hero", x: 0, y: bottom, w: 12, h: CTEM_WIDGETS.hero.h });
});

test("removing a hidden widget or adding a shown one changes nothing", () => {
  const removed = removeWidget(defaultLayout(), "hero");
  assert.equal(removeWidget(removed, "hero"), removed);
  const layout = defaultLayout();
  assert.equal(addWidget(layout, "hero"), layout);
});

test("the grid's arrangement keeps the hidden widgets and drops what the grid adds", () => {
  const layout = removeWidget(defaultLayout(), "briefing");
  const grid = layout.items.map((item) => ({ ...item, minW: 1, minH: 1, moved: false, static: false }));
  grid[0] = { ...grid[0], y: 40 };
  const next = withGridItems(layout, grid);
  assert.deepEqual(next.hidden, ["briefing"]);
  assert.deepEqual(next.items[0], { i: "hero", x: 0, y: 40, w: 12, h: CTEM_WIDGETS.hero.h });
  assert.deepEqual(Object.keys(next.items[0]).sort(), ["h", "i", "w", "x", "y"]);
});

test("reading order is top to bottom, then left to right", () => {
  const order = readingOrder([
    { i: "digests", x: 6, y: 4, w: 6, h: 6 },
    { i: "hero", x: 0, y: 4, w: 6, h: 6 },
    { i: "briefing", x: 0, y: 0, w: 12, h: 4 },
  ]);
  assert.deepEqual(order.map((item) => item.i), ["briefing", "hero", "digests"]);
});

test("a widget marked hidden waits in the catalog: off the default layout, never pushed onto a saved one", () => {
  const small = {
    a: { x: 0, y: 0, w: 12, h: 4, minW: 4, minH: 2 },
    old: { x: 0, y: 0, w: 6, h: 4, minW: 4, minH: 2, hidden: true },
    b: { x: 0, y: 4, w: 12, h: 4, minW: 4, minH: 2 },
  } satisfies grid.WidgetRegistry<"a" | "old" | "b">;
  const base = grid.defaultLayout(small);
  assert.deepEqual(ids(base), ["a", "b"]);
  assert.deepEqual(base.hidden, ["old"]);
  assert.equal(grid.isDefaultLayout(base, small), true);
  // Added back: shown below the others, and the layout is no longer the default.
  const added = grid.addWidget(base, "old", small);
  assert.deepEqual(added.items[added.items.length - 1], { i: "old", x: 0, y: 8, w: 6, h: 4 });
  assert.equal(grid.isDefaultLayout(added, small), false);
  // A saved layout that names it keeps it where it was saved.
  const saved = grid.resolveLayout({ items: [{ i: "old", x: 0, y: 0, w: 6, h: 4 }], hidden: [] }, small);
  assert.deepEqual(ids(saved), ["old", "a", "b"]);
  // A saved layout that skips it does not grow it: it goes to the catalog.
  const skipped = grid.resolveLayout({ items: [{ i: "a", x: 0, y: 0, w: 12, h: 4 }], hidden: [] }, small);
  assert.deepEqual(ids(skipped), ["a", "b"]);
  assert.deepEqual(skipped.hidden, ["old"]);
});
