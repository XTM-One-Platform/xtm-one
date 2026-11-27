// The canvas inspector and the design bar as a person uses them: an element's
// style changed field by field (a colour saved only once it is whole), an
// image uploaded to the dashboard and drawn, the canvas's size and display,
// the dashboard's theme; elements added on top and picked. Asserted on the
// definition each change produces and on the upload sent.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

import { CanvasItemInspector, CanvasSettings, DashboardSettings } from "./CanvasInspector";
import { DashboardDesignBar } from "./DashboardDesignBar";
import { normalizeDashboard } from "./spec";
import type { DashboardDesigner } from "./useDashboardDesigner";

const api = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn(), post: vi.fn(), put: vi.fn(), postForm: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

type Definition = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

const FILE = "3f2b8c1e-7d4a-4c55-9a8e-0b1c2d3e4f50";

function definition(): Definition {
  return {
    version: 2,
    title: "Ops",
    visualizations: {
      box: { type: "xtm.shape.rectangle", options: { fillColor: "#112233", rx: 4 } },
      map: { type: "xtm.image", options: { alt: "Map" } },
    },
    layout: {
      tabs: { items: [{ layoutId: "wall", label: "Wall" }] },
      layoutDefinitions: {
        wall: {
          type: "absolute",
          options: { width: 800, height: 400 },
          structure: [
            { item: "box", type: "block", position: { x: 0, y: 0, w: 100, h: 50 } },
            { item: "map", type: "block", position: { x: 200, y: 0, w: 100, h: 50 } },
          ],
        },
      },
    },
  };
}

function designer(): DashboardDesigner & { changes: Definition[]; keys: (string | undefined)[] } {
  const changes: Definition[] = [];
  const keys: (string | undefined)[] = [];
  return {
    definition: definition(),
    change: (update, coalesce) => {
      changes.push(update(definition()));
      keys.push(coalesce);
    },
    undo: vi.fn(),
    redo: vi.fn(),
    canUndo: false,
    canRedo: false,
    selection: [],
    select: vi.fn(),
    copy: vi.fn(),
    cut: vi.fn(),
    paste: vi.fn(),
    duplicate: vi.fn(),
    canPaste: false,
    onShortcut: () => vi.fn(),
    saveState: "saved",
    changes,
    keys,
  };
}

const model = () => normalizeDashboard(definition()).tabs[0].canvas!;
const item = (id: string) => model().items.find((i) => i.id === id)!;

describe("CanvasInspector", () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset();
  });

  it("changes a shape's style field by field, a run of one field being one step", () => {
    const d = designer();
    renderWithProviders(<CanvasItemInspector designer={d} layoutId="wall" item={item("box")} model={model()} dashboardId="d1" />);
    fireEvent.change(screen.getByRole("spinbutton", { name: "Corner radius" }), { target: { value: "24" } });
    expect(d.changes[0].visualizations.box.options).toEqual({ fillColor: "#112233", rx: 24 });
    expect(d.keys[0]).toBe("box.rx");
    fireEvent.change(screen.getByRole("spinbutton", { name: "Width" }), { target: { value: "300" } });
    expect(d.changes[1].layout.layoutDefinitions.wall.structure[0].position).toEqual({ x: 0, y: 0, w: 300, h: 50 });
  });

  it("saves a colour only once it is whole", () => {
    const d = designer();
    renderWithProviders(<CanvasItemInspector designer={d} layoutId="wall" item={item("box")} model={model()} dashboardId="d1" />);
    const typed = screen.getAllByLabelText("Fill colour").find((node) => node.getAttribute("type") !== "color") as HTMLInputElement;
    fireEvent.change(typed, { target: { value: "#ab" } });
    expect(d.changes).toEqual([]);
    expect(typed.value).toBe("#ab");
    fireEvent.change(typed, { target: { value: "#abcdef" } });
    expect(d.changes.map((c) => c.visualizations.box.options.fillColor)).toEqual(["#abcdef"]);
  });

  it("uploads an image to the dashboard and draws it, with its alternative text", async () => {
    api.postForm.mockResolvedValue({ id: FILE, filename: "map.png", content_type: "image/png", size: 10 });
    const d = designer();
    const { container } = renderWithProviders(
      <CanvasItemInspector designer={d} layoutId="wall" item={item("map")} model={model()} dashboardId="d1" />,
    );
    const file = new File([new Uint8Array([137, 80, 78, 71])], "map.png", { type: "image/png" });
    fireEvent.change(container.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [file] } });
    await waitFor(() => expect(d.changes.length).toBe(1));
    expect(api.postForm.mock.calls[0][0]).toBe("/custom-dashboards/d1/assets");
    expect((api.postForm.mock.calls[0][1] as FormData).get("file")).toBe(file);
    expect(d.changes[0].visualizations.map.options).toEqual({ alt: "Map", fileId: FILE });
    fireEvent.change(screen.getByRole("textbox", { name: "Alternative text" }), { target: { value: "The network" } });
    expect(d.changes[1].visualizations.map.options.alt).toBe("The network");
  });

  it("sets the canvas's size, and the dashboard's theme and tab bar", () => {
    const d = designer();
    renderWithProviders(
      <>
        <CanvasSettings designer={d} layoutId="wall" model={model()} dashboardId="d1" />
        <DashboardSettings designer={d} showTabBar={false} barPosition="top" />
      </>,
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "Width" }), { target: { value: "1920" } });
    expect(d.changes[0].layout.layoutDefinitions.wall.options).toEqual({ width: 1920, height: 400 });
    fireEvent.click(screen.getByRole("switch", { name: "Show the tab bar" }));
    expect(d.changes[1].layout.tabs.options).toEqual({ showTabBar: true });
  });
});

