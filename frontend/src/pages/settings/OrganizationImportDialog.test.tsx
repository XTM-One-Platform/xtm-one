// The import stepper of Settings > Organization (#11780, #11886): section, source, filters picked from the source,
// mapping with an example per field, preview with counts and the rows no longer in the source, then Import.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { OrganizationImportDialog } from "./OrganizationImportDialog";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

const SOURCES = {
  sections: [
    {
      section: "assets",
      field: "assets_exposure",
      sources: [
        { source: "openaev", available: false, reason: "not connected, or no token for this administrator", filters: ["asset_groups", "tags", "category", "subcategory", "platform"] },
        {
          source: "opencti",
          available: true,
          reason: "",
          filters: ["labels", "markings", "created_by", "creator", "category", "subcategory", "criticality", "internet_facing", "environment", "platform", "created_after", "filter_group"],
        },
        { source: "intune", available: false, reason: "no Microsoft Intune integration is connected", reason_code: "not_connected", filters: [] },
        { source: "defender", available: false, reason: "no token for the Microsoft Defender integration: authorize it again", reason_code: "no_token", filters: [] },
      ],
    },
    { section: "crown_jewels", field: "crown_jewels", sources: [{ source: "opencrq", available: true, reason: "", filters: ["min_criticality", "tenant_id"] }] },
  ],
};

const OPTIONS = {
  options: {
    labels: [{ value: "l1", label: "dmz", description: "", parent: "" }],
    category: [
      { value: "HOST", label: "HOST", description: "", parent: "" },
      { value: "NETWORK_DEVICE", label: "NETWORK_DEVICE", description: "", parent: "" },
    ],
    subcategory: [
      { value: "SERVER", label: "SERVER", description: "", parent: "HOST" },
      { value: "ROUTER", label: "ROUTER", description: "", parent: "NETWORK_DEVICE" },
    ],
  },
  unavailable: {},
};

const OLD_OPENCTI = {
  options: { labels: [] },
  unavailable: Object.fromEntries(["category", "subcategory", "criticality", "internet_facing", "environment", "platform"].map((key) => [key, "no_system_taxonomy"])),
};

type Counts = { new: number; updated: number; unchanged: number; skipped: number; gone?: number };

const plan = ({ gone = 0, ...counts }: Counts, total: number | null = 3) => ({
  based_on_version: 7,
  applied: false,
  version: null,
  plan: {
    section: "assets",
    source: "opencti",
    source_total: total,
    read: 3,
    counts: { ...counts, gone },
    new: [],
    updated: [],
    skipped: [],
    gone: Array.from({ length: gone }, (_, i) => `old-${i}`),
    remove_gone: false,
    batch_typed: 1,
    sample: [{ name: "payments-api", category: "host", subcategory: "server", criticality: "high", owner: "Infra team" }],
    truncated: false,
    error: null,
  },
});

function open() {
  renderWithProviders(<OrganizationImportDialog open onOpenChange={() => {}} onQuickSetup={() => {}} />);
}

/** A choice card of the stepper (the shared OptionCard, a radio). */
async function choice(key: string) {
  let card: HTMLElement | null = null;
  await waitFor(() => {
    card = document.getElementById(`org-import-choice-${key}`);
    expect(card).toBeTruthy();
  });
  return card as unknown as HTMLButtonElement;
}

async function toPreview() {
  fireEvent.click(await choice("assets"));
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  await choice("opencti");
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(await screen.findByTestId("org-import.mapping")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Preview" }));
}

const PREVIEW = "/organization-context/import/preview";

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(async (path: string) => {
    if (path === "/organization-context/import/sources") return SOURCES;
    if (path.startsWith("/organization-context/import/options")) return OPTIONS;
    if (path === "/auth/me") return { id: "u-1", email: "me.admin@example.com", full_name: "Me Admin" };
    throw new Error(`unexpected GET ${path}`);
  });
});

