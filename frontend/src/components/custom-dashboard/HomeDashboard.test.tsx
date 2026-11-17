// The custom dashboard chosen as one's dashboard: shown in place of the
// platform's, the way back to it, and the platform's again when it cannot be read.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { CustomDashboard } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import HomeDashboard from "./HomeDashboard";

const ID = "11111111-1111-4111-8111-111111111111";
const DASHBOARD: CustomDashboard = {
  id: ID,
  title: "Board brief",
  tags: [],
  widget_count: 1,
  revision: 3,
  created_at: "2026-10-20T08:00:00Z",
  updated_at: "2026-10-20T08:00:00Z",
  assignment_run_id: null,
  conversation_id: null,
  spec: { title: "Board brief", widgets: [{ kind: "score", id: "s", label: "Exposure score", value: 58, max: 100 }] },
  widgets: [{ kind: "score", id: "s", label: "Exposure score", value: 58, max: 100 }],
  can_manage: true,
  can_use_as_home: true,
  user_id: "33333333-3333-4333-8333-333333333333",
  is_company_managed: false,
  is_shared: false,
  permission: "owner",
  shared_user_ids: [],
  shared_user_permissions: {},
  shared_group_ids: [],
  shared_group_permissions: {},
  access_restricted: false,
};

function draw() {
  // The app's own client: any other failed query throws to the nearest boundary.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, throwOnError: true } } });
  return renderWithProviders(
    <MemoryRouter>
      <HomeDashboard dashboardId={ID} fallback={<p>Platform dashboard</p>} />
    </MemoryRouter>,
    client,
  );
}

describe("HomeDashboard", () => {
  beforeEach(() => {
    api.get.mockReset();
    api.put.mockReset();
  });

  it("shows the chosen dashboard, and leads back to the standard one", async () => {
    api.get.mockResolvedValue(DASHBOARD);
    api.put.mockResolvedValue({ status: "ok" });
    draw();
    expect(await screen.findByRole("heading", { name: "Board brief" })).toBeTruthy();
    expect(screen.getByText("Exposure score")).toBeTruthy();
    expect(screen.queryByText("Platform dashboard")).toBeNull();
    expect(screen.getByRole("link", { name: "Open the dashboard" }).getAttribute("href")).toBe(`/dashboards/${ID}`);
    fireEvent.click(screen.getByRole("button", { name: "Use the standard dashboard" }));
    await waitFor(() => expect(api.put.mock.calls).toEqual([["/settings/preferences", { home_dashboard_id: null }]]));
  });

  it("is the platform's dashboard when the chosen one cannot be read", async () => {
    api.get.mockRejectedValue(Object.assign(new Error("Dashboard not found"), { status: 404 }));
    draw();
    expect(await screen.findByText("Platform dashboard")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Use the standard dashboard" })).toBeNull();
  });
});
