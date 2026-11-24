// Level 2 data sources as a person meets them: an answer in words, a question
// asked to an agent on demand, a visualization's Inspect and CSV export, and
// the studio's Data sources panel writing `set` / `delete` operations.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import { PLATFORM_FORMS } from "@/testing/platform-forms";
import { renderWithProviders } from "@/testing/providers";
import type { CustomDashboard, WidgetData } from "@/types";

const api = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => true }));
vi.mock("react-grid-layout", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-grid-layout")>()),
  useContainerWidth: () => ({ width: 1280, mounted: true, containerRef: { current: null } }),
}));

import { CustomDashboardView } from "./CustomDashboardView";
import { DashboardDataSourcesPanel } from "./DashboardDataSourcesPanel";
import { normalizeDashboard } from "./spec";

const ID = "11111111-1111-4111-8111-111111111111";
const AGENT = "22222222-2222-4222-8222-222222222222";

function spec(appProperties: Record<string, unknown> = {}) {
  return {
    version: 2,
    title: "Board",
    dataSources: {
      q: { type: "ask", name: "Why", options: { agent_id: AGENT, question: "Why is the score down?", shape: "text" } },
      n: { type: "static", options: { shape: "number", data: { value: 37, change: -5 } } },
      spare: { type: "static", options: { shape: "number", data: { value: 1, change: null } } },
    },
    visualizations: {
      why: { type: "xtm.answer", title: "Why", dataSources: { primary: "q" } },
      open: { type: "xtm.singlevalue", title: "Open findings", dataSources: { primary: "n" } },
    },
    layout: {
      tabs: { items: [{ layoutId: "main", label: "Main" }] },
      layoutDefinitions: {
        main: {
          structure: [
            { item: "why", position: { x: 0, y: 0, w: 6, h: 8 } },
            { item: "open", position: { x: 6, y: 0, w: 6, h: 6 } },
          ],
        },
      },
    },
    applicationProperties: appProperties,
  };
}

function draw(appProperties: Record<string, unknown> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderWithProviders(
    <CustomDashboardView dashboard={normalizeDashboard(spec(appProperties))} dashboardId={ID} revision={2} />,
    client,
  );
}

const ok = (shape: string, data: unknown): WidgetData =>
  ({ status: "ok", shape, data, fetched_at: "2026-10-25T10:00:00Z", open_url: null }) as WidgetData;

describe("dashboard data sources", () => {
  beforeEach(() => {
    api.post.mockReset();
    api.get.mockReset();
    api.get.mockResolvedValue([]);
  });

  it("asks the agent only when the person asks, then draws its answer", async () => {
    let asked = false;
    api.post.mockImplementation(async (url: string) => {
      if (url.endsWith("/data-sources/q/ask")) {
        asked = true;
        return { status: "error", code: "running", open_url: null };
      }
      if (url.endsWith("/widgets/why/data")) {
        return asked ? ok("text", { text: "Two critical findings were closed." }) : { status: "error", code: "not_run", open_url: null };
      }
      return ok("number", { value: 37, change: -5 });
    });
    draw();
    fireEvent.click(await screen.findByRole("button", { name: "Ask the agent" }, { timeout: 15_000 }));
    expect(await screen.findByText("Two critical findings were closed.", {}, { timeout: 15_000 })).toBeTruthy();
    expect(api.post.mock.calls.filter(([url]) => url.endsWith("/ask"))).toEqual([[`/custom-dashboards/${ID}/data-sources/q/ask`, {}]]);
    expect(screen.getByRole("button", { name: "Ask again" })).toBeTruthy();
  }, 30_000);

  it("says why a question cannot be asked", async () => {
    api.post.mockImplementation(async (url: string) =>
      url.endsWith("/ask")
        ? { status: "error", code: "rate_limited", open_url: null }
        : url.endsWith("/widgets/why/data")
          ? { status: "error", code: "not_run", open_url: null }
          : ok("number", { value: 1, change: null }),
    );
    draw();
    fireEvent.click(await screen.findByRole("button", { name: "Ask the agent" }, { timeout: 15_000 }));
    expect((await screen.findByRole("alert")).textContent).toBe("Too many questions at once; try again in a minute.");
  }, 30_000);

  it("inspects a visualization's source and result", async () => {
    api.post.mockImplementation(async (url: string) =>
      url.endsWith("/widgets/open/data") ? ok("number", { value: 37, change: -5 }) : { status: "error", code: "not_run", open_url: null },
    );
    draw();
    await screen.findByText("37", {}, { timeout: 15_000 });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Data of Open findings" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Inspect" }));
    const dialog = await screen.findByTestId("dashboard.inspect");
    const [source, result] = within(dialog).getAllByText((_, node) => node?.tagName === "PRE");
    expect(JSON.parse(source.textContent ?? "")).toEqual(spec().dataSources.n);
    expect(JSON.parse(result.textContent ?? "")).toEqual({ value: 37, change: -5 });
  }, 30_000);

  it("offers no CSV export when the definition hides exports", async () => {
    api.post.mockResolvedValue(ok("number", { value: 37, change: -5 }));
    draw({ hideExport: true });
    await screen.findAllByText("37", {}, { timeout: 15_000 });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Data of Open findings" }), { button: 0, ctrlKey: false });
    await screen.findByRole("menuitem", { name: "Inspect" });
    expect(screen.queryByRole("menuitem", { name: "Export CSV" })).toBeNull();
  }, 30_000);
});

