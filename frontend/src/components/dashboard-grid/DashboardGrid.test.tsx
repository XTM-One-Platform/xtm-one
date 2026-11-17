// Building a dashboard on the grid: what each edit hands back to be saved, and
// what the person can reach while editing or not. The command center's widgets
// serve as the registry under test.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, within } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { CTEM_WIDGETS } from "@/pages/xtm-one/ctem/dashboard-layout";
import type { CtemDashboardLayout, CtemWidgetId } from "@/types";
import { DashboardGrid } from "./DashboardGrid";
import { defaultLayout as gridDefaultLayout, removeWidget, widgetIds, type WidgetSpec } from "./layout";

// The command center's widgets, every one shown by default (the widgets the
// default leaves in the catalog included), serve as the registry under test.
// The v1 grid keeps its places (the briefing its v1 one, under the KPI row,
// rows 0 to 111) and the v2 sections are stacked below it in their reading
// order with no gap, so no two overlap and nothing is left for compaction to
// close before a move.
const V2_SECTIONS = ["value_band", "act_now", "cases", "lifecycle_band", "stats_coverage", "done_for_you", "fleets_row", "prioritization_funnel"] as const;
const V1_PLACES: Partial<Record<string, Partial<WidgetSpec>>> = { briefing: { y: 16, h: 8 } };
const V2_PLACES: Record<string, Partial<WidgetSpec>> = {};
V2_SECTIONS.reduce((y, id) => {
  V2_PLACES[id] = { x: 0, y };
  return y + CTEM_WIDGETS[id].h;
}, 111);
const REGISTRY = Object.fromEntries(
  Object.entries(CTEM_WIDGETS).map(([id, { hidden: _hidden, catalogWhenSkipped: _catalog, ...spec }]) => [id, { ...spec, ...V1_PLACES[id], ...V2_PLACES[id] }]),
) as Record<CtemWidgetId, WidgetSpec>;
const CTEM_WIDGET_IDS = widgetIds(REGISTRY);
const defaultLayout = () => gridDefaultLayout(REGISTRY);

// happy-dom lays nothing out: the grid's container width is what the test says.
// The real grid is drawn; the props it was last given are kept, so a test can
// end a drag or a resize the way the grid reports one.
const container = vi.hoisted(() => ({ width: 1280 }));
const gridProps = vi.hoisted(() => ({ last: null as null | Record<string, unknown> }));
vi.mock("react-grid-layout", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-grid-layout")>();
  const Grid = actual.default;
  return {
    ...actual,
    default: (props: React.ComponentProps<typeof Grid>) => {
      gridProps.last = props as unknown as Record<string, unknown>;
      return <Grid {...props} />;
    },
    useContainerWidth: () => ({ width: container.width, mounted: true, containerRef: { current: null } }),
  };
});

type GridItem = { i: string; x: number; y: number; w: number; h: number; minW?: number; minH?: number; maxH?: number };

/** The keyboard handle of a widget in edit mode. */
const handle = (id: string) =>
  screen.getByRole("group", { name: `Label ${id}: the arrow keys move it, Shift and the arrow keys resize it` });

/** End a drag (or a resize) with the grid reporting `layout`. */
function stop(callback: "onDragStop" | "onResizeStop", layout: GridItem[]) {
  const handler = gridProps.last?.[callback] as ((layout: GridItem[]) => void) | undefined;
  if (!handler) throw new Error(`the grid was given no ${callback}`);
  act(() => handler(layout));
}

const labels = Object.fromEntries(CTEM_WIDGET_IDS.map((id) => [id, `Label ${id}`])) as Record<CtemWidgetId, string>;
const widgets = Object.fromEntries(
  CTEM_WIDGET_IDS.map((id) => [id, <button key={id} type="button">{`Open ${id}`}</button>]),
) as Record<CtemWidgetId, React.ReactNode>;

