// The studio's Packs panel: a pack reading a platform with several usable
// integrations asks which one; one with none says it cannot be inserted;
// "Insert" sends the choice, below the selected widget, at the revision read.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CustomDashboard, WidgetPack } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), postForm: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));

import { DashboardPacksPanel } from "./DashboardPacksPanel";

const pack = (overrides: Partial<WidgetPack>): WidgetPack => ({
  id: "p1", name: "Threat figures", description: null, tags: [], origin: "upload", sources: ["opencti"],
  widget_count: 2, builtin: false, is_company_managed: false, user_id: "u1", can_manage: true, notes: [],
  created_at: null, updated_at: null, ...overrides,
});  // fmt: skip

const DASHBOARD = { id: "d1", revision: 3, can_manage: true } as unknown as CustomDashboard;

function serve(integrations: { id: string; provider: string; name: string }[]) {
  api.get.mockImplementation(async (path: string) => {
    if (path.endsWith("/integrations")) return integrations;
    return [pack({}), pack({ id: "p2", name: "Risk", sources: ["opencrq"] })];
  });
}

describe("DashboardPacksPanel", () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset();
  });

  it("asks which integration when there are several, before inserting below the selection", async () => {
    serve([
      { id: "i1", provider: "opencti", name: "Lab" },
      { id: "i2", provider: "opencti", name: "Prod" },
    ]);
    api.post.mockResolvedValue({ inserted: ["reports"], dashboard: {} });
    const inserted = vi.fn();
    renderWithProviders(<DashboardPacksPanel dashboard={DASHBOARD} afterWidgetId="score" onInserted={inserted} />);
    fireEvent.click(await screen.findByRole("button", { name: /Threat figures/ }));
    const insert = await screen.findByRole("button", { name: "Insert below the selected widget" });
    expect((insert as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("OpenCTI to read from")).toBeTruthy();
  });

  it("says a pack reading a platform the person cannot use cannot be inserted", async () => {
    serve([{ id: "i1", provider: "opencti", name: "Lab" }]);
    renderWithProviders(<DashboardPacksPanel dashboard={DASHBOARD} afterWidgetId={null} onInserted={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /Risk/ }));
    expect(await screen.findByText("You can use no OpenCRQ integration: its widgets cannot be inserted.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Insert into this dashboard" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("inserts with the only integration implied", async () => {
    serve([{ id: "i1", provider: "opencti", name: "Lab" }]);
    api.post.mockResolvedValue({ inserted: ["reports", "note-2"], dashboard: {} });
    const inserted = vi.fn();
    renderWithProviders(<DashboardPacksPanel dashboard={DASHBOARD} afterWidgetId={null} onInserted={inserted} />);
    fireEvent.click(await screen.findByRole("button", { name: /Threat figures/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Insert into this dashboard" }));
    await waitFor(() =>
      expect(api.post.mock.calls).toEqual([
        ["/dashboard-widget-packs/p1/insert", { dashboard_id: "d1", revision: 3, bindings: {} }],
      ]),
    );
    await waitFor(() => expect(inserted).toHaveBeenCalledWith(["reports", "note-2"]));
  });
});