describe("DashboardDataSourcesPanel", () => {
  const dashboard = { id: ID, revision: 3, can_manage: true, spec: spec(), widgets: [] } as unknown as CustomDashboard;

  beforeEach(() => {
    api.get.mockReset();
    api.get.mockResolvedValue([]);
  });

  function open(edit = vi.fn()) {
    renderWithProviders(
      <DashboardDataSourcesPanel dashboard={dashboard} layoutId="main" edit={edit} onEditSource={vi.fn()} onVisualizationAdded={vi.fn()} />,
    );
    return edit;
  }

  it("lists every source with what uses it", () => {
    open();
    const items = screen.getAllByRole("listitem").map((item) => [item.getAttribute("data-source-id"), item.textContent]);
    expect(items).toEqual([
      ["q", "WhyQuestion to an agentUsed by 1"],
      ["n", "nWritten in the dashboardUsed by 1"],
      ["spare", "spareWritten in the dashboardUnused"],
    ]);
  });

  it("creates a source written in the dashboard as one set operation", async () => {
    const edit = open();
    fireEvent.click(screen.getByRole("button", { name: "New" }));
    fireEvent.click(screen.getByRole("radio", { name: "Written in the dashboard" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "Target" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(edit.mock.calls.map(([operations]) => operations)).toEqual([
        [{ op: "set", path: "dataSources.ds_target", value: { type: "static", name: "Target", options: { shape: "number", data: { value: 0 } } } }],
      ]),
    );
  });

  it("offers the source types on tiles, a platform widget first and picked", async () => {
    open();
    fireEvent.click(screen.getByRole("button", { name: "New" }));
    const tiles = within(screen.getByRole("radiogroup", { name: "Type" })).getAllByRole("radio");
    expect(tiles.map((tile) => [tile.textContent, tile.getAttribute("aria-checked")])).toEqual([
      ["Platform widget", "true"],
      ["Written in the dashboard", "false"],
      ["Assignment answer", "false"],
      ["Question to an agent", "false"],
      ["Variable", "false"],
      ["Integration tool", "false"],
      ["Knowledge base table", "false"],
      ["Derived (steps on a source)", "false"],
    ]);
  });

  it("creates a knowledge base table read on the server every hour, with the writer's access", async () => {
    api.get.mockImplementation(async (path: string) =>
      path.startsWith("/knowledge-bases/kb1/documents")
        ? [{ id: "doc1", filename: "weekly.md" }]
        : path.startsWith("/knowledge-bases")
          ? [{ id: "kb1", name: "SOC data" }]
          : [],
    );
    const edit = open();
    fireEvent.click(screen.getByRole("button", { name: "New" }));
    fireEvent.click(screen.getByRole("radio", { name: "Knowledge base table" }));
    const dialog = screen.getByTestId("dashboard.sources.new");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), { target: { value: "Severity" } });
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Knowledge base" }));
    fireEvent.click(await screen.findByRole("option", { name: "SOC data" }));
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Document" }));
    fireEvent.click(await screen.findByRole("option", { name: "weekly.md" }));
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Section (optional)" }), { target: { value: "Weekly" } });
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Read" }));
    fireEvent.click(await screen.findByRole("option", { name: "On the server, every…" }));
    expect(within(dialog).getByText(/even those who could not read it themselves/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Every" }));
    fireEvent.click(await screen.findByRole("option", { name: "Hour" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(edit.mock.calls.map(([operations]) => operations)).toEqual([
        [
          {
            op: "set",
            path: "dataSources.ds_severity",
            value: {
              type: "knowledge",
              name: "Severity",
              options: {
                knowledge_base_id: "kb1", document_id: "doc1", format: "auto", section: "Weekly", shape: "rows",
                refreshMode: "server", refresh: "1h",
              },
            },
          },
        ],
      ]),
    );
  });

  it("creates a platform widget and places it at once, drawn as its module draws it", async () => {
    api.get.mockImplementation(async (url: string) =>
      url === "/custom-dashboards/platform-forms" ? PLATFORM_FORMS : [{ id: "i1", provider: "opencti", name: "Lab" }],
    );
    api.post.mockImplementation(async (url: string) =>
      url.endsWith("/widgets/preview")
        ? { ...ok("series", { series: [] }), presentation: { view: "xtm.column", options: {} } }
        : { status: "ok", items: [], code: null },
    );
    const edit = vi.fn((_operations, handlers) => handlers?.onSuccess?.());
    const added = vi.fn();
    renderWithProviders(<DashboardDataSourcesPanel dashboard={dashboard} layoutId="main" edit={edit} onEditSource={vi.fn()} onVisualizationAdded={added} />);
    fireEvent.click(screen.getByRole("button", { name: "New" }));
    // The platform form draws its widget types on tiles too.
    fireEvent.click(await screen.findByRole("radio", { name: "Vertical bars" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "Reports" } });
    fireEvent.click(screen.getByRole("button", { name: "Create and place" }));
    await waitFor(() => expect(edit).toHaveBeenCalledTimes(1));
    const operations = edit.mock.calls[0][0] as { op: string; path: string; value: Record<string, unknown> }[];
    expect(operations.map((operation) => operation.path)).toEqual(["dataSources.ds_reports", "visualizations.viz_ds_reports", "layout"]);
    expect(operations[0].value).toMatchObject({ type: "platform", name: "Reports", options: { source: "opencti", integration_id: "i1", native: { type: "vertical-bar" } } });
    expect(operations[1].value).toEqual({ type: "xtm.column", title: "Reports", dataSources: { primary: "ds_reports" } });
    expect(added.mock.calls).toEqual([["viz_ds_reports"]]);
  });

  it("deletes an unused source and never one in use", async () => {
    const edit = open();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Actions on n" }), { button: 0, ctrlKey: false });
    expect((await screen.findByRole("menuitem", { name: "Delete (in use)" })).getAttribute("aria-disabled")).toBe("true");
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Actions on spare" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    expect(edit.mock.calls.map(([operations]) => operations)).toEqual([[{ op: "delete", path: "dataSources.spare" }]]);
  });

  it("adds a visualization of a source to the tab on screen", async () => {
    const edit = open();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Actions on spare" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Add a visualization" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Title (heading)" }), { target: { value: "Spare" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    const [operations] = edit.mock.calls[0];
    expect(operations[0]).toEqual({
      op: "set",
      path: "visualizations.viz_spare",
      value: { type: "xtm.singlevalue", dataSources: { primary: "spare" }, title: "Spare" },
    });
    expect(operations[1].value.layoutDefinitions.main.structure.at(-1)).toEqual({
      item: "viz_spare",
      type: "block",
      position: { x: 0, y: 8, w: 6, h: 3 },
    });
  });
});
