// A live platform widget: what it asks the server for, and how it draws each
// answer - data per shape, a sentence per error code, a link to the platform -
// and the placeholder outside a stored dashboard or with the studio off.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import { renderWithProviders } from "@/testing/providers";
import type { WidgetData } from "@/types";

const api = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));
const flags = vi.hoisted(() => ({ studio: true }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => flags.studio }));

import { CustomDashboardView } from "./CustomDashboardView";
import { normalizeDashboard } from "./spec";

const ID = "11111111-1111-4111-8111-111111111111";
const INTEGRATION = "22222222-2222-4222-8222-222222222222";

function draw(native: Record<string, unknown>, view: string | undefined, dashboardId: string | null = ID, title = "Reports") {
  const dashboard = normalizeDashboard({
    title: "Threat board",
    widgets: [{ kind: "platform", id: "live", title, source: "opencti", integration_id: INTEGRATION, view, native }],
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderWithProviders(<CustomDashboardView dashboard={dashboard} dashboardId={dashboardId ?? undefined} revision={2} />, client);
}

const NUMBER = { type: "number", perspective: "entities", dataSelection: [{}] };
const OPEN = "https://cti.example/dashboard/workspaces/dashboards";

describe("PlatformWidget", () => {
  beforeEach(() => {
    api.post.mockReset();
    flags.studio = true;
  });

  it("asks for the stored widget's data and draws a number", async () => {
    api.post.mockResolvedValue({
      status: "ok", shape: "number", data: { value: 1234, change: 12 }, fetched_at: "2026-10-23T10:00:00Z", open_url: OPEN,
    } satisfies WidgetData);
    draw(NUMBER, "number");
    // The chart module loads lazily: give it the time a busy machine takes.
    expect(await screen.findByText("1.2K", {}, { timeout: 15_000 })).toBeTruthy();
    expect(screen.getByText("12")).toBeTruthy();
    expect(api.post.mock.calls).toEqual([[`/custom-dashboards/${ID}/widgets/live/data`, {}]]);
    // No footer under the drawing: the way to the platform is the widget menu's.
    expect(screen.queryByRole("link", { name: "Open in OpenCTI" })).toBeNull();
  }, 20_000);

  it("draws a module's widget in that module's accent, its frame in the platform's", async () => {
    api.post.mockResolvedValue({
      status: "ok", shape: "number", data: { value: 7, change: 1 }, fetched_at: "2026-10-23T10:00:00Z", open_url: OPEN,
      presentation: { view: "xtm.singlevalue", options: { valueStyle: "difference", accentColor: "brand" } },
    } satisfies WidgetData);
    const { container } = draw(NUMBER, "number");
    await screen.findByText("7", {}, { timeout: 15_000 });
    const content = container.querySelector("[data-accent]") as HTMLElement;
    expect(content.dataset.accent).toBe("brand");
    expect(content.style.getPropertyValue("--color-accent")).toBe("var(--color-module-brand)");
    // The menu of the frame is outside it.
    expect(content.contains(screen.getByRole("button", { name: "Data of Reports" }))).toBe(false);
  }, 20_000);

  it("writes its frame as the module asks: Expand before the menu", async () => {
    api.post.mockResolvedValue({
      status: "ok", shape: "number", data: { value: 7, change: 1 }, fetched_at: "2026-10-23T10:00:00Z", open_url: OPEN,
      presentation: { view: "xtm.singlevalue", options: { titleStyle: "capitalize", expandShown: true } },
    } satisfies WidgetData);
    draw(NUMBER, "number");
    await screen.findByText("7", {}, { timeout: 15_000 });
    expect(screen.getByRole("heading", { name: "Reports" })).toBeTruthy();
    const expand = screen.getByRole("button", { name: "Expand Reports" });
    const menu = screen.getByRole("button", { name: "Data of Reports" });
    expect(expand.compareDocumentPosition(menu) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  }, 20_000);

  it("raises only the first letter of a title the module capitalizes: a sentence keeps its own case", async () => {
    api.post.mockResolvedValue({
      status: "ok", shape: "number", data: { value: 7, change: 1 }, fetched_at: "2026-10-23T10:00:00Z", open_url: OPEN,
      presentation: { view: "xtm.singlevalue", options: { titleStyle: "capitalize" } },
    } satisfies WidgetData);
    draw(NUMBER, "number", ID, "reports of the last week");
    await screen.findByText("7", {}, { timeout: 15_000 });
    expect(screen.getByRole("heading", { name: "Reports of the last week" })).toBeTruthy();
  }, 20_000);

  it("puts a tile's change sentence in the widget's header when its look asks, in the module's accent", async () => {
    api.post.mockResolvedValue({
      status: "ok", shape: "number", data: { value: 4, change: 3, previous: 1, subject: "finding", span: "3m" }, fetched_at: "2026-10-23T10:00:00Z",
      open_url: OPEN, presentation: { view: "xtm.singlevalue", options: { valueStyle: "tile", valueInfo: "header", accentColor: "brand" } },
    } as unknown as WidgetData);
    draw(NUMBER, "number");
    const info = await screen.findByLabelText("+3 findings over the last 3 months (1 previously)", {}, { timeout: 15_000 });
    expect(info.closest("[data-header-slot]")).toBeTruthy();
    expect(info.style.color).toBe("var(--color-module-brand)");
    const menu = screen.getByRole("button", { name: "Data of Reports" });
    expect(info.compareDocumentPosition(menu) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  }, 20_000);

  it("keeps the platform's accent for a widget whose look names none", async () => {
    api.post.mockResolvedValue({ status: "ok", shape: "number", data: { value: 7, change: 1 }, fetched_at: "2026-10-23T10:00:00Z", open_url: OPEN } satisfies WidgetData);
    const { container } = draw(NUMBER, "number");
    await screen.findByText("7", {}, { timeout: 15_000 });
    expect(container.querySelector("[data-accent]")).toBeNull();
  }, 20_000);

  it("draws rows as text, linking only to http(s) addresses", async () => {
    api.post.mockResolvedValue({
      status: "ok",
      shape: "rows",
      data: {
        columns: [{ key: "name", label: "Name" }, { key: "type", label: "Type" }],
        rows: [
          { name: "Q3 report", type: "Report", _url: "https://cti.example/dashboard/id/r1" },
          { name: "<b>bold</b>", type: "Note", _url: "javascript:alert(1)" },
        ],
        total: 40,
      },
      fetched_at: "2026-10-23T10:00:00Z",
      open_url: null,
    } satisfies WidgetData);
    draw({ type: "list", perspective: "entities", dataSelection: [{}] }, "list");
    expect((await screen.findByRole("link", { name: "Q3 report" }, { timeout: 15_000 })).getAttribute("href")).toBe(
      "https://cti.example/dashboard/id/r1",
    );
    expect(screen.getByText("<b>bold</b>").tagName).toBe("SPAN");
    expect(screen.queryByRole("link", { name: "<b>bold</b>" })).toBeNull();
    expect(screen.getByText("2 of 40")).toBeTruthy();
  }, 20_000);

  it("draws a distribution list", async () => {
    api.post.mockResolvedValue({
      status: "ok", shape: "distribution", data: { items: [{ label: "ACME CERT", value: 5 }, { label: "Jane", value: 2 }] },
      fetched_at: "2026-10-23T10:00:00Z", open_url: null,
    } satisfies WidgetData);
    draw({ type: "distribution-list", perspective: "entities", dataSelection: [{}] }, "distribution");
    expect(await screen.findByText("ACME CERT", {}, { timeout: 15_000 })).toBeTruthy();
    expect(screen.getByText("Jane")).toBeTruthy();
  }, 20_000);

  it.each([
    ["integration_unavailable", "The OpenCTI integration of this widget is not available to you."],
    ["access_denied", "OpenCTI refused your access to this data."],
    ["timeout", "OpenCTI took too long to answer."],
    ["upstream_error", "OpenCTI could not answer."],
    ["rate_limited", "Too many reads at once; try again in a moment."],
  ] as const)("says why there is no data: %s", async (code, sentence) => {
    api.post.mockResolvedValue({ status: "error", code, open_url: null } satisfies WidgetData);
    draw(NUMBER, "number");
    expect(await screen.findByText(sentence)).toBeTruthy();
  });

  it("says whose access read a snapshot refreshed on the server, and refreshes it on request", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-23T10:05:00Z"));
    const snapshot = { mode: "server", every: "15m", refreshed_at: "2026-10-23T10:00:00Z", publisher: "Alice" } as const;
    const changes = { since: "2026-10-23T09:05:00Z", figure: { before: 4, after: 7 }, items: [{ label: "high", before: 2, after: 6 }] };
    api.post.mockResolvedValue({
      status: "ok", shape: "number", data: { value: 7, change: null }, fetched_at: "2026-10-23T10:00:00Z", open_url: null, snapshot, changes,
    } satisfies WidgetData);  // prettier-ignore
    const dashboard = normalizeDashboard({
      version: 2,
      title: "Board",
      dataSources: { sev: { type: "static", options: { shape: "number", data: { value: 7 } } } },
      visualizations: { big: { type: "xtm.singlevalue", title: "Open findings", dataSources: { primary: "sev" } } },
      layout: {
        tabs: { items: [{ layoutId: "main", label: "Main" }] },
        layoutDefinitions: { main: { structure: [{ item: "big", position: { x: 0, y: 0, w: 4, h: 3 } }] } },
      },
    });
    renderWithProviders(<CustomDashboardView dashboard={dashboard} dashboardId={ID} revision={2} />, new QueryClient({ defaultOptions: { queries: { retry: false } } }));
    expect(await screen.findByText("Data read 5m ago with Alice's access.")).toBeTruthy();
    // What changed since the previous read, said from the two reads.
    expect(screen.getByTestId("widget.changes").textContent).toBe("4 → 7 since 1h ago. high 2 → 6");
    fireEvent.click(screen.getByRole("button", { name: "Refresh now" }));
    await waitFor(() => expect(api.post.mock.calls.map(([path]) => String(path))).toContain(`/custom-dashboards/${ID}/data-sources/sev/refresh`));
    vi.useRealTimers();
  });

  it("refreshes the source holding the snapshot a derived widget shows, and says when it cannot", async () => {
    const snapshot = { source_id: "sev", mode: "server", every: "15m", refreshed_at: "2026-10-23T10:00:00Z", publisher: "Alice" } as const;
    api.post.mockImplementation(async (path: string) => {
      if (String(path).endsWith("/refresh")) throw new Error("Too many refreshes");
      return { status: "ok", shape: "number", data: { value: 7, change: null }, fetched_at: "2026-10-23T10:00:00Z", open_url: null, snapshot };
    });
    const dashboard = normalizeDashboard({
      version: 2,
      title: "Board",
      dataSources: {
        sev: { type: "static", options: { shape: "number", data: { value: 7 } } },
        view: { type: "derived", options: { extends: "sev", steps: [] } },
      },
      visualizations: { big: { type: "xtm.singlevalue", title: "Open findings", dataSources: { primary: "view" } } },
      layout: {
        tabs: { items: [{ layoutId: "main", label: "Main" }] },
        layoutDefinitions: { main: { structure: [{ item: "big", position: { x: 0, y: 0, w: 4, h: 3 } }] } },
      },
    });
    renderWithProviders(<CustomDashboardView dashboard={dashboard} dashboardId={ID} revision={2} />, new QueryClient({ defaultOptions: { queries: { retry: false } } }));
    fireEvent.click(await screen.findByRole("button", { name: "Refresh now" }));
    await waitFor(() => expect(api.post.mock.calls.map(([path]) => String(path))).toContain(`/custom-dashboards/${ID}/data-sources/sev/refresh`));
    expect(await screen.findByText("The data could not be read again")).toBeTruthy();
  });

  it("says a snapshot not read yet waits for the server, not for an agent", async () => {
    api.post.mockResolvedValue({
      status: "error", code: "running", open_url: null, snapshot: { mode: "server", every: "15m", refreshed_at: null, publisher: "Alice" },
    } satisfies WidgetData);  // prettier-ignore
    draw(NUMBER, "number");
    expect(await screen.findByText("Read on the server in a moment.")).toBeTruthy();
  });

  it("draws a type it does not chart as a link to the platform", async () => {
    api.post.mockResolvedValue({ status: "error", code: "unsupported", open_url: OPEN } satisfies WidgetData);
    draw({ type: "map", perspective: "relationships", dataSelection: [{}] }, undefined);
    expect(screen.getByText("This widget is drawn in OpenCTI.")).toBeTruthy();
    await waitFor(() => expect(screen.getByRole("link", { name: "Open in OpenCTI" }).getAttribute("href")).toBe(OPEN));
  });

  it("shows its description and when it was read, and reads its annotation source", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-23T10:05:00Z"));
    api.post.mockImplementation((path: string) =>
      Promise.resolve(
        path.includes("/data-sources/")
          ? ({ status: "ok", shape: "rows", data: { columns: [{ key: "x", label: "x" }], rows: [], total: null }, fetched_at: "2026-10-23T10:00:00Z", open_url: null } satisfies WidgetData)
          : ({ status: "ok", shape: "number", data: { value: 7, change: null }, fetched_at: "2026-10-23T10:00:00Z", open_url: null } satisfies WidgetData),
      ),
    );
    const dashboard = normalizeDashboard({
      version: 2,
      title: "Board",
      dataSources: {
        answer: { type: "static", options: { shape: "number", data: { value: 7 } } },
        notes: { type: "static", options: { shape: "rows", data: { columns: [{ key: "x" }], rows: [] } } },
      },
      visualizations: {
        big: {
          type: "xtm.singlevalue",
          title: "Answer",
          description: "What the agent found",
          showLastUpdated: true,
          dataSources: { primary: "answer", annotation: "notes" },
        },
      },
      layout: {
        tabs: { items: [{ layoutId: "main", label: "Main" }] },
        layoutDefinitions: { main: { structure: [{ item: "big", position: { x: 0, y: 0, w: 4, h: 6 } }] } },
      },
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderWithProviders(<CustomDashboardView dashboard={dashboard} dashboardId={ID} revision={2} />, client);
    expect(await screen.findByText("What the agent found")).toBeTruthy();
    expect(await screen.findByText("Updated 5m ago", {}, { timeout: 15_000 })).toBeTruthy();
    expect(api.post.mock.calls.map(([path]) => path).sort()).toEqual([
      `/custom-dashboards/${ID}/data-sources/notes/data`,
      `/custom-dashboards/${ID}/widgets/big/data`,
    ]);
    vi.useRealTimers();
  }, 20_000);

  it("is a placeholder outside a stored dashboard, and with the studio off", () => {
    draw(NUMBER, "number", null);
    expect(screen.getByText("Live data from OpenCTI, read with your own access.")).toBeTruthy();
    flags.studio = false;
    draw(NUMBER, "number");
    expect(screen.getAllByText("Live data from OpenCTI, read with your own access.")).toHaveLength(2);
    expect(api.post).not.toHaveBeenCalled();
  });
});
