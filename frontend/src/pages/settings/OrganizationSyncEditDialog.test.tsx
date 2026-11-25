// SPDX-License-Identifier: Apache-2.0

// The filters of a saved sync in its Edit dialog (#1982 S4): the same fields as the add stepper, a change previewed
// with POST /syncs/{id}/preview before it is saved, and a schedule-only edit saved at once.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";

import type { OrganizationSync } from "@/hooks/api/useOrganizationContextApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { SyncEditDialog } from "./OrganizationSyncEditDialog";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

const sync = (patch: Partial<OrganizationSync> = {}): OrganizationSync => ({
  id: "s-1",
  section: "assets",
  source: "openaev",
  filters: {},
  batch_type: "",
  remove_gone: false,
  direction: "outbound",
  frequency: "daily",
  hour: 6,
  minute: 0,
  weekday: 0,
  timezone: "Europe/Paris",
  enabled: true,
  paused_reason: "",
  owner_email: "admin@example.com",
  next_run_at: null,
  last_run_at: null,
  last_result: null,
  ...patch,
});

const CHANGE = { direction: "outbound", counts: { enter: 1, update: 2, leave: 1, kept: 0 }, enter: ["web-01"], update: [], leave: ["old-db"], kept: [], error: null };

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(async (path: string) => {
    if (path === "/organization-context/syncs/directions") return [{ section: "assets", source: "openaev", directions: ["inbound", "outbound"], reason: "" }];
    if (path === "/auth/me") return { id: "u-1", email: "me.admin@example.com", full_name: "Me Admin" };
    return {};
  });
  post.mockImplementation(async (path: string) => {
    if (path === "/organization-context/syncs/s-1/preview") return CHANGE;
    throw new Error(`unexpected POST ${path}`);
  });
});

describe("SyncEditDialog, filters (#1982 S4)", () => {
  it("previews a filter change, lists the rows the next run removes, then saves the new filters", async () => {
    const onSave = vi.fn();
    renderWithProviders(<SyncEditDialog sync={sync()} open saving={false} onClose={() => {}} onSave={onSave} />);
    const filters = await screen.findByTestId("org-sync-edit-filters");
    fireEvent.change(within(filters).getByRole("textbox"), { target: { value: "prod" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview the change" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/organization-context/syncs/s-1/preview", { filters: { search: "prod" }, batch_type: "", remove_gone: false }),
    );
    expect((await screen.findByTestId("org-sync-edit-change.counts")).textContent).toContain("To remove1");
    expect(screen.getByTestId("org-sync-edit-change.leave").textContent).toContain("old-db");
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Save the change" }));
    expect(onSave).toHaveBeenCalledWith(expect.anything(), true, "outbound", { search: "prod" });
  });

  it("asks for a new preview after another edit", async () => {
    renderWithProviders(<SyncEditDialog sync={sync()} open saving={false} onClose={() => {}} onSave={() => {}} />);
    const box = within(await screen.findByTestId("org-sync-edit-filters")).getByRole("textbox");
    fireEvent.change(box, { target: { value: "prod" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview the change" }));
    await screen.findByRole("button", { name: "Save the change" });
    fireEvent.change(box, { target: { value: "production" } });
    expect(screen.getByRole("button", { name: "Preview the change" })).toBeTruthy();
    expect(screen.queryByTestId("org-sync-edit-change")).toBeNull();
  });

  it("saves a schedule-only edit at once, without a preview or a filter change", async () => {
    const onSave = vi.fn();
    renderWithProviders(<SyncEditDialog sync={sync({ filters: { search: "prod", criticality: [] } })} open saving={false} onClose={() => {}} onSave={onSave} />);
    await screen.findByTestId("org-sync-edit-filters");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(post).not.toHaveBeenCalled();
    expect(onSave).toHaveBeenCalledWith(expect.anything(), true, "outbound", undefined);
  });

  it("keeps the outbound filters usable after the platform's own values could not be read inbound", async () => {
    get.mockImplementation(async (path: string) => {
      if (path === "/organization-context/syncs/directions") return [{ section: "assets", source: "opencti", directions: ["inbound", "outbound"], reason: "" }];
      if (path === "/organization-context/import/sources")
        return { sections: [{ section: "assets", field: "assets_exposure", sources: [{ source: "opencti", available: true, reason: "", filters: ["labels", "category"] }] }] };
      if (path.startsWith("/organization-context/import/options")) return { options: {}, unavailable: { labels: "not_connected", category: "not_connected" } };
      if (path === "/auth/me") return { id: "u-1", email: "me.admin@example.com", full_name: "Me Admin" };
      return {};
    });
    renderWithProviders(<SyncEditDialog sync={sync({ source: "opencti", direction: "inbound" })} open saving={false} onClose={() => {}} onSave={() => {}} />);
    expect((await screen.findAllByText("OpenCTI is not connected for your account.")).length).toBeGreaterThan(0);
    fireEvent.click(within(screen.getByTestId("org-sync-edit-direction")).getByRole("radio", { name: /Outbound/ }));
    await waitFor(() => expect(screen.queryByText("OpenCTI is not connected for your account.")).toBeNull());
  });

  it("draws no filter for Sync all", async () => {
    renderWithProviders(<SyncEditDialog sync={sync({ section: "all", source: "" })} open saving={false} onClose={() => {}} onSave={() => {}} />);
    expect(await screen.findByRole("heading", { name: "Edit the sync" })).toBeTruthy();
    expect(screen.queryByTestId("org-sync-edit-filters")).toBeNull();
  });
});
