/**
 * Unit tests for the Copilot dashboard grid - `yarn test:unit`.
 *
 * Its default layout is the dashboard as it was (every widget in place, inside
 * the grid, no overlap, the five KPI cards across one row), and its status
 * header draws in full until its size takes the orbit away.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { GRID_COLUMNS, defaultLayout, isDefaultLayout, resolveLayout, widgetIds } from "../../components/dashboard-grid/layout.ts";
import { COPILOT_WIDGETS } from "./copilot-dashboard-layout.ts";
import { copilotHeroDensity } from "./copilot-density.ts";

test("the dashboard has its thirteen widgets, each placed inside the grid at no less than its minimum", () => {
  const layout = defaultLayout(COPILOT_WIDGETS);
  assert.deepEqual(widgetIds(COPILOT_WIDGETS), [
    "hero",
    "kpi_agents",
    "kpi_conversations",
    "kpi_executions",
    "kpi_integrations",
    "kpi_documents",
    "agents",
    "conversations",
    "live_activity",
    "digests",
    "value",
    "value_drivers",
    "fleets",
  ]);
  for (const item of layout.items) {
    const spec = COPILOT_WIDGETS[item.i];
    assert.ok(item.x + item.w <= GRID_COLUMNS, item.i);
    assert.ok(item.w >= spec.minW && item.h >= spec.minH, item.i);
  }
  assert.equal(isDefaultLayout(layout, COPILOT_WIDGETS), true);
});

test("the Value row sits under every other widget, so a saved layout gains it without a move", () => {
  const items = defaultLayout(COPILOT_WIDGETS).items;
  const value = items.find((item) => item.i === "value");
  assert.ok(value);
  const row = new Set(["value", "value_drivers", "fleets"]);
  for (const item of items) if (!row.has(item.i)) assert.ok(item.y + item.h <= value.y, item.i);
});

test("the Fleets widget sits full width under the Value row, so a saved layout gains it without a move (#12063)", () => {
  const items = defaultLayout(COPILOT_WIDGETS).items;
  const fleets = items.find((item) => item.i === "fleets");
  const value = items.find((item) => item.i === "value");
  assert.ok(fleets && value);
  assert.equal(fleets.y, value.y + value.h);
  assert.deepEqual([fleets.x, fleets.w], [0, GRID_COLUMNS]);
  for (const item of items) if (item.i !== "fleets") assert.ok(item.y + item.h <= fleets.y, item.i);
});

test("the top value drivers balance the Value card: the right half of its row, the same height", () => {
  const items = defaultLayout(COPILOT_WIDGETS).items;
  const value = items.find((item) => item.i === "value");
  const drivers = items.find((item) => item.i === "value_drivers");
  assert.ok(value && drivers);
  assert.deepEqual([drivers.y, drivers.h, drivers.w], [value.y, value.h, value.w]);
  assert.equal(drivers.x, value.x + value.w);
  assert.equal(value.w + drivers.w, GRID_COLUMNS);
});

test("the default layout leaves no overlap and fills each row it uses", () => {
  const cells = new Set<string>();
  for (const item of defaultLayout(COPILOT_WIDGETS).items) {
    for (let x = item.x; x < item.x + item.w; x += 1) {
      for (let y = item.y; y < item.y + item.h; y += 1) {
        const cell = `${x},${y}`;
        assert.equal(cells.has(cell), false, `${item.i} overlaps at ${cell}`);
        cells.add(cell);
      }
    }
  }
  const kpis = defaultLayout(COPILOT_WIDGETS).items.filter((item) => item.i.startsWith("kpi_"));
  assert.equal(new Set(kpis.map((item) => item.y)).size, 1);
  assert.equal(kpis.reduce((width, item) => width + item.w, 0), GRID_COLUMNS);
});

test("a command center widget in a saved Copilot layout is dropped", () => {
  const layout = resolveLayout({ items: [{ i: "stage_scoping", x: 0, y: 0, w: 4, h: 8 }], hidden: ["briefing"] }, COPILOT_WIDGETS);
  assert.equal(layout.items.length, widgetIds(COPILOT_WIDGETS).length);
  assert.deepEqual(layout.hidden, []);
});

test("the status header draws in full out of the grid and at its default size", () => {
  const full = { orbit: true, subtitle: "full", ticker: true };
  assert.deepEqual(copilotHeroDensity(null), full);
  const { w, h } = COPILOT_WIDGETS.hero;
  assert.deepEqual(copilotHeroDensity({ cols: w, rows: h }), full);
});

test("a small status header swaps its orbit for the count and keeps the latest runs", () => {
  const { minW, minH } = COPILOT_WIDGETS.hero;
  assert.deepEqual(copilotHeroDensity({ cols: minW, rows: minH }), { orbit: false, subtitle: "clamped", ticker: false });
  assert.deepEqual(copilotHeroDensity({ cols: 12, rows: 7 }), { orbit: false, subtitle: "full", ticker: true });
  assert.equal(copilotHeroDensity({ cols: 12, rows: 6 }).ticker, false);
  assert.equal(copilotHeroDensity({ cols: 8, rows: 8 }).orbit, true);
});
