// SPDX-License-Identifier: Apache-2.0

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { OrganizationSource, OrganizationSync } from "@/hooks/api/useOrganizationContextApi";
import { OrganizationSourcesSyncs, type SyncPreset } from "./OrganizationSourcesSyncs";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const auth = vi.hoisted(() => ({ isAdmin: true }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => auth }));

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

function render(scope: "" | "profile" | "inventory" = "", onSetUpSync?: (preset: SyncPreset) => void) {
  renderWithProviders(
    <MemoryRouter>
      <OrganizationSourcesSyncs scope={scope} onAddSource={() => {}} onSetUpSync={onSetUpSync} />
    </MemoryRouter>,
  );
}

const rows = async () => {
  await waitFor(() => expect(screen.getAllByTestId("organization-sources.row").length).toBeGreaterThan(0));
  return screen.getAllByTestId("organization-sources.row");
};

beforeEach(() => {
  vi.clearAllMocks();
  auth.isAdmin = true;
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
        source({ key: "ciso-office", kind: "agent", display_name: "ciso-office", rows: { primary_assets: 4 }, total: 4, written_by_agent: "CISO Office", sync_ids: [] }),
        source({ key: "manual", kind: "manual", display_name: "manual", rows: { primary_assets: 2 }, total: 2, sync_ids: [] }),
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
        source({ key: "ciso-office", kind: "agent", display_name: "ciso-office", rows: { primary_assets: 4 }, total: 4, written_by_agent: "CISO Office", sync_ids: [] }),
        source({ key: "manual", kind: "manual", display_name: "manual", rows: { primary_assets: 2 }, total: 2, sync_ids: [] }),
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
    fireEvent.click(await screen.findByRole("menuitem", { name: "Remove or hand over the rows" }));
    const dialog = await screen.findByTestId("organization-sources.delete");
    fireEvent.click(within(dialog).getByRole("radio", { name: /Purge the rows it wrote/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: /^Purge/ }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/organization-context/sources/ciso-office/rows", { mode: "purge" }));
  });

  it.each([
    ["opencrq", "OpenCRQ", { primary_assets: 9 }, "primary_assets"],
    ["opencti", "OpenCTI", { assets_exposure: 12 }, "assets"],
    ["openaev", "OpenAEV", { software: 4 }, "software"],
  ] as const)("gives a %s source whose rows outlived their sync a menu: set up a sync, purge or hand over (#1940)", async (key, name, counts, section) => {
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    serve([], [source({ key, display_name: name, rows: counts, total, sync_ids: [] })]);
    post.mockResolvedValue({ ...plan, source: key, count: total });
    const onSetUpSync = vi.fn();
    render("inventory", onSetUpSync);
    const [row] = await rows();
    expect(row.textContent).toContain("Rows from an earlier sync, none scheduled");
    fireEvent.pointerDown(within(row).getByRole("button", { name: `Actions on ${name}` }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Set up a sync" }));
    expect(onSetUpSync).toHaveBeenCalledWith({ section, source: key });
    fireEvent.pointerDown(within(row).getByRole("button", { name: `Actions on ${name}` }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Remove or hand over the rows" }));
    const dialog = await screen.findByTestId("organization-sources.delete");
    fireEvent.click(within(dialog).getByRole("button", { name: "Make the rows editable" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith(`/organization-context/sources/${key}/rows`, { mode: "release" }));
  });

  it("offers no action on a connected platform with no row and no sync (#1940)", async () => {
    serve([], [source({ key: "opencrq", display_name: "OpenCRQ", rows: {}, total: 0, sync_ids: [] })]);
    render("inventory", vi.fn());
    const [row] = await rows();
    expect(row.textContent).toContain("No sync scheduled");
    expect(within(row).queryByRole("button", { name: /Actions on/ })).toBeNull();
  });

  it("purges the rows of an API source with no sync (#1940: every source but hand rows)", async () => {
    serve([], [source({ key: "api", kind: "api", display_name: "api", rows: { assets_exposure: 2 }, total: 2, sync_ids: [] })]);
    post.mockResolvedValue({ ...plan, source: "api", count: 2 });
    render();
    const [row] = await rows();
    fireEvent.pointerDown(within(row).getByRole("button", { name: /Actions on/ }), { button: 0, ctrlKey: false });
    expect(screen.queryByRole("menuitem", { name: "Set up a sync" })).toBeNull();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Remove or hand over the rows" }));
    const dialog = await screen.findByTestId("organization-sources.delete");
    fireEvent.click(within(dialog).getByRole("radio", { name: /Purge the rows it wrote/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: /^Purge/ }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/organization-context/sources/api/rows", { mode: "purge" }));
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
      [sync({ id: "s-1", source: "openaev" }), sync({ id: "s-2", source: "openaev", section: "software" })],
      [
        source({ key: "openaev", display_name: "OpenAEV", sync_ids: ["s-1", "s-2"] }),
        source({ key: "csv", kind: "csv", display_name: "csv", rows: { assets_exposure: 3 }, total: 3, sync_ids: [] }),
        source({ key: "api", kind: "api", display_name: "api", rows: { assets_exposure: 2 }, total: 2, sync_ids: [] }),
        source({ key: "manual", kind: "manual", display_name: "manual", rows: { primary_assets: 1 }, total: 1, sync_ids: [] }),
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

  it("lands an Assets chip filtered on the source key and draws a section with no Source filter as plain text (UX round 5 on #1254, B11)", async () => {
    serve(
      [sync({ id: "s-1", source: "openaev", section: "software" })],
      [
        source({ key: "openaev", display_name: "OpenAEV", rows: { software: 4 }, total: 4, sync_ids: ["s-1"] }),
        source({ key: "csv", kind: "csv", display_name: "csv", rows: { assets_exposure: 12 }, total: 12, sync_ids: [] }),
      ],
    );
    render("inventory");
    const all = await rows();
    const csv = all.find((row) => row.textContent?.includes("CSV import"));
    const openaev = all.find((row) => row.textContent?.includes("OpenAEV"));
    if (!csv || !openaev) throw new Error("rows missing");
    const assets = within(csv).getByTestId("organization-sources.chip");
    expect(assets.tagName).toBe("BUTTON");
    expect(assets.getAttribute("aria-label")).toBe("Assets from CSV import: 12 rows");
    const tech = within(openaev).getByTestId("organization-sources.chip");
    expect(tech.tagName).not.toBe("BUTTON");
    expect(tech.closest("button")).toBeNull();
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

  it("names an agent or partner row by its source, its agent on the subline (lab retest of #1294, D2)", async () => {
    serve(
      [],
      [
        source({ key: "intune", kind: "agent", display_name: "Microsoft Intune", rows: { assets_exposure: 12 }, total: 12, written_by_agent: "CISO Office", sync_ids: [] }),
        source({ key: "ciso-notes", kind: "agent", display_name: "ciso-notes", rows: { primary_assets: 1 }, total: 1, written_by_agent: "CISO Office", sync_ids: [] }),
      ],
    );
    render();
    const all = await rows();
    const intune = all.find((row) => row.textContent?.includes("Microsoft Intune"));
    expect(intune?.textContent).toContain("Written by agent CISO Office");
    expect(within(intune!).getByRole("button", { name: "Actions on Microsoft Intune" })).toBeTruthy();
    // A source with only its raw key for a name falls back to the agent.
    expect(all.some((row) => row.textContent?.startsWith("CISO Office"))).toBe(true);
    expect(all.map((row) => row.textContent ?? "").join(" | ")).not.toContain("ciso-notes");
  });

  it("puts Filters behind Columns so the default columns fit the 1458 px card at 1728 (lab retest of #1294, D1)", async () => {
    serve([sync()], [source()]);
    render();
    await rows();
    const headers = screen.getAllByRole("columnheader").map((cell) => cell.textContent ?? "");
    // Filters steps behind Columns; the label summary is still searched and exported.
    expect(headers.some((text) => text.startsWith("Filters"))).toBe(false);
    expect(screen.getByRole("button", { name: /Columns/ })).toBeTruthy();
    expect(screen.getAllByTestId("organization-sources.row")[0].textContent).not.toContain("dmz");
  });

  it.each(["inventory", "profile"] as const)("runs every enabled sync of the %s scope with Run all syncs, no Sync all created (#1548)", async (scope) => {
    serve(
      [
        sync({ id: "s-1" }),
        sync({ id: "s-2", source: "openaev", section: "software" }),
        sync({ id: "s-3", source: "openaev", section: "asset_groups", enabled: false }),
        sync({ id: "s-all", section: "all", source: "", filters: {} }),
      ],
      [source(), source({ key: "openaev", display_name: "OpenAEV", sync_ids: ["s-2", "s-3"] })],
    );
    post.mockImplementation(async (path: string) => sync({ id: path.split("/")[3], last_run_at: "2026-11-23T14:00:00Z" }));
    render(scope);
    await rows();
    expect(get).toHaveBeenCalledWith(`/organization-context/syncs?scope=${scope}`);
    const header = screen.getByTestId("organization-sources");
    const runAll = within(header).getByRole("button", { name: /Run all syncs/ });
    // "Run all syncs" (secondary) before "Add a source" (primary), and no button creates a Sync all.
    const add = within(header).getByRole("button", { name: /Add a source/ });
    expect(runAll.compareDocumentPosition(add) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText(/Add a Sync all schedule|Add Sync all/)).toBeNull();
    fireEvent.click(runAll);
    expect(await screen.findByText("2 syncs started")).toBeTruthy();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(post).toHaveBeenNthCalledWith(1, "/organization-context/syncs/s-1/run", {});
    expect(post).toHaveBeenNthCalledWith(2, "/organization-context/syncs/s-2/run", {});
    expect(post).not.toHaveBeenCalledWith("/organization-context/syncs", expect.anything());
    // The list is read again after each run, so the Last run cells follow.
    await waitFor(() => expect(get.mock.calls.filter(([path]) => path === `/organization-context/syncs?scope=${scope}`).length).toBeGreaterThanOrEqual(3));
  });

  it("reports the syncs that failed after Run all syncs", async () => {
    serve([sync({ id: "s-1" }), sync({ id: "s-2", source: "openaev", section: "software" })], [source(), source({ key: "openaev", display_name: "OpenAEV", sync_ids: ["s-2"] })]);
    post.mockResolvedValueOnce(sync({ id: "s-1" })).mockRejectedValueOnce(new Error("Timeout"));
    render("inventory");
    await rows();
    fireEvent.click(screen.getByRole("button", { name: /Run all syncs/ }));
    expect(await screen.findByText("1 sync failed")).toBeTruthy();
  });

  it("shows no Run all syncs to a reader", async () => {
    auth.isAdmin = false;
    serve([sync()], [source()]);
    render("inventory");
    await rows();
    expect(screen.queryByRole("button", { name: /Run all syncs/ })).toBeNull();
  });

  it("keeps a saved Sync all row listed and editable, its dialog titled Edit the sync (#1548)", async () => {
    serve([sync({ id: "s-all", section: "all", source: "", filters: {} }), sync()], [source()]);
    render();
    const all = await rows();
    const syncAll = all.find((row) => row.textContent?.includes("Every section sync"));
    if (!syncAll) throw new Error("Sync all row missing");
    fireEvent.pointerDown(within(syncAll).getByRole("button", { name: /Actions on/ }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Edit" }));
    expect(await screen.findByRole("heading", { name: "Edit the sync" })).toBeTruthy();
  });

  it("shows a centred first-use state with Add a source when nothing feeds the sections", async () => {
    serve([], []);
    render();
    const empty = await screen.findByTestId("organization-sources.empty");
    expect(within(empty).getByRole("button", { name: /Add a source/ })).toBeTruthy();
  });
});
