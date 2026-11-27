// A dashboard an agent built, as the person meets it: every widget kind drawn,
// what this build cannot draw said in its place without taking the rest of the
// page down, links that stay in the web app, and asks that only prefill.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

import { DashboardAskContext } from "./ask";
import { type AskAssistant } from "@/lib/ask-assistant";
import { CustomDashboardView, type DashboardEditing, type DashboardSelection } from "./CustomDashboardView";
import { normalizeDashboard, type NormalizedDashboard } from "./spec";

// happy-dom lays nothing out: the dashboard is drawn as wide as a page, on its grid.
vi.mock("react-grid-layout", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-grid-layout")>()),
  useContainerWidth: () => ({ width: 1280, mounted: true, containerRef: { current: null } }),
}));

// A dashboard of one widget of each kind.
const TODAY = {
  title: "Today",
  widgets: [
    {
      kind: "score",
      id: "score",
      label: "Exposure score",
      value: 62,
      max: 100,
      trend: { direction: "down", label: "8 pts this week", tone: "success" },
      series: [78, 74, 70, 62],
      series_label: "Exposure score over 30 days",
      stats: [{ label: "Critical", value: "7", tone: "warning" }],
    },
    { kind: "stats", id: "why", items: [{ label: "To domain admin", value: "4 steps" }] },
    {
      kind: "stages",
      id: "cycle",
      title: "CTEM cycle · Q4 scope",
      link: { label: "Details", href: "/ctem" },
      items: [
        { label: "Scoping", state: "done", status: "Done" },
        { label: "Validate", state: "pending", status: "2 running", progress: 0.4 },
      ],
    },
    {
      kind: "decision",
      id: "decision",
      eyebrow: "Needs your decision",
      text: "Isolate **3 internet-facing VPN gateways** until the patch window.",
      tags: ["Validated by simulation"],
      actions: [
        { label: "Review the plan", prompt: "Show me the containment plan" },
        { label: "Open runs", href: "/operations" },
      ],
    },
    {
      kind: "feed",
      id: "feed",
      title: "Latest from your agents",
      items: [{ badge: "DSC", title: "Attack surface discovery", time: "07:40", text: "14 new assets.", href: "/agents" }],
    },
    { kind: "callout", id: "intel", eyebrow: "Threat landscape", text: "2 intrusion sets match 4 exposures.", tone: "ai" },
    { kind: "ranked_list", id: "top", items: [{ title: "VPN gateways - RCE", badge: { label: "CRIT", tone: "danger" } }] },
    { kind: "steps", id: "path", items: [{ label: "Initial access via VPN RCE", detail: "Not prevented", tone: "danger" }] },
    { kind: "checklist", id: "plan", eyebrow: "Proposed plan", items: [{ label: "Isolate 3 gateways", done: true }, { label: "Re-run" }] },
    {
      kind: "progress",
      id: "bas",
      title: "Breach & attack simulation",
      status: { label: "Running", tone: "accent" },
      progress: 0.64,
      progress_label: "Step 7 of 11",
      lines: [{ label: "T1566 Phishing - prevented", tone: "success" }],
    },
    { kind: "prompts", id: "ask", placeholder: "Ask XTM One anything…", suggestions: ["Draft a 3-line update for the CISO"] },
    { kind: "text", id: "note", body: "Your score improved by **8 points**." },
  ],
};

function draw(
  dashboard: NormalizedDashboard,
  {
    ask = null,
    editing = null,
    selection = null,
  }: { ask?: AskAssistant | null; editing?: DashboardEditing | null; selection?: DashboardSelection | null } = {},
) {
  return renderWithProviders(
    <MemoryRouter>
      <DashboardAskContext.Provider value={ask}>
        <CustomDashboardView dashboard={dashboard} revision={1} editing={editing} selection={selection} />
      </DashboardAskContext.Provider>
    </MemoryRouter>,
  );
}

