import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import { AgentGraphFilterControls, AgentGraphForces, AgentGraphStatusLine, AgentGraphToolbar } from "./agent-graph-toolbar";
import { DEFAULT_GRAPH_FILTERS } from "@/lib/agent-graph-filters";

describe("AgentGraphToolbar", () => {
  it("shows the counters of the scope and switches the layout", async () => {
    const user = userEvent.setup();
    const onLayoutChange = vi.fn();
    renderWithProviders(
      <AgentGraphToolbar
        layout="force"
        onLayoutChange={onLayoutChange}
        counters={[
          { key: "agents", label: "agents", value: 7 },
          { key: "runs", label: "runs in flight", value: 2, live: true },
        ]}
      />,
    );
    expect(screen.getByRole("toolbar", { name: "Graph toolbar" })).toBeTruthy();
    expect(screen.getByText("7")).toBeTruthy();
    expect(screen.getByText("runs in flight")).toBeTruthy();
    await user.click(screen.getByRole("combobox", { name: "Graph layout" }));
    await user.click(screen.getByRole("option", { name: "Radial" }));
    expect(onLayoutChange).toHaveBeenCalledWith("radial");
    await user.click(screen.getByRole("combobox", { name: "Graph layout" }));
    await user.click(screen.getByRole("option", { name: "Org chart" }));
    expect(onLayoutChange).toHaveBeenCalledWith("orgchart");
  });

  it("keeps the legend behind the Legend button", () => {
    renderWithProviders(
      <AgentGraphToolbar layout="force" onLayoutChange={vi.fn()} counters={[]} legend={<span>Delegate swatch</span>} />,
    );
    const button = screen.getByRole("button", { name: "Legend" });
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Delegate swatch")).toBeNull();
    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Delegate swatch")).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByText("Delegate swatch")).toBeNull();
  });

  it("bounds the legend to the card that clips it and lets it scroll", () => {
    const { container } = renderWithProviders(
      <div style={{ overflow: "hidden" }} data-testid="card">
        <AgentGraphToolbar layout="force" onLayoutChange={vi.fn()} counters={[]} legend={<span>Delegate swatch</span>} />
      </div>,
    );
    const card = screen.getByTestId("card");
    const rect = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 0, width: 0, height: bottom - top, x: 0, y: top, toJSON: () => ({}) });
    const spy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return this === card ? rect(0, 500) : rect(100, 600);
    });
    fireEvent.click(screen.getByRole("button", { name: "Legend" }));
    const panel = screen.getByRole("region", { name: "Legend" });
    // 500 (card bottom) - 100 (panel top) - 8 (margin).
    expect(panel.style.maxHeight).toBe("392px");
    expect(panel.getAttribute("tabindex")).toBe("0");
    // Keyboard focus lands on the panel and draws the design system's ring, not the native outline.
    panel.focus();
    expect(document.activeElement).toBe(panel);
    expect(panel.getAttribute("data-focus-ring")).toBe("ring-focus");
    expect(container.contains(panel)).toBe(true);
    spy.mockRestore();
  });

  it("picks the layout with one select, never a pill row that reads as tabs (#9980, UX B1)", () => {
    renderWithProviders(<AgentGraphToolbar layout="force" onLayoutChange={vi.fn()} counters={[]} />);
    const toolbar = screen.getByRole("toolbar", { name: "Graph toolbar" });
    expect(within(toolbar).queryAllByRole("group")).toEqual([]);
    expect(within(toolbar).getByRole("combobox", { name: "Graph layout" }).textContent).toContain("Force");
    expect(within(toolbar).queryByRole("tablist")).toBeNull();
    expect(within(toolbar).queryByText("Data flow")).toBeNull();
  });

  it("shows no Legend button without a legend", () => {
    renderWithProviders(<AgentGraphToolbar layout="force" onLayoutChange={vi.fn()} counters={[]} />);
    expect(screen.queryByRole("button", { name: "Legend" })).toBeNull();
  });
});

describe("AgentGraphStatusLine", () => {
  it("shows the counters in a status line", () => {
    renderWithProviders(
      <AgentGraphStatusLine counters={[{ key: "agents", label: "agents", value: 153 }, { key: "edges", label: "handovers", value: 86 }]} />,
    );
    const status = screen.getByRole("status", { name: "Graph status" });
    expect(status.textContent).toContain("153");
    expect(status.textContent).toContain("handovers");
  });
});

describe("AgentGraphForces", () => {
  it("opens the force sliders and reports a change", () => {
    const onChange = vi.fn();
    renderWithProviders(<AgentGraphForces forces={{ charge: 1, linkDistance: 126, collide: 90 }} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Forces" }));
    const sliders = screen.getAllByRole("slider");
    expect(sliders).toHaveLength(3);
    fireEvent.change(sliders[2], { target: { value: "130" } });
    expect(onChange).toHaveBeenCalledWith({ charge: 1, linkDistance: 126, collide: 130 });
  });
});

describe("AgentGraphFilterControls", () => {
  it("toggles a kind and an activity filter, and badges the count", () => {
    const onChange = vi.fn();
    const { rerender } = renderWithProviders(<AgentGraphFilterControls filters={DEFAULT_GRAPH_FILTERS} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Filters" }));
    fireEvent.click(screen.getByLabelText("Personal"));
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_GRAPH_FILTERS, kinds: ["personal"] });
    fireEvent.click(screen.getByLabelText("With runs in flight"));
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_GRAPH_FILTERS, withRunsInFlight: true });
    rerender(<AgentGraphFilterControls filters={{ ...DEFAULT_GRAPH_FILTERS, withRunsInFlight: true }} onChange={onChange} />);
    expect(screen.getByRole("button", { name: /Filters/ }).textContent).toContain("1");
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(onChange).toHaveBeenLastCalledWith(DEFAULT_GRAPH_FILTERS);
  });
});
