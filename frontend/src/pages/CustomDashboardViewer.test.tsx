// The dashboard page: making it one's dashboard and undoing it, and what each
// tier is offered (share, rearrange, history, delete) - the server decides the
// tier, the page only follows `permission` / `can_manage` / `can_use_as_home`.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import CustomDashboardViewer from "./CustomDashboardViewer";

const ID = "11111111-1111-4111-8111-111111111111";

type Tier = "owner" | "config" | "read_only";

function serve({
  canManage,
  home,
  permission = canManage ? "owner" : "read_only",
  asHome = canManage,
}: {
  canManage: boolean;
  home: string | null;
  permission?: Tier;
  asHome?: boolean;
}) {
  api.get.mockImplementation(async (path: string) => {
    if (path === "/auth/me") return { id: "u1", full_name: "Sofia", home_dashboard_id: home };
    if (path.endsWith("/versions")) {
      return [
        { revision: 2, title: "Board brief", widget_count: 1, source: "manual", summary: "removed note", author_name: "Ada", created_at: null },
        { revision: 1, title: "Board brief", widget_count: 2, source: "agent", summary: null, author_name: "Sofia", created_at: null },
      ];
    }
    if (path.endsWith("/versions/1")) {
      return { revision: 1, title: "Board brief", widget_count: 2, source: "agent", spec: { title: "Board brief", widgets: [{ kind: "text", id: "old", body: "Earlier text" }] } };
    }
    return {
      id: ID, title: "Board brief", widget_count: 1, revision: 2, created_at: null, updated_at: null,
      assignment_run_id: null, conversation_id: null, can_manage: canManage, can_use_as_home: asHome,
      permission, is_company_managed: false, is_shared: permission !== "owner", user_id: "owner-1",
      shared_user_ids: [], shared_user_permissions: {}, shared_group_ids: [], shared_group_permissions: {},
      access_restricted: false,
      spec: { title: "Board brief", widgets: [{ kind: "text", id: "t", body: "Hello" }] },
    };
  });
}

function draw() {
  return renderWithProviders(
    <MemoryRouter initialEntries={[`/dashboards/${ID}`]}>
      <Routes>
        <Route path="/dashboards/:id" element={<CustomDashboardViewer />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("CustomDashboardViewer - your dashboard", () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset();
    api.put.mockResolvedValue({ status: "ok" });
  });

  it("makes it the owner's dashboard", async () => {
    serve({ canManage: true, home: null });
    draw();
    fireEvent.click(await screen.findByRole("button", { name: "Use as my dashboard" }));
    await waitFor(() => expect(api.put.mock.calls).toEqual([["/settings/preferences", { home_dashboard_id: ID }]]));
  });

  it("says it already is, and undoes it", async () => {
    serve({ canManage: true, home: ID });
    draw();
    const chosen = await screen.findByRole("button", { name: "Your dashboard" });
    expect(chosen.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(chosen);
    await waitFor(() => expect(api.put.mock.calls).toEqual([["/settings/preferences", { home_dashboard_id: null }]]));
  });

  it("offers nothing on a dashboard an admin reads by their role", async () => {
    serve({ canManage: false, home: null, asHome: false });
    draw();
    expect(await screen.findByRole("heading", { name: "Board brief" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Use as my dashboard" })).toBeNull();
  });
});

describe("CustomDashboardViewer - tiers", () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset();
  });

  it("lets a viewer open the history and use it as their dashboard, nothing more", async () => {
    serve({ canManage: false, home: null, permission: "read_only", asHome: true });
    draw();
    expect(await screen.findByText("Shared with you to view: you cannot change it.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Use as my dashboard" })).toBeTruthy();
    for (const name of ["Share", "Rearrange", "Refine with the assistant", "Delete dashboard"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    fireEvent.click(screen.getByRole("button", { name: "Dashboard history" }));
    expect(await screen.findByText("removed note")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Restore this version" })).toBeNull();
  });

  it("lets an editor share and restore, but not delete", async () => {
    serve({ canManage: true, home: null, permission: "config" });
    api.post.mockResolvedValue({});
    draw();
    expect(await screen.findByRole("button", { name: "Share" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Delete dashboard" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Dashboard history" }));
    fireEvent.click(await screen.findByRole("button", { name: /Revision 1/ }));
    expect(await screen.findByText("Earlier text")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Restore this version" }));
    await waitFor(() =>
      expect(api.post.mock.calls).toEqual([[`/custom-dashboards/${ID}/versions/1/restore`, { revision: 2 }]]),
    );
  });

  it("shows the owner every control", async () => {
    serve({ canManage: true, home: null, permission: "owner" });
    draw();
    for (const name of ["Share", "Rearrange", "Refine with the assistant", "Delete dashboard", "Dashboard history"]) {
      expect(await screen.findByRole("button", { name })).toBeTruthy();
    }
  });

  it("folds the navigation to its rail while a dashboard asking for it is open, and shows it fullscreen", async () => {
    const { NavPrefsProvider, useNavPrefs } = await import("@/hooks/useNavPrefs");
    function Rail() {
      return <span data-testid="rail">{useNavPrefs().rail ? "rail" : "full"}</span>;
    }
    const collapsing = {
      version: 2,
      title: "Board brief",
      visualizations: { t: { type: "xtm.text", options: { body: "Hello" } } },
      layout: { tabs: { items: [{ layoutId: "main", label: "Main" }] }, layoutDefinitions: { main: { structure: [{ item: "t", position: { x: 0, y: 0, w: 12, h: 4 } }] } } },
      applicationProperties: { collapseNavigation: true },
    };
    api.get.mockImplementation(async (path: string) =>
      path === "/auth/me"
        ? { id: "u1", full_name: "Sofia", home_dashboard_id: null }
        : {
            id: ID, title: "Board brief", widget_count: 1, revision: 2, created_at: null, updated_at: null,
            assignment_run_id: null, conversation_id: null, can_manage: false, can_use_as_home: false,
            permission: "read_only", is_company_managed: false, is_shared: true, user_id: "owner-1",
            shared_user_ids: [], shared_user_permissions: {}, shared_group_ids: [], shared_group_permissions: {},
            access_restricted: false, spec: collapsing,
          },
    );
    renderWithProviders(
      <NavPrefsProvider>
        <Rail />
        <MemoryRouter initialEntries={[`/dashboards/${ID}`]}>
          <Routes>
            <Route path="/dashboards/:id" element={<CustomDashboardViewer />} />
          </Routes>
        </MemoryRouter>
      </NavPrefsProvider>,
    );
    await screen.findByText("Hello");
    expect(screen.getByTestId("rail").textContent).toBe("rail");
    expect(localStorage.getItem("copilot-nav:rail")).toBeNull();
    fireEvent.click(screen.getByTestId("dashboard.fullscreen"));
    expect(await screen.findByRole("button", { name: "Leave fullscreen" })).toBeTruthy();
  });
});
