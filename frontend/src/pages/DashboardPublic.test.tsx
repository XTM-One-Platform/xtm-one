// A dashboard opened by its published link: drawn from its snapshot, with
// nothing read live (no API call but the link's own), no menu and no link
// into a platform; a link that does not answer says so.
import { afterEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), postAs: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => true }));

import DashboardPublic from "./DashboardPublic";

const TOKEN = "A".repeat(43);

const PAYLOAD = {
  title: "Ops",
  snapshot_at: new Date().toISOString(),
  definition: {
    version: 2,
    title: "Ops",
    dataSources: { open: { type: "static", name: "Open findings", options: { shape: "number", data: {} } } },
    visualizations: { kpi: { type: "xtm.singlevalue", title: "Open findings", dataSources: { primary: "open" } } },
    inputs: {},
    layout: {
      tabs: { items: [{ layoutId: "main", label: "Main" }] },
      layoutDefinitions: { main: { type: "grid", structure: [{ item: "kpi", position: { x: 0, y: 0, w: 6, h: 6 } }] } },
      globalInputs: [],
    },
  },
  snapshot: {
    widgets: { kpi: { status: "ok", shape: "number", fetched_at: new Date().toISOString(), open_url: null, data: { value: 37, change: 4 } } },
    sources: {},
  },
};

function draw() {
  return renderWithProviders(
    <MemoryRouter initialEntries={[`/public/dashboards/${TOKEN}`]}>
      <Routes>
        <Route path="/public/dashboards/:token" element={<DashboardPublic />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("DashboardPublic", () => {
  it("draws the snapshot and reads nothing live", async () => {
    const fetched: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        fetched.push(`${String(url)} ${init?.credentials}`);
        return new Response(JSON.stringify(PAYLOAD), { status: 200, headers: { "Content-Type": "application/json" } });
      }),
    );
    draw();
    expect(await screen.findByText("37", {}, { timeout: 10_000 })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Ops");
    expect(screen.getByTestId("dashboard.public.badge").textContent).toBe("Public");
    expect(fetched).toEqual([`/api/v1/public/dashboards/${TOKEN} omit`]);
    expect(api.post.mock.calls.length + api.postAs.mock.calls.length + api.get.mock.calls.length).toBe(0);
    expect(screen.queryByRole("button", { name: /^Data of / })).toBeNull();
    expect(screen.queryByText(/Open in/)).toBeNull();
  }, 20_000);

  it("says a link that does not answer opens nothing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "This dashboard is not available" }), { status: 404 })));
    draw();
    expect((await screen.findByTestId("dashboard.public.unavailable")).textContent).toContain("This link no longer opens a dashboard.");
  });
});