function bar(d: DashboardDesigner, canvasLayoutId: string | null, extra: Partial<Parameters<typeof DashboardDesignBar>[0]> = {}) {
  return renderWithProviders(
    <DashboardDesignBar
      designer={d}
      layoutId={canvasLayoutId ?? "main"}
      canvasLayoutId={canvasLayoutId}
      zoom={null}
      onZoom={vi.fn()}
      theme={undefined}
      onAdded={vi.fn()}
      onOpenData={vi.fn()}
      onOpenSource={vi.fn()}
      {...extra}
    />,
  );
}

describe("DashboardDesignBar", () => {
  it("adds an element on top of a canvas and picks it", () => {
    const d = designer();
    bar(d, "wall");
    fireEvent.click(screen.getByRole("button", { name: "Add an ellipse" }));
    expect(d.changes[0].visualizations.ellipse).toEqual({ type: "xtm.shape.ellipse", options: { fillOpacity: 0.15, strokeWidth: 2 } });
    expect(d.changes[0].layout.layoutDefinitions.wall.structure[2]).toEqual({
      item: "ellipse",
      type: "block",
      position: { x: 88, y: 88, w: 160, h: 160 },
    });
    expect(d.select).toHaveBeenCalledWith(["ellipse"]);
  });

  it("offers charts, inputs and the dashboard's tools on a grid, and the canvas elements disabled", () => {
    const d = designer();
    const { unmount } = bar(d, null);
    const buttons = screen.getAllByRole("button");
    expect(buttons.map((b) => b.getAttribute("aria-label"))).toEqual([
      "Add a chart",
      "Add an input",
      "Add a rectangle - on a canvas tab",
      "Add an ellipse - on a canvas tab",
      "Add a line - on a canvas tab",
      "Add an icon - on a canvas tab",
      "Add an image - on a canvas tab",
      "Add markdown - on a canvas tab",
      "Data sources",
      "Dashboard source",
      "Light theme",
    ]);
    // The canvas elements only: undo, redo and the save state are the studio bar's.
    expect(buttons.filter((b) => (b as HTMLButtonElement).disabled).length).toBe(6);
    unmount();
    bar(d, "wall");
    expect((screen.getByRole("button", { name: "Delete" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("adds a chart on sample data, picked and shown in its panel", async () => {
    const d = designer();
    const onAdded = vi.fn();
    bar(d, null, { onAdded });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Add a chart" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Pie chart" }));
    const added = d.changes[0];
    expect(added.visualizations.viz_ds_sample).toEqual({ type: "xtm.pie", dataSources: { primary: "ds_sample" } });
    expect((added.dataSources as Record<string, { type: string; options: { shape: string } }>).ds_sample.options.shape).toBe("distribution");
    expect(d.select).toHaveBeenCalledWith(["viz_ds_sample"]);
    expect(onAdded).toHaveBeenCalledWith("viz_ds_sample", "visualization");
  });

  it("adds an input above the tabs, switches the theme and opens the sources", async () => {
    const d = designer();
    const onAdded = vi.fn();
    const onOpenData = vi.fn();
    const onOpenSource = vi.fn();
    bar(d, null, { onAdded, onOpenData, onOpenSource, theme: "light" });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Add an input" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Dropdown" }));
    expect((d.changes[0].inputs as Record<string, unknown>).input_dropdown).toEqual({
      type: "input.dropdown",
      title: "Dropdown",
      token: "input_dropdown",
      options: { items: [] },
    });
    expect(onAdded).toHaveBeenCalledWith("input_dropdown", "input");
    fireEvent.click(screen.getByRole("button", { name: "Dark theme" }));
    expect(d.changes[1].theme).toBe("dark");
    fireEvent.click(screen.getByRole("button", { name: "Data sources" }));
    fireEvent.click(screen.getByRole("button", { name: "Dashboard source" }));
    expect(onOpenData.mock.calls.length).toBe(1);
    expect(onOpenSource.mock.calls.length).toBe(1);
  });

});
