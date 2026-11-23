// SPDX-License-Identifier: Apache-2.0

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { OrganizationSource, OrganizationSync } from "@/hooks/api/useOrganizationContextApi";
import { OrganizationSourcesSyncs } from "./OrganizationSourcesSyncs";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);
const put = vi.mocked(api.put);
const del = vi.mocked(api.delete);

const sync = (patch: Partial<OrganizationSync> = {}): OrganizationSync => ({
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

const source = (patch: Partial<OrganizationSource> = {}): OrganizationSource => ({
  key: "opencti",
  kind: "platform",
  display_name: "OpenCTI",
  rows: { assets_exposure: 1186 },
  total: 1186,
  written_by_agent: "",
  sync_ids: ["s-1"],
  ...patch,
});

const plan = { source: "opencti", mode: "purge", count: 1186, counts: { assets_exposure: 1186 }, rows: {}, kept_elsewhere: { assets_exposure: ["web-01"] } };

function serve(syncs: OrganizationSync[], sources: OrganizationSource[]) {
  get.mockImplementation(async (path: string) => {
    if (path.startsWith("/organization-context/syncs/directions")) return [];
    if (path.includes("/history")) return [{ at: "2026-11-14T05:00:00Z", action: "run", by: null, summary: "", trigger: "schedule", added: 3, updated: 1, removed: 0, version: 9, error: null }];
    if (path.includes("rows-preview")) return path.includes("mode=release") ? { ...plan, mode: "release" } : plan;
    if (path.startsWith("/organization-context/syncs")) return syncs;
    if (path.startsWith("/organization-context/sources")) return sources;
    if (path.startsWith("/auth/me")) return { email: "admin@example.com" };
    return [];
  });
}

function render(scope: "" | "profile" | "inventory" = "") {
  renderWithProviders(
    <MemoryRouter>
      <OrganizationSourcesSyncs scope={scope} onAddSource={() => {}} />
    </MemoryRouter>,
  );
}

const rows = async () => {
  await waitFor(() => expect(screen.getAllByTestId("organization-sources.row").length).toBeGreaterThan(0));
  return screen.getAllByTestId("organization-sources.row");
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe("OrganizationSourcesSyncs (#1276)", () => {
  it("reads only its location's syncs and sources when given a scope", async () => {
    serve([sync()], [source()]);
    render("inventory");
    await rows();
    expect(get).toHaveBeenCalledWith("/organization-context/syncs?scope=inventory");
    expect(get).toHaveBeenCalledWith("/organization-context/sources?scope=inventory");
  });

  it("lists one row per sync and per source with no sync, kinds in words, never a raw slug or Typed", async () => {
    serve(
      [sync()],
      [
        source(),
        source({ key: "ciso-office", kind: "agent", display_name: "ciso-office", rows: { crown_jewels: 4 }, total: 4, written_by_agent: "CISO Office", sync_ids: [] }),
        source({ key: "manual", kind: "manual", display_name: "manual", rows: { crown_jewels: 2 }, total: 2, sync_ids: [] }),
      ],
    );
    render();
    const all = await rows();
    expect(all).toHaveLength(3);
    const text = all.map((row) => row.textContent ?? "").join(" | ");
    expect(text).toContain("Platform sync");
    expect(text).toContain("Agent or partner");
    expect(text).toContain("Written by agent CISO Office");
    expect(text).toContain("Entered by hand");
    expect(text).not.toContain("ciso-office");
    expect(text).not.toContain("Typed");
    expect(screen.getByTestId("organization-sources.summary").textContent).toContain("3 sources, 1 sync");
  });

  it("deletes a sync with the purge choice, counts from the dry run, other sources' rows named as kept", async () => {
    serve([sync()], [source()]);
    del.mockResolvedValue(undefined);
    render();
    const [row] = await rows();
    fireEvent.pointerDown(within(row).getByRole("button", { name: "Actions on OpenCTI" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete OpenCTI" }));
    const dialog = await screen.findByTestId("organization-sources.delete");
    // An ICU plural per section (UX r1 B6 on #1294).
    await waitFor(() => expect(dialog.textContent).toContain("It wrote 1,186 assets."));
    fireEvent.click(within(dialog).getByRole("radio", { name: /Purge the rows it wrote/ }));
    expect((await within(dialog).findByTestId("organization-sources.delete-kept")).textContent).toContain("web-01");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete and purge 1,186 rows" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("/organization-context/syncs/s-1?rows=purge"));
  });

  it("keeps the rows by default when a sync is deleted", async () => {
    serve([sync()], [source()]);
    del.mockResolvedValue(undefined);
    render();
    const [row] = await rows();
    fireEvent.pointerDown(within(row).getByRole("button", { name: "Actions on OpenCTI" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete OpenCTI" }));
    const dialog = await screen.findByTestId("organization-sources.delete");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete the sync" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("/organization-context/syncs/s-1?rows=leave"));
  });

  it("purges the rows of an agent source with no sync, and offers nothing on hand rows", async () => {
    serve(
      [],
      [
        source({ key: "ciso-office", kind: "agent", display_name: "ciso-office", rows: { crown_jewels: 4 }, total: 4, written_by_agent: "CISO Office", sync_ids: [] }),
        source({ key: "manual", kind: "manual", display_name: "manual", rows: { crown_jewels: 2 }, total: 2, sync_ids: [] }),
      ],
    );
    post.mockResolvedValue({ ...plan, source: "ciso-office", count: 4 });
    render();
    const all = await rows();
    const hand = all.find((row) => row.textContent?.includes("Entered by hand"));
    expect(hand && within(hand).queryByRole("button", { name: /Actions on/ })).toBeNull();
    const agent = all.find((row) => row.textContent?.includes("CISO Office"));
    if (!agent) throw new Error("agent row missing");
    fireEvent.pointerDown(within(agent).getByRole("button", { name: "Actions on CISO Office" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Remove or hand over the rows of CISO Office" }));
    const dialog = await screen.findByTestId("organization-sources.delete");
    fireEvent.click(within(dialog).getByRole("radio", { name: /Purge the rows it wrote/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: /^Purge/ }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/organization-context/sources/ciso-office/rows", { mode: "purge" }));
  });

  it("runs a sync now and reports its counts", async () => {
    serve([sync()], [source()]);
    post.mockResolvedValue(sync({ last_result: { added: 5, updated: 0, removed: 1, version: 10, error: null } }));
    render();
    const [row] = await rows();
    fireEvent.click(within(row).getByRole("button", { name: "Run OpenCTI now" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/organization-context/syncs/s-1/run", {}));
    expect(await screen.findByText("5 added, 0 updated, 1 removed")).toBeTruthy();
  });

  it("disables a sync from its menu", async () => {
    serve([sync()], [source()]);
    put.mockResolvedValue(sync({ enabled: false }));
    render();
    const [row] = await rows();
    fireEvent.pointerDown(within(row).getByRole("button", { name: "Actions on OpenCTI" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Disable" }));
    await waitFor(() => expect(put).toHaveBeenCalledWith("/organization-context/syncs/s-1", expect.objectContaining({ enabled: false })));
  });

  it("names a disabled sync with one word: state, next run and menu (UX r1 B5 on #1294)", async () => {
    serve([sync({ enabled: false })], [source()]);
    render();
    const [row] = await rows();
    expect(row.textContent).toContain("Disabled");
    expect(row.textContent).toContain("Disabled: enable it to run again");
    expect(row.textContent).not.toContain("Paused");
    fireEvent.pointerDown(within(row).getByRole("button", { name: "Actions on OpenCTI" }), { button: 0, ctrlKey: false });
    expect(await screen.findByRole("menuitem", { name: "Enable" })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: "Resume" })).toBeNull();
  });

  it("counts a platform with several syncs as one source, and gives each kind with no sync its own schedule words (UX r1 B3, B4 on #1294)", async () => {
    serve(
      [sync({ id: "s-1", source: "openaev" }), sync({ id: "s-2", source: "openaev", section: "tech_stack" })],
      [
        source({ key: "openaev", display_name: "OpenAEV", sync_ids: ["s-1", "s-2"] }),
        source({ key: "csv", kind: "csv", display_name: "csv", rows: { assets_exposure: 3 }, total: 3, sync_ids: [] }),
        source({ key: "api", kind: "api", display_name: "api", rows: { assets_exposure: 2 }, total: 2, sync_ids: [] }),
        source({ key: "manual", kind: "manual", display_name: "manual", rows: { crown_jewels: 1 }, total: 1, sync_ids: [] }),
      ],
    );
    render();
    const all = await rows();
    expect(screen.getByTestId("organization-sources.summary").textContent).toContain("4 sources, 2 syncs");
    const text = all.map((row) => row.textContent ?? "").join(" | ");
    expect(text).toContain("Writes at each CSV import");
    expect(text).toContain("Writes at each API call");
    expect(text).toContain("Changes when a person edits the profile");
  });

  it("labels each history entry in words and never shows the backend's sentence (UX r1 B7 on #1294)", async () => {
    serve([sync()], [source()]);
    const base = get.getMockImplementation();
    get.mockImplementation(async (path: string) =>
      path.includes("/history")
        ? [{ at: "2026-11-14T06:00:00Z", action: "create", by: "admin@example.com", summary: "sync created by admin", trigger: "", added: null, updated: null, removed: null, version: null, error: null }]
        : base?.(path),
    );
    render();
    const [row] = await rows();
    fireEvent.pointerDown(within(row).getByRole("button", { name: "Actions on OpenCTI" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "View history" }));
    const panel = await screen.findByTestId("organization-sources.history");
    await waitFor(() => expect(within(panel).getAllByTestId("organization-sources.history-entry")).toHaveLength(1));
    expect(panel.textContent).toContain("Created");
    expect(panel.textContent).not.toContain("create ");
    expect(panel.textContent).not.toContain("sync created by admin");
  });

  it("opens the history of a sync with its runs", async () => {
    serve([sync()], [source()]);
    render();
    const [row] = await rows();
    fireEvent.pointerDown(within(row).getByRole("button", { name: "Actions on OpenCTI" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "View history" }));
    const panel = await screen.findByTestId("organization-sources.history");
    await waitFor(() => expect(within(panel).getAllByTestId("organization-sources.history-entry")).toHaveLength(1));
    expect(panel.textContent).toContain("3 added, 1 updated, 0 removed");
  });

  it("shows the reason instead of a next run when a sync is paused, and a failed state on an error", async () => {
    serve(
      [sync({ paused_reason: "The token was revoked." }), sync({ id: "s-2", source: "openaev", last_result: { error: "Timeout" } })],
      [source(), source({ key: "openaev", display_name: "OpenAEV", sync_ids: ["s-2"] })],
    );
    render();
    const all = await rows();
    const text = all.map((row) => row.textContent ?? "").join(" | ");
    expect(text).toContain("Paused: The token was revoked.");
    expect(text).toContain("Failed");
  });

  it("shows a centred first-use state with Add a source when nothing feeds the sections", async () => {
    serve([], []);
    render();
    const empty = await screen.findByTestId("organization-sources.empty");
    expect(within(empty).getByRole("button", { name: /Add a source/ })).toBeTruthy();
  });
});
