// The tab bar as the studio edits it: a tab added (grid or canvas) opens,
// renamed through its dialog, moved, retyped, removed after a confirmation;
// a canvas holding elements a grid cannot draw stays a canvas, and says so.
// Out of the studio it only switches tabs.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

import { DashboardTabsBar } from "./DashboardTabsBar";
import { normalizeDashboard } from "./spec";

type Definition = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

function definition(): Definition {
  return {
    version: 2,
    title: "Ops",
    visualizations: {
      box: { type: "xtm.shape.rectangle", options: {} },
      note: { type: "xtm.text", options: { body: "Hello" } },
    },
    layout: {
      tabs: { items: [{ layoutId: "wall", label: "Wall" }, { layoutId: "grid", label: "Grid" }] },
      layoutDefinitions: {
        wall: { type: "absolute", options: { width: 800, height: 400 }, structure: [{ item: "box", position: { x: 0, y: 0, w: 10, h: 10 } }] },
        grid: { type: "grid", structure: [{ item: "note", position: { x: 0, y: 0, w: 12, h: 4 } }] },
      },
    },
  };
}

function draw(value: string, editable = true) {
  const tabs = normalizeDashboard(definition()).tabs;
  const changes: Definition[] = [];
  const onValueChange = vi.fn();
  renderWithProviders(
    <DashboardTabsBar
      tabs={tabs}
      value={value}
      onValueChange={onValueChange}
      position="top"
      editing={editable ? { definition: definition(), onChange: (update) => changes.push(update(definition())) } : null}
    />,
  );
  return { changes, onValueChange };
}

async function pick(menu: string, item: string) {
  fireEvent.pointerDown(screen.getByRole("button", { name: menu }), { button: 0, ctrlKey: false });
  fireEvent.click(await screen.findByRole("menuitem", { name: item }));
}

describe("DashboardTabsBar", () => {
  it("only switches tabs out of the studio", () => {
    const { onValueChange } = draw("wall", false);
    expect(screen.queryByRole("button", { name: "Add a tab" })).toBeNull();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Grid" }));
    fireEvent.click(screen.getByRole("tab", { name: "Grid" }));
    expect(onValueChange).toHaveBeenCalledWith("grid");
  });

  it("adds a canvas tab and opens it", async () => {
    const { changes, onValueChange } = draw("wall");
    await pick("Add a tab", "Add an absolute layout tab");
    expect(changes[0].layout.tabs.items).toEqual([
      { layoutId: "wall", label: "Wall" },
      { layoutId: "grid", label: "Grid" },
      { layoutId: "layout_3", label: "Absolute layout" },
    ]);
    expect(changes[0].layout.layoutDefinitions.layout_3).toEqual({ type: "absolute", options: { width: 1440, height: 960 }, structure: [] });
    expect(onValueChange).toHaveBeenCalledWith("layout_3");
  });

  it("renames the open tab through its dialog", async () => {
    const { changes } = draw("grid");
    await pick("Tab options", "Rename");
    const field = await screen.findByRole("textbox", { name: "Tab name" });
    fireEvent.change(field, { target: { value: "Notes" } });
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    expect(changes[0].layout.tabs.items[1]).toEqual({ layoutId: "grid", label: "Notes" });
  });

  it("moves the open tab and turns a grid into a canvas", async () => {
    const { changes } = draw("grid");
    await pick("Tab options", "Move left");
    expect(changes[0].layout.tabs.items.map((t: Definition) => t.layoutId)).toEqual(["grid", "wall"]);
    await pick("Tab options", "Use an absolute layout");
    expect(changes[1].layout.layoutDefinitions.grid.type).toBe("absolute");
  });

  it("keeps a canvas holding shapes a canvas, and says why", async () => {
    const { changes } = draw("wall");
    await pick("Tab options", "Use a grid");
    expect(await screen.findByText("This tab cannot become a grid")).toBeTruthy();
    expect(changes).toEqual([]);
  });

  it("removes the open tab once confirmed, with what only it shows, and opens its neighbour", async () => {
    const { changes, onValueChange } = draw("wall");
    await pick("Tab options", "Delete tab");
    expect(await screen.findByText("The tab and what only it shows are removed. Undo brings them back.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Type Wall to confirm deletion"), { target: { value: "Wall" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(changes.length).toBe(1));
    expect(Object.keys(changes[0].layout.layoutDefinitions)).toEqual(["grid"]);
    expect(Object.keys(changes[0].visualizations)).toEqual(["note"]);
    expect(onValueChange).toHaveBeenCalledWith("grid");
  });
});
