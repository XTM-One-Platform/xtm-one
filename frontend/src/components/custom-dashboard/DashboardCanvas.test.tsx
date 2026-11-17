// A canvas tab as a viewer meets it (elements and widgets at their pixels,
// stacked in order, an image said by its alternative text) and as the studio
// edits it: a click picks, Shift adds, a drag moves, a handle resizes, a line
// end snaps to a block's side, the keys move, resize, remove and forward the
// rest. Each gesture is one change of the definition, checked on the
// definition it produces.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

import { DashboardCanvas, type CanvasEditing } from "./DashboardCanvas";
import { DashboardLiveContext } from "./live";
import { normalizeDashboard, type DashboardWidget } from "./spec";

vi.mock("@/hooks/useAuthenticatedSrc", () => ({
  useAuthenticatedSrc: (url: string | null) => (url ? `blob:${url}` : null),
}));

const IMAGE = "3f2b8c1e-7d4a-4c55-9a8e-0b1c2d3e4f50";

type Definition = Record<string, unknown>;

function definition(): Definition {
  return {
    version: 2,
    title: "Ops room",
    visualizations: {
      box: { type: "xtm.shape.rectangle", options: { fillColor: "#112233", rx: 6 } },
      dot: { type: "xtm.shape.ellipse", options: {} },
      arrow: { type: "xtm.shape.line", options: { toArrow: true } },
      map: { type: "xtm.image", options: { fileId: IMAGE, alt: "The network" } },
      notes: { type: "xtm.markdown", options: { markdown: "## Ops\n\nAll **green**" } },
      brief: { type: "xtm.text", title: "Brief", options: { body: "Hello from a widget" } },
    },
    layout: {
      tabs: { items: [{ layoutId: "canvas", label: "Canvas" }] },
      layoutDefinitions: {
        canvas: {
          type: "absolute",
          options: { width: 1000, height: 600, backgroundColor: "#000000" },
          structure: [
            { item: "box", type: "block", position: { x: 10, y: 20, w: 200, h: 100 } },
            { item: "dot", type: "block", position: { x: 400, y: 20, w: 80, h: 80 } },
            { item: "arrow", type: "line", position: { from: { item: "box", port: "e" }, to: { x: 300, y: 300 } } },
            { item: "map", type: "block", position: { x: 10, y: 300, w: 300, h: 200 } },
            { item: "notes", type: "block", position: { x: 600, y: 300, w: 300, h: 120 } },
            { item: "brief", type: "block", position: { x: 600, y: 450, w: 300, h: 120 } },
          ],
        },
      },
    },
  };
}

function draw(editing?: Partial<CanvasEditing>) {
  const tab = normalizeDashboard(definition()).tabs[0];
  const widgets = new Map(tab.widgets.map((w) => [w.id, w]));
  const changes: Definition[] = [];
  const binding: CanvasEditing | null = editing
    ? {
        layoutId: "canvas",
        selection: [],
        onSelect: vi.fn(),
        onChange: (update) => changes.push(update(definition())),
        onShortcut: vi.fn(),
        zoom: 1,
        ...editing,
      }
    : null;
  renderWithProviders(
    <DashboardLiveContext.Provider value={{ dashboardId: "d1", hideOpenLinks: false }}>
      <DashboardCanvas
        canvas={tab.canvas!}
        widgets={widgets}
        renderWidget={(widget: DashboardWidget) => <div data-testid={`widget-${widget.id}`}>{widget.title}</div>}
        editing={binding}
        dashboardId="d1"
        label="Canvas"
      />
    </DashboardLiveContext.Provider>,
  );
  return { binding, changes };
}

const placed = (def: Definition, id: string) =>
  ((def.layout as any).layoutDefinitions.canvas.structure as { item: string; position: unknown }[]).find((e) => e.item === id)?.position; // eslint-disable-line @typescript-eslint/no-explicit-any

