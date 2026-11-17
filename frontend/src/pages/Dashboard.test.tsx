// The Copilot dashboard on the grid: its eleven widgets are drawn, the
// Customize button opens the editor, and a change is saved as the Copilot
// layout - never as the command center's.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { DashboardStats, User } from "@/types";
import Dashboard from "./Dashboard";

const me = vi.hoisted(() => ({ user: null as null | Partial<User> }));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), put: vi.fn(async () => ({ status: "ok" })) },
}));
vi.mock("@/pages/xtm-one/DashboardNotices", () => ({ DashboardNotices: () => null }));
vi.mock("@/hooks/api/useAuthApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAuthApi")>()),
  useMe: () => ({ data: me.user }),
}));
vi.mock("@/hooks/api/useMiscApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useMiscApi")>()),
  useDashboardStats: () => ({ data: STATS, isLoading: false }),
}));
vi.mock("@/hooks/api/useAssignmentApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAssignmentApi")>()),
  useAllAssignmentRuns: () => ({ data: [] }),
}));
vi.mock("@/hooks/api/useChatApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useChatApi")>()),
  useConversations: () => ({ data: [] }),
}));
vi.mock("@/hooks/api/useIntegrationApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useIntegrationApi")>()),
  useIntegrations: () => ({ data: [] }),
}));
// happy-dom lays nothing out: the grid's container is wide enough for the grid.
vi.mock("react-grid-layout", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-grid-layout")>()),
  useContainerWidth: () => ({ width: 1280, mounted: true, containerRef: { current: null } }),
}));

const STATS = {
  agents: { total: 3, active: 3 },
  assignments: { total: 2, active: 2 },
  runs: { total: 9, completed: 8, failed: 1, today: 1, this_month: 9, running: 0 },
  integrations_connected: 1,
  knowledge_bases: 2,
  document_count: 23,
  conversation_count: 4,
  message_count: 40,
  agent_summaries: [],
  recent_digests: [],
  diffs: {
    agents_prev_month: 2,
    conversations_prev_month: 3,
    runs_prev_month_count: 7,
    integrations_prev_month: 1,
    documents_prev_month: 20,
  },
} as unknown as DashboardStats;

function Location() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

function dashboard() {
  renderWithProviders(
    <MemoryRouter>
      <Dashboard />
      <Location />
    </MemoryRouter>,
  );
}

const KPI_TILES = [
  ["Agents", "/agents"],
  ["Conversations", "/chat"],
  ["Agentic Executions", "/operations/executions"],
  ["Integrations", "/integrations"],
  ["Documents", "/knowledge"],
] as const;

/** The KPI tile whose label is `label`: the button its label sits in. */
function kpiTile(label: string): HTMLElement {
  const tiles = screen
    .getAllByText(label, { selector: "p" })
    .map((p) => p.closest<HTMLElement>("[role='button']"))
    .filter((el): el is HTMLElement => el !== null);
  expect(tiles).toHaveLength(1);
  return tiles[0];
}

beforeEach(() => {
  me.user = { id: "u-1", full_name: "Ada Lovelace", copilot_dashboard_layout: null };
  vi.mocked(api.put).mockClear();
});

describe("Copilot Dashboard", () => {
  it("draws its thirteen widgets, the Value row and the Fleets widget among them (#9836 R6b, #12009, #12063)", () => {
    dashboard();
    // The greeting depends on the time of day: only the name is fixed.
    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain("Ada");
    for (const title of ["Agents", "Conversations", "Agentic Executions", "Integrations", "Documents", "Last Conversations", "Live Activity", "Latest Results & Digests"]) {
      expect(screen.getAllByText(title).length).toBeGreaterThan(0);
    }
    expect(screen.getByTestId("home-value-card")).toBeTruthy();
    expect(screen.getByTestId("home-value-drivers-card")).toBeTruthy();
    expect(screen.getByTestId("copilot-fleets")).toBeTruthy();
    expect(document.querySelectorAll(".react-grid-item")).toHaveLength(13);
  });

  it("opens the editor with Customize, and saves a removed widget as the Copilot layout", async () => {
    dashboard();
    fireEvent.click(screen.getByRole("button", { name: "Customize" }));
    expect(screen.getByText("Customize your dashboard")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Remove Latest Results & Digests" }));

    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    const [path, body] = vi.mocked(api.put).mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe("/settings/preferences");
    expect(Object.keys(body)).toEqual(["copilot_dashboard_layout"]);
    const layout = body.copilot_dashboard_layout as { items: { i: string }[]; hidden: string[] };
    expect(layout.hidden).toEqual(["digests"]);
    expect(layout.items.map((item) => item.i)).not.toContain("digests");
    expect(layout.items).toHaveLength(12);
  });

  it("draws the saved layout again when the write is refused", async () => {
    vi.mocked(api.put).mockRejectedValueOnce(new Error("422"));
    dashboard();
    fireEvent.click(screen.getByRole("button", { name: "Customize" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove Latest Results & Digests" }));
    expect(document.querySelectorAll(".react-grid-item")).toHaveLength(12);

    await waitFor(() => expect(document.querySelectorAll(".react-grid-item")).toHaveLength(13));
    expect(api.put).toHaveBeenCalledTimes(1);
  });

  it("draws a saved layout: a removed widget is not drawn, and the catalog offers it back", () => {
    me.user = {
      id: "u-1",
      full_name: "Ada Lovelace",
      copilot_dashboard_layout: { items: [], hidden: ["live_activity"] },
    };
    dashboard();
    expect(document.querySelectorAll(".react-grid-item")).toHaveLength(12);
    expect(screen.queryByText("Live Activity")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Customize" }));
    // The design system's menu opens on pointer down, as Radix menus do, not on click.
    fireEvent.pointerDown(screen.getByRole("button", { name: "Add a widget" }), { button: 0, ctrlKey: false });
    expect(within(screen.getByRole("menu")).getByRole("menuitem", { name: "Live Activity" })).toBeTruthy();
  });

  it.each(KPI_TILES)("opens the %s KPI tile's page with the keyboard", (label, path) => {
    dashboard();
    const tile = kpiTile(label);
    expect(tile.tabIndex).toBe(0);
    tile.focus();
    expect(document.activeElement).toBe(tile);
    fireEvent.keyDown(tile, { key: "Enter" });
    expect(screen.getByTestId("location").textContent).toBe(path);
  });

  it("opens a KPI tile's page with Space, and with a click as before", () => {
    dashboard();
    fireEvent.keyDown(kpiTile("Documents"), { key: " " });
    expect(screen.getByTestId("location").textContent).toBe("/knowledge");
    fireEvent.click(kpiTile("Agents"));
    expect(screen.getByTestId("location").textContent).toBe("/agents");
  });

  it("keeps the KPI tiles inert while the dashboard is edited", () => {
    dashboard();
    expect(kpiTile("Agents").closest("[inert]")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Customize" }));
    expect(kpiTile("Agents").closest("[inert]")).not.toBeNull();
  });
});
