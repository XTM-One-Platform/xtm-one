// A dashboard's Refresh: every widget read again from its platform (`fresh`, past the answer kept a minute),
// by hand or on the interval the viewer picked, kept in this browser for this dashboard.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
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
import { DashboardRefresh } from "./DashboardRefresh";
import { normalizeDashboard } from "./spec";

const ID = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const DEFINITION = {
  version: 2,
  title: "Board",
  dataSources: { n: { type: "static", options: { shape: "number", data: { value: 37, change: null } } } },
  visualizations: { open: { type: "xtm.singlevalue", title: "Open findings", dataSources: { primary: "n" } } },
  layout: {
    tabs: { items: [{ layoutId: "main", label: "Main" }] },
    layoutDefinitions: { main: { structure: [{ item: "open", position: { x: 0, y: 0, w: 6, h: 6 } }] } },
  },
};

// Each test its own dashboard: a refresh keeps its reads fresh for a few seconds.
function draw(id = ID) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderWithProviders(
    <MemoryRouter>
      <DashboardRefresh dashboardId={id} />
      <CustomDashboardView dashboard={normalizeDashboard(DEFINITION)} dashboardId={id} revision={1} />
    </MemoryRouter>,
    client,
  );
}

const reads = () => api.post.mock.calls.filter(([url]) => String(url).endsWith("/widgets/open/data")).map(([, body]) => body);

describe("DashboardRefresh", () => {
  beforeEach(() => {
    api.post.mockReset();
    api.get.mockReset();
    api.get.mockResolvedValue({});
    api.post.mockResolvedValue({ status: "ok", shape: "number", data: { value: 37, change: null }, fetched_at: "2026-10-26T10:00:00Z", open_url: null });
    window.localStorage.clear();
  });
  afterEach(() => vi.useRealTimers());

  it("reads every widget again from its platform when asked", async () => {
    draw();
    await waitFor(() => expect(reads()).toEqual([{}]));
    await waitFor(() => expect((screen.getByRole("button", { name: "Refresh" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(reads()).toEqual([{}, { fresh: true }]));
  });

  it("refreshes on the interval picked, and keeps it for this dashboard", async () => {
    draw(OTHER);
    await waitFor(() => expect(reads()).toHaveLength(1));
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(screen.getByRole("combobox", { name: "Auto-refresh" }));
    fireEvent.click(within(screen.getByRole("listbox")).getByRole("option", { name: "Every 5 minutes" }));
    expect(window.localStorage.getItem(`xtm-dashboard-refresh:${OTHER}`)).toBe("5");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000);
    });
    await waitFor(() => expect(reads()).toEqual([{}, { fresh: true }]));
  });
});
