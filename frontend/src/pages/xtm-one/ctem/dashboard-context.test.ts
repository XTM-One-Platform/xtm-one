// SPDX-License-Identifier: Apache-2.0
/**
 * The CTEM dashboard context and widget settings (dev-docs/ctem-stage-viz.md
 * 8.1 to 8.3, slice S2f): a layout saved before them loads and saves back
 * unchanged, a value equal to the default is never stored, a framework the
 * data no longer carries falls back to the one with the most data.
 */
import { describe, expect, it } from "vitest";
import * as grid from "@/components/dashboard-grid/layout";
import type { CtemOutcomeMatrix, CtemWidgetId, DashboardLayout } from "@/types";
import { CTEM_WIDGETS } from "./dashboard-layout";
import {
  DEFAULT_PERIOD,
  contextIsDefault,
  defaultFramework,
  effectiveFramework,
  overriddenWidgets,
  readContext,
  savedLayoutFramework,
  widgetPeriod,
  withContext,
  withDefaultContext,
  withWidgetSettings,
} from "./dashboard-context";

const registry = CTEM_WIDGETS as unknown as grid.WidgetRegistry<CtemWidgetId>;
const resolve = (saved: Parameters<typeof grid.resolveLayout>[0]) => grid.resolveLayout(saved, registry);

function chain(id: string, count: number, phases = 1) {
  return { id, label: id, count, default: false, groups: Array.from({ length: phases }, (_, i) => ({ id: `p${i}` })) };
}
const matrix = (...chains: ReturnType<typeof chain>[]) => ({ chains }) as unknown as CtemOutcomeMatrix;

describe("a layout saved before the context", () => {
  const saved = grid.defaultLayout(registry);
  const before = JSON.parse(JSON.stringify(saved));

  it("loads unchanged: no context, no widget settings, every place kept", () => {
    const layout = resolve(before);
    expect(layout).toEqual(before);
    expect("context" in layout).toBe(false);
    expect(layout.items.every((item) => !("settings" in item))).toBe(true);
  });

  it("reads the defaults", () => {
    expect(readContext(resolve(before))).toEqual({ framework: null, period: DEFAULT_PERIOD });
    expect(contextIsDefault(resolve(before))).toBe(true);
    expect(widgetPeriod(resolve(before), "exposure_stats")).toBe(DEFAULT_PERIOD);
  });

  it("saves back unchanged after a move or a widget removed and added", () => {
    const layout = resolve(before);
    const moved = grid.withGridItems(layout, layout.items, registry);
    expect(moved).toEqual(before);
    const roundTrip = grid.addWidget(grid.removeWidget(layout, "briefing"), "briefing", registry);
    expect("context" in roundTrip).toBe(false);
  });
});

describe("the context", () => {
  const base = grid.defaultLayout(registry);

  it("keeps the context and the widget settings through a load, a move and a remove", () => {
    const set = withWidgetSettings(withContext(base, { framework: "disarm", period: 7 }), "hero", { period: 90 });
    const loaded = resolve(JSON.parse(JSON.stringify(set)));
    expect(loaded.context).toEqual({ framework: "disarm", period: 7 });
    expect(loaded.items.find((item) => item.i === "hero")?.settings).toEqual({ period: 90 });
    const moved = grid.withGridItems(loaded, loaded.items, registry);
    expect(moved).toEqual(loaded);
    expect(grid.removeWidget(loaded, "briefing").context).toEqual({ framework: "disarm", period: 7 });
  });

  it("never stores a default value, and reset removes the key", () => {
    expect("context" in withContext(base, { period: DEFAULT_PERIOD })).toBe(false);
    const set = withContext(base, { period: 90 });
    expect(set.context).toEqual({ period: 90 });
    expect("context" in withContext(set, { period: null })).toBe(false);
    expect("context" in withDefaultContext(withContext(set, { framework: "x" }))).toBe(false);
  });

  it("reads a period not offered as the default", () => {
    const odd = { ...base, context: { period: 14 } } as unknown as DashboardLayout<CtemWidgetId>;
    expect(readContext(odd).period).toBe(DEFAULT_PERIOD);
  });
});

describe("the widget settings", () => {
  const base = withContext(grid.defaultLayout(registry), { period: 7 });

  it("a widget reads its own period, else the dashboard's", () => {
    const own = withWidgetSettings(base, "hero", { period: 90 });
    expect(widgetPeriod(own, "hero")).toBe(90);
    expect(widgetPeriod(own, "value_band")).toBe(7);
    expect(overriddenWidgets(own)).toEqual([{ id: "hero", period: 90 }]);
  });

  it("'From dashboard' removes the widget's settings", () => {
    const own = withWidgetSettings(base, "hero", { period: 90 });
    const back = withWidgetSettings(own, "hero", { period: null });
    expect(back.items.find((item) => item.i === "hero")).not.toHaveProperty("settings");
    expect(overriddenWidgets(back)).toEqual([]);
  });
});

describe("the framework", () => {
  it("defaults to the chain with the most data, a tie to the one with more phases", () => {
    expect(defaultFramework(matrix(chain("a", 3), chain("b", 9), chain("c", 0)))).toBe("b");
    expect(defaultFramework(matrix(chain("a", 4, 2), chain("b", 4, 5)))).toBe("b");
    expect(defaultFramework(matrix(chain("a", 0)))).toBeNull();
    expect(defaultFramework(undefined)).toBeNull();
  });

  it("falls back when the saved framework is no longer in the data", () => {
    const layout = withContext(grid.defaultLayout(registry), { framework: "gone" });
    expect(effectiveFramework(layout, matrix(chain("a", 3)))).toEqual({ id: "a", fellBack: true });
    const kept = withContext(grid.defaultLayout(registry), { framework: "a" });
    expect(effectiveFramework(kept, matrix(chain("a", 3), chain("b", 9)))).toEqual({ id: "a", fellBack: false });
  });

  it("is what the Validation stage page opens on, from the saved layout (UX round 1, B2)", () => {
    const m = matrix(chain("a", 3), chain("b", 9));
    expect(savedLayoutFramework({ context: { framework: "a" } }, m)).toBe("a");
    expect(savedLayoutFramework({ context: { framework: "gone" } }, m)).toBe("b");
    expect(savedLayoutFramework({}, m)).toBe("b");
    expect(savedLayoutFramework(null, m)).toBe("b");
    expect(savedLayoutFramework({ context: "junk" }, m)).toBe("b");
  });
});