describe("DashboardCanvas", () => {
  it("draws its elements and widgets at their pixels, in stacking order, over its background", () => {
    draw();
    const surface = document.querySelector("[data-canvas-surface]") as HTMLElement;
    expect(surface.style.width).toBe("1000px");
    expect(surface.style.backgroundColor).toBe("#000000");
    const order = [...surface.querySelectorAll("[data-canvas-item]")].map((node) => node.getAttribute("data-canvas-item"));
    expect(order).toEqual(["box", "dot", "arrow", "map", "notes", "brief"]);
    const box = surface.querySelector('[data-canvas-item="box"]') as HTMLElement;
    expect([box.style.left, box.style.top, box.style.width, box.style.height]).toEqual(["10px", "20px", "200px", "100px"]);
    expect(screen.getByAltText("The network").getAttribute("src")).toBe(`blob:/api/v1/custom-dashboards/d1/assets/${IMAGE}`);
    expect(screen.getByRole("heading", { name: "Ops" })).toBeTruthy();
    expect(screen.getByText("green").tagName).toBe("STRONG");
    expect(screen.getByTestId("widget-brief").textContent).toBe("Brief");
    // A viewer gets no handles and no pickers.
    expect(document.querySelectorAll("[data-canvas-handle]").length).toBe(0);
    expect(screen.queryByRole("button", { name: "Rectangle" })).toBeNull();
  });

  it("draws a line held to a block from the middle of its side, with its arrow", () => {
    draw();
    const line = document.querySelector('[data-canvas-line="arrow"]') as SVGElement;
    const path = line.querySelector(":scope > path") as SVGPathElement;
    // From the box's east port (210, 70) to (300, 300), in a box padded by 12 px.
    expect([line.style.left, line.style.top]).toEqual(["198px", "58px"]);
    expect(path.getAttribute("d")).toBe("M 12 12 L 102 242");
    expect(path.getAttribute("marker-end")).toMatch(/^url\(#canvas-arrow-arrow-end\)$/);
    expect(path.getAttribute("marker-start")).toBeNull();
  });

  it("picks an item by a click, adds one with Shift, and clears the selection on the empty canvas", () => {
    const { binding } = draw({ selection: ["box"] });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Ellipse" }), { button: 0, clientX: 5, clientY: 5 });
    expect(binding!.onSelect).toHaveBeenLastCalledWith(["dot"]);
    fireEvent.pointerUp(screen.getByTestId("dashboard.canvas"), { clientX: 5, clientY: 5 });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Image" }), { button: 0, shiftKey: true, clientX: 5, clientY: 5 });
    expect(binding!.onSelect).toHaveBeenLastCalledWith(["box", "map"]);
    fireEvent.pointerUp(screen.getByTestId("dashboard.canvas"), { clientX: 5, clientY: 5 });
    const surface = document.querySelector("[data-canvas-surface]") as HTMLElement;
    fireEvent.pointerDown(surface, { button: 0, clientX: 950, clientY: 590 });
    fireEvent.pointerUp(screen.getByTestId("dashboard.canvas"), { clientX: 950, clientY: 590 });
    expect(binding!.onSelect).toHaveBeenLastCalledWith([]);
  });

  it("moves the picked items by a drag, as one change", () => {
    const { changes } = draw({ selection: ["box", "dot"] });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Rectangle" }), { button: 0, clientX: 100, clientY: 100 });
    const stage = screen.getByTestId("dashboard.canvas");
    fireEvent.pointerMove(stage, { clientX: 130, clientY: 110 });
    fireEvent.pointerMove(stage, { clientX: 150, clientY: 140 });
    // The drag shows where the box would land before anything changes.
    expect((document.querySelector('[data-canvas-item="box"]') as HTMLElement).style.left).toBe("60px");
    expect(changes).toEqual([]);
    fireEvent.pointerUp(stage, { clientX: 150, clientY: 140 });
    expect(changes.length).toBe(1);
    expect(placed(changes[0], "box")).toEqual({ x: 60, y: 60, w: 200, h: 100 });
    expect(placed(changes[0], "dot")).toEqual({ x: 450, y: 60, w: 80, h: 80 });
  });

  it("resizes the one picked block by its handle", () => {
    const { changes } = draw({ selection: ["box"] });
    expect(document.querySelectorAll("[data-canvas-handle]").length).toBe(8);
    const handle = document.querySelector('[data-canvas-handle="se"]') as HTMLElement;
    fireEvent.pointerDown(handle, { button: 0, clientX: 0, clientY: 0 });
    fireEvent.pointerMove(screen.getByTestId("dashboard.canvas"), { clientX: 40, clientY: -30 });
    fireEvent.pointerUp(screen.getByTestId("dashboard.canvas"), { clientX: 40, clientY: -30 });
    expect(placed(changes[0], "box")).toEqual({ x: 10, y: 20, w: 240, h: 70 });
  });

  it("snaps a dragged line end to the side of a block it comes near", () => {
    const { changes } = draw({ selection: ["arrow"] });
    const end = document.querySelector('[data-canvas-end="to"]') as HTMLElement;
    fireEvent.pointerDown(end, { button: 0, clientX: 300, clientY: 300 });
    // The ellipse's west port is (400, 60).
    fireEvent.pointerMove(screen.getByTestId("dashboard.canvas"), { clientX: 395, clientY: 64 });
    fireEvent.pointerUp(screen.getByTestId("dashboard.canvas"), { clientX: 395, clientY: 64 });
    expect(placed(changes[0], "arrow")).toEqual({ from: { item: "box", port: "e" }, to: { item: "dot", port: "w" } });
  });

  it("moves by 10 px with the arrows, 1 px with Shift, resizes with Alt, removes, and forwards the rest", () => {
    const { binding, changes } = draw({ selection: ["box"] });
    const stage = screen.getByTestId("dashboard.canvas");
    fireEvent.keyDown(stage, { key: "ArrowRight" });
    fireEvent.keyDown(stage, { key: "ArrowDown", shiftKey: true });
    fireEvent.keyDown(stage, { key: "ArrowRight", altKey: true });
    fireEvent.keyDown(stage, { key: "Delete" });
    expect(placed(changes[0], "box")).toEqual({ x: 20, y: 20, w: 200, h: 100 });
    expect(placed(changes[1], "box")).toEqual({ x: 10, y: 21, w: 200, h: 100 });
    expect(placed(changes[2], "box")).toEqual({ x: 10, y: 20, w: 210, h: 100 });
    expect(Object.keys(changes[3].visualizations as object)).toEqual(["dot", "arrow", "map", "notes", "brief"]);
    expect(placed(changes[3], "arrow")).toEqual({ from: { x: 210, y: 70 }, to: { x: 300, y: 300 } });
    expect(binding!.onSelect).toHaveBeenLastCalledWith([]);
    fireEvent.keyDown(stage, { key: "c", ctrlKey: true });
    expect((binding!.onShortcut as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1);
    fireEvent.keyDown(stage, { key: "a", metaKey: true });
    expect(binding!.onSelect).toHaveBeenLastCalledWith(["box", "dot", "arrow", "map", "notes", "brief"]);
  });

  it("names every item for a keyboard user, who picks one with Enter", () => {
    const { binding } = draw({ selection: [] });
    const stage = screen.getByTestId("dashboard.canvas");
    expect(stage.getAttribute("role")).toBe("application");
    const names = within(stage)
      .getAllByRole("button")
      .map((button) => button.getAttribute("aria-label"));
    expect(names).toEqual(["Rectangle", "Ellipse", "Line", "Image", "Markdown", "Brief"]);
    fireEvent.keyDown(screen.getByRole("button", { name: "Markdown" }), { key: "Enter" });
    expect(binding!.onSelect).toHaveBeenLastCalledWith(["notes"]);
  });
});
