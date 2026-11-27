// The configuration panel of a visualization in the Dashboard Studio: each
// section writes the definition through the designer (one history step per
// change), as the server would store it.
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import type { CustomDashboard } from "@/types";

import type { DashboardDesigner } from "./useDashboardDesigner";
import { VisualizationConfig } from "./VisualizationConfig";

const api = vi.hoisted(() => ({ get: vi.fn().mockResolvedValue([]) }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

type Definition = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

function definition(): Definition {
  return {
    version: 2,
    title: "Ops",
    dataSources: {
      ds_count: { type: "static", name: "Open alerts", options: { shape: "number", data: { value: 12 } } },
      ds_other: { type: "static", name: "Closed alerts", options: { shape: "number", data: { value: 3 } } },
    },
    visualizations: {
      kpi: { type: "xtm.singlevalue", title: "Alerts", dataSources: { primary: "ds_count" }, options: { unit: "%" } },
    },
    inputs: {},
    layout: {
      tabs: { items: [{ layoutId: "main", label: "Main" }] },
      layoutDefinitions: { main: { type: "grid", structure: [{ item: "kpi", type: "block", position: { x: 2, y: 0, w: 4, h: 6 } }] } },
      globalInputs: [],
    },
  };
}

interface Recorded {
  changes: Definition[];
  keys: (string | undefined)[];
}

/** The panel with a designer whose changes are shown at once, each kept with its coalescing key. */
function Harness({ recorded, onEditSource, onOpenDataPanel }: { recorded: Recorded; onEditSource: (sid: string | null) => void; onOpenDataPanel: () => void }) {
  const [current, setCurrent] = useState(definition);
  const d: DashboardDesigner = {
    definition: current,
    change: (update, coalesce) => {
      const next = update(current);
      recorded.changes.push(next);
      recorded.keys.push(coalesce);
      setCurrent(next);
    },
    undo: vi.fn(),
    redo: vi.fn(),
    canUndo: false,
    canRedo: false,
    selection: ["kpi"],
    select: vi.fn(),
    copy: vi.fn(),
    cut: vi.fn(),
    paste: vi.fn(),
    duplicate: vi.fn(),
    canPaste: false,
    onShortcut: () => vi.fn(),
    saveState: "saved",
  };
  return <VisualizationConfig dashboard={DASHBOARD} designer={d} vizId="kpi" layoutId="main" onEditSource={onEditSource} onOpenDataPanel={onOpenDataPanel} />;
}

const DASHBOARD = { id: "d1", title: "Ops", revision: 3, can_manage: true, spec: definition() } as unknown as CustomDashboard;

function draw(extra: { onEditSource?: (sid: string | null) => void; onOpenDataPanel?: () => void } = {}) {
  const d: Recorded = { changes: [], keys: [] };
  const view = renderWithProviders(
    <Harness recorded={d} onEditSource={extra.onEditSource ?? vi.fn()} onOpenDataPanel={extra.onOpenDataPanel ?? vi.fn()} />,
  );
  return { d, ...view };
}

describe("VisualizationConfig", () => {
  it("shows the configuration sections for a single value", () => {
    draw();
    const titles = screen.getAllByRole("button", { expanded: true }).map((b) => b.textContent);
    expect(titles).toEqual(["General", "Data source", "Data display", "Coloring", "Interactions", "Position"]);
    // Collapsed until opened.
    // The section headers only: a colour field's picker trigger is a collapsed button too.
    expect(screen.getAllByRole("button", { expanded: false }).map((b) => b.textContent).filter(Boolean)).toEqual(["Visibility", "Source"]);
  });

  it("switches the type among those drawing the same data, keeping the options it takes", async () => {
    const user = userEvent.setup();
    const { d } = draw();
    await user.click(screen.getByRole("combobox", { name: "Visualization type" }));
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Single value",
      "Single value with icon",
      "Single value radial",
      "Filler gauge",
      "Marker gauge",
    ]);
    await user.click(screen.getByRole("option", { name: "Filler gauge" }));
    expect(d.changes.length).toBe(1);
    // A gauge takes a unit too.
    expect(d.changes[0].visualizations.kpi).toEqual({ type: "xtm.fillergauge", title: "Alerts", dataSources: { primary: "ds_count" }, options: { unit: "%" } });
  });

  it("writes a heading as one run of changes, and removes an emptied one", () => {
    const { d } = draw();
    const heading = screen.getByLabelText("Heading");
    fireEvent.change(heading, { target: { value: "Open alerts" } });
    expect(d.changes[0].visualizations.kpi.title).toBe("Open alerts");
    expect(d.keys[0]).toBe("kpi.title");
    fireEvent.change(heading, { target: { value: "" } });
    expect("title" in d.changes[1].visualizations.kpi).toBe(false);
  });

  it("points the visualization at another source, or opens it in the editors", async () => {
    const user = userEvent.setup();
    const onEditSource = vi.fn();
    const onOpenDataPanel = vi.fn();
    const { d } = draw({ onEditSource, onOpenDataPanel });
    await user.click(screen.getByRole("combobox", { name: "Data source" }));
    await user.click(screen.getByRole("option", { name: /Closed alerts/ }));
    expect(d.changes[0].visualizations.kpi.dataSources).toEqual({ primary: "ds_other" });
    fireEvent.click(screen.getByRole("button", { name: "Edit the data source" }));
    expect(onEditSource).toHaveBeenCalledWith("ds_other");
    fireEvent.click(screen.getByRole("button", { name: "All data sources" }));
    expect(onOpenDataPanel.mock.calls.length).toBe(1);
  });

  it("colours the value by ranges, written as a dynamic option and its config", async () => {
    const user = userEvent.setup();
    const { d } = draw();
    await user.click(screen.getByRole("combobox", { name: "Value colour" }));
    await user.click(screen.getByRole("option", { name: "By ranges of the value" }));
    const kpi = d.changes[0].visualizations.kpi;
    expect(kpi.options.majorColor).toBe("> majorValue | rangeValue(majorColorEditorConfig)");
    expect(kpi.context).toEqual({
      majorColorEditorConfig: [
        { to: 50, value: "#4caf50" },
        { from: 50, to: 80, value: "#ff9800" },
        { from: 80, value: "#f44336" },
      ],
    });
    // A range removed rewrites the config.
    const ranges = screen.getByRole("group", { name: "Ranges" });
    fireEvent.click(within(ranges).getAllByRole("button", { name: "Remove the range" })[1]);
    expect(d.changes[1].visualizations.kpi.context.majorColorEditorConfig).toEqual([
      { to: 50, value: "#4caf50" },
      { from: 80, value: "#f44336" },
    ]);
  });

  it("switches a display option off, and back to its default by removing it", () => {
    const { d } = draw();
    const abbreviate = screen.getByRole("switch", { name: "Abbreviate the value" });
    expect(abbreviate.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(abbreviate);
    expect(d.changes[0].visualizations.kpi.options).toEqual({ unit: "%", shouldAbbreviateValue: false });
    fireEvent.click(screen.getByRole("switch", { name: "Abbreviate the value" }));
    expect(d.changes[1].visualizations.kpi.options).toEqual({ unit: "%" });
  });

  it("adds an interaction through the designer", async () => {
    const user = userEvent.setup();
    const { d } = draw();
    fireEvent.click(screen.getByRole("button", { name: "Add an interaction" }));
    const form = screen.getByTestId("dashboard.interactions.new");
    await user.type(within(form).getByLabelText("Token"), "picked");
    fireEvent.click(within(form).getByRole("button", { name: "Add the interaction" }));
    expect(d.changes[0].visualizations.kpi.eventHandlers).toEqual([
      { type: "drilldown.setToken", options: { tokens: [{ token: "picked", key: "value" }] } },
    ]);
  });

  it("places the visualization in the grid's cells, counted from one", () => {
    const { d } = draw();
    const column = screen.getByLabelText("Column") as HTMLInputElement;
    expect(column.value).toBe("3");
    fireEvent.change(column, { target: { value: "8" } });
    // Column 8 is x 7 in the definition.
    expect(d.changes[0].layout.layoutDefinitions.main.structure[0].position).toEqual({ x: 7, y: 0, w: 4, h: 6 });
    // Four columns wide, column 12 would leave the grid: kept on it, at column 9.
    fireEvent.change(screen.getByLabelText("Column"), { target: { value: "12" } });
    expect(d.changes[1].layout.layoutDefinitions.main.structure[0].position).toEqual({ x: 8, y: 0, w: 4, h: 6 });
    expect(d.keys[1]).toBe("kpi.position.x");
  });
});