function grid({ layout = defaultLayout(), editing = false }: { layout?: CtemDashboardLayout; editing?: boolean } = {}) {
  const onChange = vi.fn();
  const onDone = vi.fn();
  renderWithProviders(
    <DashboardGrid
      registry={REGISTRY}
      layout={layout}
      widgets={widgets}
      labels={labels}
      title="Customize your command center"
      editing={editing}
      onChange={onChange}
      onDone={onDone}
    />,
  );
  return { onChange, onDone };
}

beforeEach(() => {
  container.width = 1280;
  gridProps.last = null;
});

describe("DashboardGrid", () => {
  it("draws every shown widget, and none of the hidden ones", () => {
    grid({ layout: removeWidget(defaultLayout(), "briefing") });
    expect(screen.getAllByRole("button", { name: /^Open / })).toHaveLength(CTEM_WIDGET_IDS.length - 1);
    expect(screen.queryByRole("button", { name: "Open briefing" })).toBeNull();
  });

  it("offers no edit control out of edit mode, and keeps the widgets usable", () => {
    grid();
    expect(screen.queryByRole("button", { name: /^Remove / })).toBeNull();
    expect(screen.queryByRole("button", { name: "Done" })).toBeNull();
    expect(screen.getByRole("button", { name: "Open hero" }).closest("[inert]")).toBeNull();
  });

  it("makes the widgets inert while editing, so a drag never opens what it starts on", () => {
    grid({ editing: true });
    expect(screen.getByRole("button", { name: "Open hero" }).closest("[inert]")).not.toBeNull();
  });

  it("removes a widget into the hidden ones", () => {
    const { onChange } = grid({ editing: true });
    fireEvent.click(screen.getByRole("button", { name: "Remove Label briefing" }));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(removeWidget(defaultLayout(), "briefing"));
  });

  it("adds a removed widget back from the catalog, below the others", () => {
    const layout = removeWidget(defaultLayout(), "briefing");
    const { onChange } = grid({ layout, editing: true });
    // The design system's menu opens on pointer down, as Radix menus do, not on click.
    fireEvent.pointerDown(screen.getByRole("button", { name: "Add a widget" }), { button: 0, ctrlKey: false });
    fireEvent.click(within(screen.getByRole("menu")).getByRole("menuitem", { name: "Label briefing" }));
    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as CtemDashboardLayout;
    expect(next.hidden).toEqual([]);
    const bottom = Math.max(...layout.items.map((item) => item.y + item.h));
    expect(next.items[next.items.length - 1]).toEqual({ i: "briefing", x: 0, y: bottom, w: 12, h: 8 });
  });

  it("says every widget is shown when none was removed", () => {
    grid({ editing: true });
    expect(screen.queryByRole("button", { name: "Add a widget" })).toBeNull();
    expect(screen.getByText("Every widget is shown")).toBeTruthy();
  });

  it("resets to the default layout, which it cannot do from the default layout", () => {
    const { onChange } = grid({ layout: removeWidget(defaultLayout(), "hero"), editing: true });
    fireEvent.click(screen.getByRole("button", { name: "Reset to default" }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("disables the reset on the default layout", () => {
    grid({ editing: true });
    expect(screen.getByRole("button", { name: "Reset to default" })).toHaveProperty("disabled", true);
  });

  it("leaves edit mode with Done, saving nothing", () => {
    const { onChange, onDone } = grid({ editing: true });
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("stacks the widgets in reading order when the grid has no room, with no remove control", () => {
    container.width = 500;
    const layout = defaultLayout();
    layout.items = layout.items.map((item) => (item.i === "digests" ? { ...item, y: 0, x: 0 } : { ...item, y: item.y + 9 }));
    grid({ layout, editing: true });
    const opened = screen.getAllByRole("button", { name: /^Open / }).map((button) => button.textContent);
    expect(opened[0]).toBe("Open digests");
    expect(opened[1]).toBe("Open hero");
    expect(screen.queryByRole("button", { name: /^Remove / })).toBeNull();
    expect(screen.getByText(/Widen the window to rearrange the widgets\./)).toBeTruthy();
  });
  it("saves where a widget was dragged, as whole grid units without the grid's own fields", () => {
    const layout = removeWidget(defaultLayout(), "briefing");
    const { onChange } = grid({ layout, editing: true });
    const moved = (gridProps.last!.layout as GridItem[]).map((item) =>
      item.i === "digests" ? { ...item, x: 0, y: 0, w: 6, moved: true } : item,
    );
    stop("onDragStop", moved);
    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as CtemDashboardLayout;
    expect(next.hidden).toEqual(["briefing"]);
    expect(next.items.find((item) => item.i === "digests")).toEqual({ i: "digests", x: 0, y: 0, w: 6, h: 9 });
    expect(next.items.every((item) => Object.keys(item).sort().join() === "h,i,w,x,y")).toBe(true);
  });

  it("saves a resized widget", () => {
    const { onChange } = grid({ editing: true });
    const resized = (gridProps.last!.layout as GridItem[]).map((item) => (item.i === "hero" ? { ...item, w: 8, h: 14 } : item));
    stop("onResizeStop", resized);
    expect(onChange).toHaveBeenCalledTimes(1);
    const hero = (onChange.mock.calls[0][0] as CtemDashboardLayout).items.find((item) => item.i === "hero");
    expect(hero).toEqual({ i: "hero", x: 0, y: 0, w: 8, h: 14 });
  });

  it("saves nothing when a drag ends where it started", () => {
    const { onChange } = grid({ editing: true });
    stop("onDragStop", [...(gridProps.last!.layout as GridItem[])].reverse());
    expect(onChange).not.toHaveBeenCalled();
  });

  it("gives the grid each widget's minimum size, and lets nothing move out of edit mode", () => {
    grid();
    const hero = (gridProps.last!.layout as GridItem[]).find((item) => item.i === "hero");
    expect(hero).toMatchObject({ minW: 6, minH: 9, maxH: 40 });
    expect(gridProps.last?.dragConfig).toMatchObject({ enabled: false });
    expect(gridProps.last?.resizeConfig).toMatchObject({ enabled: false });
  });
  it("resizes a widget with Shift and the arrow keys, and says where it is", () => {
    const { onChange } = grid({ editing: true });
    fireEvent.keyDown(handle("hero"), { key: "ArrowLeft", shiftKey: true });
    expect(onChange).toHaveBeenCalledTimes(1);
    const hero = (onChange.mock.calls[0][0] as CtemDashboardLayout).items.find((item) => item.i === "hero");
    const { h } = CTEM_WIDGETS.hero;
    expect(hero).toEqual({ i: "hero", x: 0, y: 0, w: 11, h });
    expect(screen.getByText(`Label hero: column 1, row 1, 11 columns wide, ${h} rows high`)).toBeTruthy();
  });

  it("never resizes a widget under its minimum or past the grid", () => {
    const { onChange } = grid({ editing: true });
    fireEvent.keyDown(handle("kpi_posture"), { key: "ArrowLeft", shiftKey: true });
    fireEvent.keyDown(handle("hero"), { key: "ArrowRight", shiftKey: true });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("moves a widget down past the one below it with the arrow keys", () => {
    const { onChange } = grid({ editing: true });
    fireEvent.keyDown(handle("hero"), { key: "ArrowDown" });
    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as CtemDashboardLayout;
    const hero = next.items.find((item) => item.i === "hero");
    expect(hero?.y).toBeGreaterThan(0);
    expect(next.items.filter((item) => item.y === 0).map((item) => item.i)).not.toContain("hero");
  });

  it("does nothing for a move off the grid, or for a key that is not an arrow", () => {
    const { onChange } = grid({ editing: true });
    fireEvent.keyDown(handle("hero"), { key: "ArrowUp" });
    fireEvent.keyDown(handle("hero"), { key: "ArrowLeft" });
    fireEvent.keyDown(handle("hero"), { key: "Enter" });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("offers no keyboard handle out of edit mode", () => {
    grid();
    expect(screen.queryByRole("group", { name: /the arrow keys move it/ })).toBeNull();
  });
});
