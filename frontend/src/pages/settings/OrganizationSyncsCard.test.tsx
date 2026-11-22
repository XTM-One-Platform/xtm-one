// Settings > Organization > Syncs (#11913): the scheduled imports, their last run, status, Run now, switch and Sync all.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { OrganizationSync } from "@/hooks/api/useOrganizationContextApi";
import { OrganizationSyncsCard } from "./OrganizationSyncsCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);
const put = vi.mocked(api.put);

const sync = (patch: Partial<OrganizationSync>): OrganizationSync => ({
  id: "s-1",
  section: "assets",
  source: "opencti",
  filters: { labels: ["dmz"] },
  batch_type: "generic_asset",
  remove_gone: false,
  frequency: "daily",
  hour: 6,
  minute: 0,
  weekday: 0,
  timezone: "Europe/Paris",
  enabled: true,
  paused_reason: "",
  owner_email: "admin@example.com",
  next_run_at: "2099-01-01T05:00:00Z",
  last_run_at: "2026-11-14T05:00:00Z",
  last_result: { added: 3, updated: 1, removed: 0, version: 9, error: null },
  ...patch,
});

function render() {
  renderWithProviders(
    <MemoryRouter>
      <OrganizationSyncsCard />
    </MemoryRouter>,
  );
}

const rows = async () => {
  await waitFor(() => expect(screen.getAllByTestId("organization-sync-row").length).toBeGreaterThan(0));
  return screen.getAllByTestId("organization-sync-row");
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe("OrganizationSyncsCard (#11913)", () => {
  it("reads only its location's syncs when given a scope (#1134 S2)", async () => {
    get.mockResolvedValue([sync({ section: "crown_jewels", source: "opencrq" })]);
    renderWithProviders(
      <MemoryRouter>
        <OrganizationSyncsCard scope="profile" />
      </MemoryRouter>,
    );
    await rows();
    expect(get).toHaveBeenCalledWith("/organization-context/syncs?scope=profile");
  });

  it("reads every sync without a scope", async () => {
    get.mockResolvedValue([sync({})]);
    render();
    await rows();
    expect(get).toHaveBeenCalledWith("/organization-context/syncs");
  });

  it("lists each sync with its schedule, last run counts and status", async () => {
    get.mockResolvedValue([sync({}), sync({ id: "s-2", section: "crown_jewels", source: "opencrq", filters: {}, paused_reason: "OpenCRQ: no token for this administrator" })]);
    render();
    const [first, second] = await rows();
    expect(first.textContent).toContain("Assets");
    expect(first.textContent).toContain("Every day at 06:00 (Europe/Paris)");
    expect(first.textContent).toContain("3 added, 1 updated, 0 removed");
    expect(first.textContent).toContain("Enabled");
    expect(second.textContent).toContain("Paused");
    expect(second.textContent).toContain("No filter: every entity");
  });

  it("reads the filters in the import stepper's words, ids counted, never the raw API keys (UX r1 B2)", async () => {
    get.mockResolvedValue([
      sync({ filters: { labels: ["id-prod", "id-dmz"], category: ["HOST"], criticality: ["high"], internet_facing: "yes", created_after: "2026-01-15" } }),
    ]);
    render();
    const [row] = await rows();
    const text = row.textContent ?? "";
    expect(text).toContain("Labels: 2 values");
    expect(text).toContain("Category: Host");
    expect(text).toContain("Criticality: High (criticality)");
    expect(text).toContain("Exposure: Internet-facing");
    expect(text).not.toContain("id-prod");
    expect(text).not.toContain("internet facing");
    expect(text).not.toContain("created after");
  });

  it("runs a sync now and switches one off with the rest of its definition kept", async () => {
    get.mockResolvedValue([sync({})]);
    post.mockResolvedValue(sync({}));
    put.mockResolvedValue(sync({ enabled: false }));
    render();
    const [row] = await rows();
    fireEvent.click(within(row).getByRole("button", { name: "Run now" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/organization-context/syncs/s-1/run", {}));
    fireEvent.click(within(row).getByRole("switch", { name: "Enabled" }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/organization-context/syncs/s-1", expect.objectContaining({ section: "assets", source: "opencti", filters: { labels: ["dmz"] }, enabled: false })),
    );
  });

  it("offers Sync all once there is a section sync, and creates it with no source", async () => {
    get.mockResolvedValue([sync({})]);
    post.mockResolvedValue(sync({ id: "all-1", section: "all", source: "" }));
    render();
    await rows();
    fireEvent.click(screen.getByRole("button", { name: "Add Sync all" }));
    fireEvent.click(await screen.findByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/organization-context/syncs", expect.objectContaining({ section: "all", source: "", filters: {}, enabled: true })),
    );
  });

  it("warns once above the table that the context is shared, and names whose permissions each sync runs with (#11994)", async () => {
    get.mockResolvedValue([sync({}), sync({ id: "s-2", owner_email: "other.admin@example.com" })]);
    render();
    const [first, second] = await rows();
    expect(screen.getAllByTestId("org-shared-context.source")).toHaveLength(1);
    expect(screen.getByText("Shared organization context")).toBeTruthy();
    expect(screen.getByText(/organization segregation and markings, are not carried over/)).toBeTruthy();
    expect(screen.getByText(/Agent integrations, dashboards and every other feature keep each user's own permissions/)).toBeTruthy();
    expect(first.textContent).toContain("admin@example.com");
    expect(second.textContent).toContain("other.admin@example.com");
  });

  it("says in the edit dialog whose permissions a sync saved by someone else runs with, and that saving makes it run as me (#11994, UX r1 B2)", async () => {
    get.mockImplementation((path: string) =>
      Promise.resolve(path === "/auth/me" ? { email: "me.admin@example.com" } : [sync({ owner_email: "other.admin@example.com" })]),
    );
    render();
    const [row] = await rows();
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    await waitFor(() =>
      expect(screen.getByTestId("org-shared-context.actor").textContent).toBe("Runs as other.admin@example.com. Saving makes it run as me.admin@example.com."),
    );
  });

  it("says only whose permissions a sync runs with when I saved it myself (#11994)", async () => {
    get.mockImplementation((path: string) => Promise.resolve(path === "/auth/me" ? { email: "admin@example.com" } : [sync({})]));
    render();
    const [row] = await rows();
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    await waitFor(() => expect(screen.getByTestId("org-shared-context.actor").textContent).toBe("Runs as admin@example.com"));
  });

  it("says how to add a sync when there is none, and offers no Sync all", async () => {
    get.mockResolvedValue([]);
    render();
    expect(await screen.findByText("No sync yet: imports run once. Turn on Keep in sync on an import's preview to schedule it.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add Sync all" })).toBeNull();
  });
});
