// The maps, graphs, flows and trellises of a dashboard as a viewer meets
// them: what each view draws from its closed data, what it says of data it
// cannot draw, and an uploaded SVG redrawn from its shapes alone. A live
// OpenCTI map goes from the widget's request to the map.

import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import { renderWithProviders } from "@/testing/providers";
import type { WidgetData } from "@/types";

// happy-dom lays nothing out: every measured box is 600 x 300.
vi.mock("@/components/presentation/measured-box", () => ({ useMeasuredBox: () => ({ width: 600, height: 300 }) }));
const api = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => true }));
// The colour mode each React Flow canvas is given: a canvas left on its own
// default draws light nodes in the dark app (the design system keys its
// elevation tokens on the mode React Flow stamps on its container).
const flowModes = vi.hoisted(() => [] as unknown[]);
vi.mock("@xyflow/react", async (importOriginal) => {
  const original = await importOriginal<typeof import("@xyflow/react")>();
  const { createElement } = await import("react");
  const ReactFlow = ((props: Parameters<typeof original.ReactFlow>[0]) => {
    flowModes.push(props.colorMode);
    return createElement(original.ReactFlow, props);
  }) as typeof original.ReactFlow;
  return { ...original, ReactFlow };
});

import { CustomDashboardView } from "../CustomDashboardView";
import { normalizeDashboard, type PlatformView } from "../spec";
import StudioViz from "./StudioViz";

type Ok = Extract<WidgetData, { status: "ok" }>;
const ok = (data: Omit<Ok, "status" | "fetched_at" | "open_url">): Ok => ({ status: "ok", fetched_at: "2026-10-25T10:00:00Z", open_url: null, ...data }) as Ok;

/** The SVG shapes naming themselves `text` on hover (their `<title>`). */
const titled = Object.assign((text: string) => screen.getAllByText(text, { selector: "title" }), {
  query: (text: string) => screen.queryAllByText(text, { selector: "title" })[0],
});

function draw(data: Ok, view: PlatformView, options: Record<string, unknown> = {}) {
  return renderWithProviders(<StudioViz data={data} view={view} height={300} options={options} />);
}

const GEO = ok({
  shape: "geo",
  data: {
    points: [{ label: "Lyon", lat: 45.76, lon: 4.84, value: 2, group: "City" }],
    regions: [
      { code: "FR", label: "France", value: 9 },
      { code: "SG", label: "Singapore", value: 3 },
    ],
  },
});
const GRAPH = ok({
  shape: "graph",
  data: {
    nodes: [
      { id: "apt28", label: "APT28", group: "Intrusion set", value: null },
      { id: "xagent", label: "X-Agent", group: "Malware", value: null },
      { id: "fr", label: "France", group: "Country", value: null },
    ],
    edges: [
      { source: "apt28", target: "xagent", label: "uses", value: 4 },
      { source: "xagent", target: "fr", label: null, value: 2 },
    ],
  },
});

