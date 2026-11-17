// Interactions and visibility as a person meets them: a row clicked sets a
// token, which shows the panel waiting for it; a panel with no data is
// hidden and the grid closes up; a click opens another dashboard with the
// clicked value as its token.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => true }));
vi.mock("react-grid-layout", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-grid-layout")>()),
  useContainerWidth: () => ({ width: 1280, mounted: true, containerRef: { current: null } }),
}));

import { CustomDashboardView } from "./CustomDashboardView";
import { normalizeDashboard } from "./spec";

const ID = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const rows = { columns: [{ key: "host", label: "Host" }, { key: "sev", label: "Severity" }], rows: [{ host: "vpn-01", sev: "critical" }], total: 1 };

const DEFINITION = {
  version: 2,
  title: "Board",
  dataSources: {
    hosts: { type: "static", options: { shape: "rows", data: rows } },
    detail: { type: "static", options: { shape: "number", data: { value: 3 } } },
    nothing: { type: "static", options: { shape: "distribution", data: { items: [] } } },
  },
  visualizations: {
    table: {
      type: "xtm.table",
      title: "Hosts",
      dataSources: { primary: "hosts" },
      eventHandlers: [
        { type: "drilldown.setToken", options: { tokens: [{ token: "host", key: "row.host.value" }] } },
      ],
    },
    card: {
      type: "xtm.singlevalue",
      title: "Findings on $host$",
      dataSources: { primary: "detail" },
      containerOptions: { visibility: { showConditions: ["picked"] } },
      eventHandlers: [{ type: "drilldown.linkToDashboard", options: { dashboardId: OTHER, tokens: [{ token: "host", value: "$host$" }] } }],
    },
    empty: { type: "xtm.pie", title: "Nothing here", dataSources: { primary: "nothing" }, containerOptions: { visibility: { hideWhenNoData: true } } },
  },
  expressions: { conditions: { picked: { name: "A host is picked", value: "isSet($host$)" } } },
  layout: {
    tabs: { items: [{ layoutId: "main", label: "Main" }] },
    layoutDefinitions: {
      main: {
        structure: [
          { item: "table", position: { x: 0, y: 0, w: 6, h: 8 } },
          { item: "card", position: { x: 6, y: 0, w: 6, h: 6 } },
          { item: "empty", position: { x: 0, y: 8, w: 6, h: 8 } },
        ],
      },
    },
  },
};

function ok(shape: string, data: unknown) {
  return { status: "ok", shape, data, fetched_at: "2026-10-25T10:00:00Z", open_url: null };
}

describe("dashboard interactions", () => {
  beforeEach(() => {
    api.post.mockReset();
    api.get.mockReset();
    api.get.mockResolvedValue({ id: "u1", email: "marcus@example.com", full_name: "Marcus", timezone: "Europe/Paris" });
    api.post.mockImplementation(async (url: string) => {
      if (url.includes("/widgets/table/")) return ok("rows", rows);
      if (url.includes("/widgets/card/")) return ok("number", { value: 3, change: null });
      return ok("distribution", { items: [] });
    });
  });

  it("shows a panel once a click sets the token it waits for, and opens another dashboard with it", async () => {
    const onNavigate = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderWithProviders(
      <MemoryRouter>
        <CustomDashboardView dashboard={normalizeDashboard(DEFINITION)} dashboardId={ID} revision={1} onNavigate={onNavigate} />
      </MemoryRouter>,
      client,
    );
    expect(await screen.findByText("vpn-01", {}, { timeout: 15_000 })).toBeTruthy();
    expect(screen.queryByRole("region", { name: /^Findings on/ })).toBeNull();
    // The panel with no data reads its source off the grid, and is not on it.
    await waitFor(() => expect(api.post.mock.calls.some(([url]) => String(url).includes("/widgets/empty/"))).toBe(true));
    await waitFor(() => expect(screen.queryByText("Nothing here")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: /vpn-01/ }));
    expect(await screen.findByText("Findings on vpn-01")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "3" }, { timeout: 15_000 }));
    expect(onNavigate.mock.calls).toEqual([[`/dashboards/${OTHER}?form.host=vpn-01`]]);
  }, 40_000);
});