describe("CustomDashboardView", () => {
  it("draws every widget kind of the catalog", () => {
    const { container } = draw(normalizeDashboard(TODAY), { ask: vi.fn() });
    expect([...container.querySelectorAll("[data-widget-kind]")].map((el) => el.getAttribute("data-widget-kind"))).toEqual(
      TODAY.widgets.map((w) => w.kind),
    );
    expect(screen.getByText("Exposure score")).toBeTruthy();
    expect(screen.getByText("8 pts this week")).toBeTruthy();
    expect(screen.getByRole("img", { name: "Exposure score over 30 days" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "CTEM cycle · Q4 scope" })).toBeTruthy();
    expect(screen.getByRole("progressbar", { name: "Validate" }).getAttribute("aria-valuenow")).toBe("40");
    // Inline markup is tokenized into elements, never into HTML.
    expect(screen.getByText("3 internet-facing VPN gateways").tagName).toBe("STRONG");
    expect(screen.getByText("1 of 2 done")).toBeTruthy();
    expect(screen.getByRole("progressbar", { name: "Step 7 of 11" }).getAttribute("aria-valuenow")).toBe("64");
    expect(screen.getByText("CRIT")).toBeTruthy();
    expect(screen.queryByText("This widget cannot be shown in this version.")).toBeNull();
  });

  it("says which widget it cannot draw, and draws the others", () => {
    draw(
      normalizeDashboard({
        title: "T",
        widgets: [
          { kind: "gauge", id: "g", value: 3 },
          { kind: "text", id: "ok", body: "Still here" },
        ],
      }),
    );
    expect(screen.getByText("This widget cannot be shown in this version.")).toBeTruthy();
    expect(screen.getByText("Widget type: gauge")).toBeTruthy();
    expect(screen.getByText("Still here")).toBeTruthy();
  });

  it("keeps a widget that fails to draw to its own card", () => {
    const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
    // A shape the reader never answers: the widget throws while drawing.
    const broken = {
      title: "T",
      widgets: [
        { id: "broken", kind: "stats", width: "full", items: null },
        { id: "ok", kind: "text", width: "full", body: "Still here" },
      ],
    } as unknown as NormalizedDashboard;
    draw(broken);
    expect(screen.getByText("This widget could not be drawn.")).toBeTruthy();
    expect(screen.getByText("Still here")).toBeTruthy();
    quiet.mockRestore();
  });

  it("links only to the web app's own pages", () => {
    draw(
      normalizeDashboard({
        title: "T",
        widgets: [
          {
            kind: "callout",
            text: "x",
            link: { label: "Phish", href: "https://evil.example/login" },
            actions: [
              { label: "Elsewhere", href: "//evil.example" },
              { label: "Agents", href: "/agents" },
            ],
          },
          { kind: "feed", items: [{ title: "Event", href: "javascript:alert(1)" }] },
        ],
      }),
    );
    const hrefs = screen.getAllByRole("link").map((link) => link.getAttribute("href"));
    expect(hrefs).toEqual(["/agents"]);
    expect(screen.queryByText("Phish")).toBeNull();
    expect(screen.queryByText("Elsewhere")).toBeNull();
    expect(screen.getByText("Event")).toBeTruthy();
  });

  it("hands a prompt to the assistant's composer, and offers no ask without one", () => {
    const ask = vi.fn();
    const dashboard = normalizeDashboard(TODAY);
    const { unmount } = draw(dashboard, { ask });
    fireEvent.click(screen.getByRole("button", { name: "Review the plan" }));
    fireEvent.click(screen.getByRole("button", { name: "Draft a 3-line update for the CISO" }));
    fireEvent.click(screen.getByRole("button", { name: "Ask XTM One anything…" }));
    expect(ask.mock.calls).toEqual([["Show me the containment plan"], ["Draft a 3-line update for the CISO"], []]);
    unmount();

    draw(dashboard);
    expect(screen.queryByRole("button", { name: "Review the plan" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Ask XTM One anything…" })).toBeNull();
    // A page link needs no assistant.
    expect(screen.getByRole("link", { name: "Open runs" }).getAttribute("href")).toBe("/operations");
  });

  const TWO = {
    title: "T",
    widgets: [
      { kind: "text", id: "a", title: "First", body: "1", width: "half" },
      { kind: "text", id: "b", title: "Second", body: "2", width: "half" },
    ],
  };
  const handle = (name: string) =>
    screen.getByRole("group", { name: `${name}: the arrow keys move it, Shift and the arrow keys resize it` });

  it("saves a resize on the grid as one layout operation naming the widget", () => {
    const editing = { onLayout: vi.fn(), onRemove: vi.fn() };
    draw(normalizeDashboard(TWO), { editing });
    fireEvent.keyDown(handle("First"), { key: "ArrowDown", shiftKey: true });
    expect(editing.onLayout.mock.calls).toEqual([[{ op: "layout", items: [{ id: "a", x: 0, y: 0, w: 6, h: 3 }] }]]);
  });

  it("removes a widget in edit mode and keeps the last one", () => {
    const editing = { onLayout: vi.fn(), onRemove: vi.fn() };
    const { unmount } = draw(normalizeDashboard(TWO), { editing });
    fireEvent.click(screen.getByRole("button", { name: "Remove First" }));
    expect(editing.onRemove.mock.calls.map(([widget]) => widget.id)).toEqual(["a"]);
    unmount();
    draw(normalizeDashboard({ title: "T", widgets: [{ kind: "text", id: "only", title: "Only", body: "1" }] }), { editing });
    expect(screen.queryByRole("button", { name: "Remove Only" })).toBeNull();
  });

  it("picks a widget with a click, in edit mode and out of it", () => {
    const selection = { selectedId: null, onSelect: vi.fn() };
    const { unmount } = draw(normalizeDashboard(TWO), { selection, editing: { onLayout: vi.fn(), onRemove: vi.fn() } });
    fireEvent.click(handle("Second"));
    unmount();
    draw(normalizeDashboard(TWO), { selection });
    fireEvent.click(screen.getByRole("region", { name: "First" }));
    expect(selection.onSelect.mock.calls.map(([widget]) => widget.id)).toEqual(["b", "a"]);
  });

  // A definition (version 2) with two tabs, each its own layout.
  const TABBED = {
    version: 2,
    title: "T",
    visualizations: {
      a: { type: "xtm.text", title: "First", options: { body: "1" } },
      b: { type: "xtm.text", title: "Second", options: { body: "2" } },
    },
    layout: {
      tabs: { items: [{ layoutId: "main", label: "Main" }, { layoutId: "two", label: "Two" }] },
      layoutDefinitions: {
        main: { structure: [{ item: "a", position: { x: 0, y: 0, w: 6, h: 4 } }] },
        two: { structure: [{ item: "b", position: { x: 0, y: 0, w: 6, h: 4 } }] },
      },
    },
  };

  it("shows a tab's widgets, and saves a resize there as a layout of that tab", () => {
    const editing = { onLayout: vi.fn(), onRemove: vi.fn() };
    draw(normalizeDashboard(TABBED), { editing });
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Main", "Two"]);
    expect(screen.queryByRole("group", { name: /^Second:/ })).toBeNull();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Two" }));
    expect(screen.queryByRole("group", { name: /^First:/ })).toBeNull();
    fireEvent.keyDown(handle("Second"), { key: "ArrowDown", shiftKey: true });
    expect(editing.onLayout.mock.calls).toEqual([
      [{ op: "layout", items: [{ id: "b", x: 0, y: 0, w: 6, h: 5 }], layoutId: "two" }],
    ]);
  });

  it("shows no tab bar for a dashboard of one tab", () => {
    draw(normalizeDashboard(TWO));
    expect(screen.queryByRole("tablist")).toBeNull();
  });

  it("says a score's name once, and its max once", () => {
    draw(
      normalizeDashboard({
        title: "Board brief",
        widgets: [{ kind: "score", id: "s", title: "Exposure score", label: "Exposure score", value: 58, max: 100, unit: "/100" }],
      }),
    );
    expect(screen.getAllByText("Exposure score")).toHaveLength(1);
    expect(screen.getAllByText("/100")).toHaveLength(1);
  });
});

describe("CustomDashboardView, level 5: canvases, theme, expand", () => {
  const CANVAS = {
    version: 2,
    title: "Wall",
    theme: "light",
    visualizations: {
      box: { type: "xtm.shape.rectangle", options: {} },
      brief: { type: "xtm.text", title: "Brief", options: { body: "Read me" } },
      untitled: { type: "xtm.text", options: { body: "No heading" } },
    },
    layout: {
      tabs: { items: [{ layoutId: "wall", label: "Wall" }, { layoutId: "grid", label: "Grid" }], options: { barPosition: "left" } },
      layoutDefinitions: {
        wall: {
          type: "absolute",
          options: { width: 800, height: 400 },
          structure: [
            { item: "box", position: { x: 0, y: 0, w: 100, h: 50 } },
            { item: "brief", position: { x: 200, y: 0, w: 300, h: 200 } },
          ],
        },
        grid: { type: "grid", structure: [{ item: "untitled", position: { x: 0, y: 0, w: 6, h: 4 } }] },
      },
    },
  };

  it("draws a canvas tab on its canvas, in the dashboard's own theme, its tab bar on the left", () => {
    draw(normalizeDashboard(CANVAS));
    const view = screen.getByTestId("dashboard.view");
    expect(view.getAttribute("data-mode")).toBe("light");
    expect(screen.getByRole("tablist").getAttribute("aria-orientation")).toBe("vertical");
    const canvas = screen.getByTestId("dashboard.canvas");
    expect([...canvas.querySelectorAll("[data-canvas-item]")].map((n) => n.getAttribute("data-canvas-item"))).toEqual(["box", "brief"]);
    expect(screen.getByText("Read me")).toBeTruthy();
  });

  it("follows the app's theme when the dashboard has none", () => {
    draw(normalizeDashboard({ ...CANVAS, theme: undefined }));
    expect(screen.getByTestId("dashboard.view").hasAttribute("data-mode")).toBe(false);
  });

  it("expands one visualization to fill the screen, and Escape closes it", () => {
    draw(normalizeDashboard(CANVAS));
    fireEvent.click(screen.getByRole("button", { name: "Expand Brief" }));
    const expanded = screen.getByRole("dialog", { name: "Brief" });
    expect(expanded.textContent).toContain("Read me");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Brief" })).toBeNull();
  });

  it("keeps the expand button of a widget without a heading, with no heading drawn for it", () => {
    draw(normalizeDashboard(CANVAS));
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Grid" }));
    const button = screen.getByRole("button", { name: "Expand untitled" });
    expect(button.closest("section")?.querySelector("h2")).toBeNull();
  });

  it("offers no expand button while the dashboard is rearranged", () => {
    draw(normalizeDashboard(CANVAS), { editing: { onLayout: vi.fn(), onRemove: vi.fn() } });
    expect(screen.queryByRole("button", { name: "Expand Brief" })).toBeNull();
  });
});
