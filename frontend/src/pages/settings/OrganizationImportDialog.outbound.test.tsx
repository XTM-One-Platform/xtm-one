// The "Push to a platform" direction of the sync stepper (#1982 S4): direction, section, target platform, filters on
// the XTM One rows, the first run's plan from /syncs/outbound-preview, then a sync saved with direction outbound.

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
        { source: "openaev", available: true, reason: "", filters: ["tags", "category"] },
        { source: "opencti", available: true, reason: "", filters: ["labels", "category"] },
        { source: "intune", available: true, reason: "", filters: [] },
      ],
    },
    { section: "primary_assets", field: "primary_assets", sources: [{ source: "opencrq", available: true, reason: "", filters: [] }] },
  ],
};

const DIRECTIONS = [
  { section: "assets", source: "openaev", directions: ["inbound", "outbound"], reason: "" },
  { section: "assets", source: "opencti", directions: ["inbound", "outbound"], reason: "" },
  { section: "primary_assets", source: "opencrq", directions: ["inbound"], reason: "OpenCRQ owns its primary assets." },
];

const PLAN = { direction: "outbound", counts: { enter: 2, update: 0, leave: 0, kept: 0 }, enter: ["payments-api", "billing-db"], update: [], leave: [], kept: [], error: null };

async function choice(key: string) {
  let card: HTMLElement | null = null;
  await waitFor(() => {
    card = document.getElementById(`org-import-choice-${key}`);
    expect(card).toBeTruthy();
  });
  return card as unknown as HTMLButtonElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(async (path: string) => {
    if (path === "/organization-context/import/sources") return SOURCES;
    if (path === "/organization-context/syncs/directions") return DIRECTIONS;
    if (path === "/auth/me") return { id: "u-1", email: "me.admin@example.com", full_name: "Me Admin" };
    throw new Error(`unexpected GET ${path}`);
  });
  post.mockImplementation(async (path: string) => {
    if (path === "/organization-context/syncs/outbound-preview") return PLAN;
    if (path === "/organization-context/syncs") return { id: "s-1" };
    throw new Error(`unexpected POST ${path}`);
  });
});

describe("OrganizationImportDialog, Push to a platform (#1982 S4)", () => {
  it("offers only the sections and platforms an outbound sync takes", async () => {
    renderWithProviders(<OrganizationImportDialog open onOpenChange={() => {}} />);
    fireEvent.click(await choice("outbound"));
    expect(screen.getByRole("heading", { name: "Push to a platform" })).toBeTruthy();
    await choice("assets");
    expect(document.getElementById("org-import-choice-primary_assets")).toBeNull();
    fireEvent.click(await choice("assets"));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await choice("opencti");
    expect(document.getElementById("org-import-choice-intune")).toBeNull();
    expect(screen.getByTestId("org-import.step").textContent).toBe("Step 2 of 4: Target platform");
  });

  it("filters on the profile's own values, previews the first run, then saves an outbound sync", async () => {
    renderWithProviders(<OrganizationImportDialog open onOpenChange={() => {}} />);
    fireEvent.click(await choice("outbound"));
    fireEvent.click(await choice("assets"));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(await choice("opencti"));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    // No platform read for the filter values: the import options endpoint is never called.
    expect(get.mock.calls.some(([path]) => String(path).startsWith("/organization-context/import/options"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/organization-context/syncs/outbound-preview", { section: "assets", source: "opencti", filters: {} }));
    expect((await screen.findByTestId("org-import.outbound-counts")).textContent).toContain("To create2");
    expect(screen.getByTestId("org-import.sample").textContent).toContain("payments-api");
    fireEvent.click(screen.getByRole("button", { name: "Save the sync" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/organization-context/syncs", expect.objectContaining({ section: "assets", source: "opencti", direction: "outbound", remove_gone: false })),
    );
  });

  it("keeps the import alone when no section of the surface can be pushed", async () => {
    renderWithProviders(<OrganizationImportDialog open onOpenChange={() => {}} sections={["primary_assets"]} />);
    await choice("primary_assets");
    expect(screen.queryByTestId("org-import.directions")).toBeNull();
  });
});