describe("OrganizationImportDialog (#11780)", () => {
  it("lists a platform that is not connected as disabled, with the reason, and picks the readable one", async () => {
    open();
    fireEvent.click(await choice("assets"));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    const openaev = await choice("openaev");
    expect(openaev.disabled).toBe(true);
    expect(openaev.textContent).toContain("not connected, or no token for this administrator");
    expect((await choice("opencti")).getAttribute("aria-checked")).toBe("true");
  });

  it("words the reason of an endpoint source from its code, never the backend's English sentence (#1251)", async () => {
    open();
    fireEvent.click(await choice("assets"));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    const intune = await choice("intune");
    expect(intune.disabled).toBe(true);
    expect(intune.textContent).toContain("Not available: no Microsoft Intune integration is connected. Connect it in Settings > Integrations.");
    const defender = await choice("defender");
    expect(defender.disabled).toBe(true);
    expect(defender.textContent).toContain("the Microsoft Defender for Endpoint integration has no token. Authorize it again");
    expect(defender.textContent).not.toContain("authorize it again");
  });

  it("moves the section with the arrow keys, as a radio group", async () => {
    open();
    fireEvent.click(await choice("assets"));
    fireEvent.keyDown(await choice("assets"), { key: "ArrowDown" });
    expect((await choice("asset_groups")).getAttribute("aria-checked")).toBe("true");
    expect(document.activeElement?.id).toBe("org-import-choice-asset_groups");
  });

  it("offers every OpenCTI filter as a picker fed from the source, and the batch type as a fallback only", async () => {
    open();
    fireEvent.click(await choice("assets"));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await choice("opencti");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Type when OpenCTI gives none")).toBeTruthy();
    for (const label of ["Labels", "Markings", "Author", "Creator", "Category", "Subcategory", "Criticality", "Exposure", "Environment", "Platform or OS", "Created on or after"]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    await waitFor(() => expect(get).toHaveBeenCalledWith("/organization-context/import/options?section=assets&source=opencti"));
    expect(screen.queryByTestId("org-import.no-taxonomy")).toBeNull();
  });

  it("hides the taxonomy filters of an OpenCTI before #19943 and says why", async () => {
    get.mockImplementation(async (path: string) => {
      if (path === "/organization-context/import/sources") return SOURCES;
      if (path.startsWith("/organization-context/import/options")) return OLD_OPENCTI;
      throw new Error(`unexpected GET ${path}`);
    });
    open();
    fireEvent.click(await choice("assets"));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await choice("opencti");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByTestId("org-import.no-taxonomy")).toBeTruthy();
    expect(screen.queryByText("Subcategory")).toBeNull();
    expect(screen.getByText("Labels")).toBeTruthy();
  });

  it("maps each profile field with its OpenCTI attribute, its fallback and an example from the source", async () => {
    post.mockResolvedValue(plan({ new: 3, updated: 0, unchanged: 0, skipped: 0 }));
    open();
    fireEvent.click(await choice("assets"));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await choice("opencti");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    const mapping = await screen.findByTestId("org-import.mapping");
    expect(mapping.textContent).toContain("x_opencti_system_category + x_opencti_system_subcategory");
    expect(mapping.textContent).toContain("x_opencti_criticality");
    await waitFor(() => expect(mapping.textContent).toContain("Infra team"));
    expect(mapping.textContent).toContain("used for 1 row without a category");
    expect(screen.getByTestId("org-import.not-provided").textContent).toContain("Group");
    // The preview is read once, when the mapping opens; "Preview" shows it.
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByTestId("org-import.source-total");
    expect(post.mock.calls.filter(([path]) => path === PREVIEW)).toHaveLength(1);
  });

  it("reports the rows no longer in the source, keeps them by default and removes them on demand", async () => {
    post.mockImplementation(async (path: string, body?: unknown) => {
      const removes = Boolean((body as { remove_gone?: boolean } | undefined)?.remove_gone);
      if (path === PREVIEW) return plan({ new: 0, updated: 0, unchanged: 1, skipped: 0, gone: 2 });
      return { ...plan({ new: 0, updated: 0, unchanged: 1, skipped: 0, gone: 2 }), applied: removes, version: 8 };
    });
    open();
    await toPreview();
    expect(await screen.findByText("2 rows are no longer in OpenCTI")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Nothing to import" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(document.getElementById("org-import-remove-gone") as HTMLElement);
    fireEvent.click(await screen.findByRole("button", { name: "Import 2 rows" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/organization-context/import/apply", expect.objectContaining({ remove_gone: true, based_on_version: 7 })),
    );
  });

  it("shows the source's own count and the counts per outcome before anything is written, then imports", async () => {
    post.mockImplementation(async (path: string) => {
      if (path === "/organization-context/import/preview") return plan({ new: 2, updated: 0, unchanged: 0, skipped: 1 });
      if (path === "/organization-context/import/apply") return { ...plan({ new: 2, updated: 0, unchanged: 0, skipped: 1 }), applied: true, version: 8 };
      throw new Error(`unexpected POST ${path}`);
    });
    open();
    await toPreview();
    expect((await screen.findByTestId("org-import.source-total")).textContent).toBe("3 entities match in OpenCTI");
    expect(screen.getByTestId("org-import.counts").textContent).toContain("Skipped1");
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith(PREVIEW, { section: "assets", source: "opencti", filters: {}, batch_type: "generic_asset" });
    fireEvent.click(screen.getByRole("button", { name: "Import 2 rows" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/organization-context/import/apply", {
        section: "assets",
        source: "opencti",
        filters: {},
        batch_type: "generic_asset",
        based_on_version: 7,
      }),
    );
  });

  it("says 1 entity in the singular and disables Import when nothing is new or updated", async () => {
    post.mockResolvedValue(plan({ new: 0, updated: 0, unchanged: 1, skipped: 0 }, 1));
    open();
    await toPreview();
    expect((await screen.findByTestId("org-import.source-total")).textContent).toBe("1 entity matches in OpenCTI");
    expect((screen.getByRole("button", { name: "Nothing to import" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("draws a large count compact, the exact figure on hover", async () => {
    post.mockResolvedValue(plan({ new: 1284, updated: 0, unchanged: 0, skipped: 0 }, 1284));
    open();
    await toPreview();
    const total = await screen.findByTestId("org-import.source-total");
    expect(total.textContent).not.toContain("1,284");
    expect(total.querySelector("[title]")?.getAttribute("title")).toBe("1,284 entities match in OpenCTI");
    expect(screen.queryByRole("button", { name: "Import 1,284 rows" })).toBeNull();
  });

  it("clears a failed import's error when a new preview is made", async () => {
    post.mockImplementation(async (path: string) => {
      if (path === "/organization-context/import/preview") return plan({ new: 2, updated: 0, unchanged: 0, skipped: 0 });
      throw new Error("409");
    });
    open();
    await toPreview();
    fireEvent.click(await screen.findByRole("button", { name: "Import 2 rows" }));
    expect(await screen.findByText("The import was not saved. Preview again, then import.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByTestId("org-import.source-total");
    expect(screen.queryByText("The import was not saved. Preview again, then import.")).toBeNull();
  });

  it("imports and keeps in sync: the same source, filters and removal policy saved with the schedule picked (#11913)", async () => {
    post.mockImplementation(async (path: string) => {
      if (path === PREVIEW) return plan({ new: 1, updated: 0, unchanged: 0, skipped: 0 });
      if (path === "/organization-context/import/apply") return { ...plan({ new: 1, updated: 0, unchanged: 0, skipped: 0 }), applied: true, version: 8 };
      if (path === "/organization-context/syncs") return { id: "s-1" };
      throw new Error(`unexpected POST ${path}`);
    });
    open();
    await toPreview();
    expect(await screen.findByRole("button", { name: "Import 1 row" })).toBeTruthy();
    fireEvent.click(screen.getByRole("switch", { name: "Keep in sync" }));
    expect(screen.getByTestId("org-import-sync.schedule")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Import 1 row and keep in sync" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/organization-context/syncs",
        expect.objectContaining({ section: "assets", source: "opencti", filters: {}, batch_type: "generic_asset", remove_gone: false, frequency: "daily", hour: 6, minute: 0, enabled: true }),
      ),
    );
    expect(post).toHaveBeenCalledWith("/organization-context/import/apply", expect.objectContaining({ based_on_version: 7 }));
  });

  it("saves the sync alone when nothing is new today, and never imports twice when only the sync failed", async () => {
    let syncCalls = 0;
    post.mockImplementation(async (path: string) => {
      if (path === PREVIEW) return plan({ new: 0, updated: 0, unchanged: 1, skipped: 0 });
      if (path === "/organization-context/syncs") {
        syncCalls += 1;
        if (syncCalls === 1) throw new Error("500");
        return { id: "s-1" };
      }
      throw new Error(`unexpected POST ${path}`);
    });
    open();
    await toPreview();
    fireEvent.click(await screen.findByRole("switch", { name: "Keep in sync" }));
    fireEvent.click(screen.getByRole("button", { name: "Save the sync" }));
    expect(await screen.findByTestId("org-import.sync-error")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save the sync" }));
    await waitFor(() => expect(syncCalls).toBe(2));
    expect(post).not.toHaveBeenCalledWith("/organization-context/import/apply", expect.anything());
  });

  it("warns on the source step that the context is shared and read with the importer's permissions (#11994)", async () => {
    open();
    fireEvent.click(await choice("assets"));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await choice("opencti");
    const notice = screen.getByTestId("org-shared-context.source");
    expect(notice.textContent).toContain("Shared organization context");
    expect(notice.textContent).toContain("with the permissions of the person who sets up this import or sync");
    expect(notice.textContent).toContain("organization segregation and markings, are not carried over");
    expect(notice.textContent).toContain("visible to every user and every agent of this XTM One platform");
    expect(notice.textContent).toContain("This applies to the organization context only.");
  });

  it("names who the import reads as next to Import, and who the sync runs as once Keep in sync is on (#11994)", async () => {
    post.mockResolvedValue(plan({ new: 1, updated: 0, unchanged: 0, skipped: 0 }));
    open();
    await toPreview();
    await screen.findByRole("button", { name: "Import 1 row" });
    await waitFor(() => expect(screen.getByTestId("org-shared-context.actor").textContent).toBe("Reads as me.admin@example.com"));
    fireEvent.click(screen.getByRole("switch", { name: "Keep in sync" }));
    expect(screen.getByTestId("org-shared-context.actor").textContent).toBe("Runs as me.admin@example.com");
  });

  it("reports a platform that fails, never its text, and saves nothing", async () => {
    post.mockResolvedValue({ ...plan({ new: 0, updated: 0, unchanged: 0, skipped: 0 }), plan: { ...plan({ new: 0, updated: 0, unchanged: 0, skipped: 0 }).plan, error: "OpenCTI: upstream 502 tenant 42" } });
    open();
    await toPreview();
    expect(await screen.findByText("OpenCTI could not be read. Nothing will be saved.")).toBeTruthy();
    expect(screen.queryByText(/upstream 502/)).toBeNull();
    expect(screen.queryByText(/^Reason:/)).toBeNull();
  });

  it("says why a source refused the read and which permission it needs (#1556, UX round 1 B1)", async () => {
    const reason = "CrowdStrike Falcon refused the group read (HTTP 401): it needs an API client with the Host groups: Read and Hosts: Read scopes.";
    const failed = plan({ new: 0, updated: 0, unchanged: 0, skipped: 0 });
    post.mockResolvedValue({ ...failed, plan: { ...failed.plan, error: reason, error_reason: reason } });
    open();
    await toPreview();
    const block = await screen.findByTestId("org-import.read-error");
    expect(block.textContent).toContain("could not be read. Nothing will be saved.");
    expect(block.textContent).toContain(`Reason: ${reason}`);
  });
});