beforeAll(() => {
  if (!("ResizeObserver" in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
});

describe("StudioViz", () => {
  it("draws a choropleth of countries by code, a dot for a country too small to see, and bubbles", async () => {
    const { container } = draw(GEO, "map");
    expect(await screen.findByRole("img", { name: "Map of 3 places" }, { timeout: 15_000 })).toBeTruthy();
    expect(titled("France: 9").length).toBe(1);
    expect(titled("Singapore: 3").length).toBe(1);
    expect(titled("Lyon: 2").length).toBe(1);
    expect(container.querySelector('[data-viz="map"]')?.getAttribute("data-region")).toBe("world");
    expect(container.querySelectorAll('[data-layer="bubble"]').length).toBe(1);
    expect(container.querySelectorAll('[data-layer="marker"]').length).toBe(0);
  }, 20_000);

  it("draws the layers a map asks for, over the region it names", async () => {
    const { container } = draw(GEO, "map", { layers: ["marker"], region: "europe" });
    expect(await screen.findByRole("img", { name: "Map of 3 places" }, { timeout: 15_000 })).toBeTruthy();
    expect(container.querySelector('[data-viz="map"]')?.getAttribute("data-region")).toBe("europe");
    expect(container.querySelectorAll('[data-layer="marker"]').length).toBe(1);
    expect(container.querySelectorAll('[data-layer="bubble"]').length).toBe(0);
    expect((titled.query("France: 9") ?? null)).toBeNull();
  }, 20_000);

  it("redraws an uploaded SVG from its shapes alone, its regions coloured by value", () => {
    const svg =
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50"><script>alert(1)</script>' +
      '<foreignObject><div>html</div></foreignObject><image href="https://evil.example/x.png"/>' +
      '<g transform="translate(1,1)"><rect id="hall" x="0" y="0" width="50" height="50" onclick="steal()" style="fill:url(https://evil.example)"/>' +
      '<rect id="lab" x="50" y="0" width="50" height="50"/><text>words</text></g></svg>';
    const { container } = draw(ok({ shape: "geo", data: { points: [], regions: [{ code: "hall", label: "Main hall", value: 7 }] } }), "choroplethsvg", { svg });
    expect(screen.getByRole("img", { name: "Map of 1 place" })).toBeTruthy();
    expect(titled("Main hall: 7")[0]).toBeTruthy();
    for (const gone of ["script", "foreignObject", "image", "text", "div"]) expect(container.querySelector(gone)).toBeNull();
    const hall = container.querySelector('[data-region="hall"]');
    expect(hall?.getAttribute("onclick")).toBeNull();
    expect(hall?.getAttribute("style")).not.toContain("evil");
    expect(container.querySelectorAll("rect").length).toBe(2);
  });

  it("says so when the SVG of a choropleth cannot be read", () => {
    draw(ok({ shape: "geo", data: { points: [], regions: [{ code: "a", label: "a", value: 1 }] } }), "choroplethsvg", { svg: "<svg><g></svg>" });
    expect(screen.getByText("The SVG of this choropleth cannot be drawn.")).toBeTruthy();
  });

  it("draws a network graph on a React Flow canvas in the app's colour mode", async () => {
    document.documentElement.dataset.mode = "dark";
    flowModes.length = 0;
    draw(GRAPH, "networkgraph");
    expect(await screen.findByRole("img", { name: "Graph of 3 nodes and 2 links" }, { timeout: 15_000 })).toBeTruthy();
    expect(new Set(flowModes)).toEqual(new Set(["dark"]));
    expect(screen.getByText("Intrusion set")).toBeTruthy();
  }, 20_000);

  it("draws an Attack Flow: an action's card names its tactic and technique, a condition its branches, an operator AND / OR", async () => {
    flowModes.length = 0;
    const flow = ok({
      shape: "graph",
      data: {
        nodes: [
          { id: "f", label: "Operation Dream Job", group: null, value: null, kind: "start" },
          { id: "a1", label: "Spearphishing Link", group: null, value: null, kind: "action", technique: "T1566.002", tactic: "initial-access" },
          { id: "c", label: "Macro enabled", group: null, value: null, kind: "condition" },
          { id: "o", label: "OR", group: null, value: null, kind: "operator", operator: "OR" },
          { id: "a2", label: "[T1204] User Execution", group: null, value: null, kind: "action", technique: "T1204" },
          { id: "w", label: "Workstation", group: null, value: null, kind: "asset" },
        ],
        edges: [
          { source: "f", target: "a1", label: null, value: null },
          { source: "a1", target: "c", label: null, value: null },
          { source: "c", target: "o", label: "true", value: null },
          { source: "o", target: "a2", label: null, value: null },
          { source: "a2", target: "w", label: null, value: null },
        ],
      },
    });
    draw(flow, "attackflow");
    const canvas = await screen.findByTestId("attack-flow", {}, { timeout: 15_000 });
    expect(await within(canvas).findByText("Spearphishing Link", {}, { timeout: 15_000 })).toBeTruthy();
    expect(within(canvas).getByText("initial-access")).toBeTruthy();
    expect(within(canvas).getByText("T1566.002")).toBeTruthy();
    expect(within(canvas).getByText("Condition")).toBeTruthy();
    expect(within(canvas).getByText("OR")).toBeTruthy();
    expect(within(canvas).getByText("Asset")).toBeTruthy();
    // The id a platform puts before a technique's name is not said twice.
    expect(within(canvas).getByText("User Execution")).toBeTruthy();
    expect(canvas.querySelectorAll("[data-flow-kind]")).toHaveLength(6);
    expect(flowModes.length).toBeGreaterThan(0);
  }, 20_000);

  it("draws a link graph from a table's columns as stages", async () => {
    const rows = ok({
      shape: "rows",
      data: {
        columns: [{ key: "actor", label: "Actor" }, { key: "malware", label: "Malware" }],
        rows: [{ actor: "APT28", malware: "X-Agent" }],
        total: null,
      },
    });
    draw(rows, "linkgraph");
    expect(await screen.findByRole("img", { name: "Graph of 2 nodes and 1 link" }, { timeout: 15_000 })).toBeTruthy();
  }, 20_000);

  it("draws a sankey's flows and nodes", () => {
    draw(GRAPH, "sankey");
    expect(screen.getByRole("img", { name: "Flows between 3 nodes" })).toBeTruthy();
    expect(titled("APT28 → X-Agent: 4")[0]).toBeTruthy();
    expect(titled("X-Agent: 4")[0]).toBeTruthy();
  });

  it("draws a timeline from a table's dates, in lanes", () => {
    const rows = ok({
      shape: "rows",
      data: {
        columns: [{ key: "name", label: "Name" }, { key: "type", label: "Type" }, { key: "date", label: "Created" }],
        rows: [
          { name: "Q3 report", type: "Report", date: "2026-10-01" },
          { name: "Campaign", type: "Campaign", date: "2026-09-12" },
        ],
        total: 2,
      },
    });
    draw(rows, "timeline");
    expect(screen.getByRole("img", { name: "Timeline of 2 events" })).toBeTruthy();
    expect(screen.getByText("Q3 report")).toBeTruthy();
    // "Campaign" is the event and its lane, named in the gutter.
    expect(screen.getAllByText("Campaign", { selector: "text" }).length).toBe(2);
    expect(screen.getAllByText("Report", { selector: "text" }).length).toBe(1);
  });

  it("draws parallel coordinates, an axis per column", () => {
    const rows = ok({
      shape: "rows",
      data: {
        columns: [{ key: "actor", label: "Actor" }, { key: "cvss", label: "CVSS" }],
        rows: [{ actor: "APT28", cvss: "9.8" }, { actor: "APT29", cvss: "5" }],
        total: null,
      },
    });
    draw(rows, "parallel");
    expect(screen.getByRole("img", { name: "Parallel coordinates of 2 rows" })).toBeTruthy();
    expect(screen.getByText("CVSS")).toBeTruthy();
    expect(titled("Actor: APT28 · CVSS: 9.8")[0]).toBeTruthy();
  });

  it("draws a treemap of a hierarchy", () => {
    draw(ok({ shape: "tree", data: { items: [{ path: ["Malware", "Ransomware"], value: 30 }, { path: ["Tool"], value: 20 }] } }), "treemap");
    expect(screen.getByRole("img", { name: "Treemap of 2 values" })).toBeTruthy();
    expect(titled("Malware / Ransomware: 30")[0]).toBeTruthy();
  });

  it("splits a chart into a trellis of panels", () => {
    const series = ok({
      shape: "series",
      data: { series: [{ name: "Reports", points: [{ x: "2026-01", y: 3 }] }, { name: "Indicators", points: [{ x: "2026-01", y: 9 }] }] },
    });
    const { container } = draw(series, "line", { trellis: { splitBy: "series", sort: "-total" } });
    const trellis = container.querySelector('[data-viz="trellis"]') as HTMLElement;
    expect(within(trellis).getAllByRole("region").map((panel) => panel.getAttribute("aria-label"))).toEqual(["Indicators", "Reports"]);
  });

  it("says what a view needs when the data does not fit it", () => {
    draw(GRAPH, "map");
    expect(screen.getByText("A map draws places: points with a latitude and a longitude, or regions by code.")).toBeTruthy();
  });
});

describe("nothing to show", () => {
  it("says it as the module says it: OpenAEV's sentence under an inbox, across the widget", () => {
    draw(ok({ shape: "rows", data: { columns: [{ key: "name", label: "Name" }], rows: [], total: 0 } }), "list", { emptyMessage: "display" });
    const note = screen.getByText("No data to display");
    expect(note.closest("[style]")?.getAttribute("style")).toContain("height: 300px");
  });
});

describe("a live OpenCTI map", () => {
  const ID = "11111111-1111-4111-8111-111111111111";
  const INTEGRATION = "22222222-2222-4222-8222-222222222222";

  beforeEach(() => api.post.mockReset());

  it("asks for the widget's data and draws the countries it answers", async () => {
    api.post.mockResolvedValue({ ...GEO, open_url: "https://cti.example/dashboard/workspaces/dashboards" } satisfies WidgetData);
    const dashboard = normalizeDashboard({
      title: "Targets",
      widgets: [
        {
          kind: "platform",
          id: "targets",
          title: "Targeted countries",
          source: "opencti",
          integration_id: INTEGRATION,
          view: "map",
          native: { type: "map", perspective: "relationships", dataSelection: [{}] },
        },
      ],
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderWithProviders(<CustomDashboardView dashboard={dashboard} dashboardId={ID} revision={1} />, client);
    expect(await screen.findByRole("img", { name: "Map of 3 places" }, { timeout: 15_000 })).toBeTruthy();
    expect(api.post.mock.calls).toEqual([[`/custom-dashboards/${ID}/widgets/targets/data`, {}]]);
    // The way to the platform is in the widget's menu, not under the map.
    expect(screen.queryByRole("link", { name: "Open in OpenCTI" })).toBeNull();
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Data of Targeted countries" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Open in OpenCTI" }));
    expect(open.mock.calls).toEqual([["https://cti.example/dashboard/workspaces/dashboards", "_blank", "noopener,noreferrer"]]);
  }, 20_000);
});
