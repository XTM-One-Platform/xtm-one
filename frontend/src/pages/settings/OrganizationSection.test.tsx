// Settings > Organization (#8987): the profile every agent reads, its missing fields, the save and the OpenCRQ check.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders, testQueryClient } from "@/testing/providers";
import type { OrganizationContextResponse } from "@/hooks/api/useOrganizationContextApi";
import { JEWEL_WIDTHS, OrganizationSection } from "./OrganizationSection";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useAgenticAIEnabled: () => agenticEnabled,
  useIsXtmOne: () => xtmOneMode,
}));

// The tab bar registers palette commands (DetailTabs); the palette is not under test here.
vi.mock("@/lib/commands/CommandsProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/commands/CommandsProvider")>()),
  useCommandSections: () => {},
}));

let agenticEnabled = true;
/** Writes the router's current path, so a test reads where a link went. */
function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location-probe">{location.pathname + location.search}</span>;
}
// XTM One mode by default: Copilot mode keeps the Identity card alone (#1690), its own tests set it false.
let xtmOneMode = true;
beforeEach(() => {
  xtmOneMode = true;
});

/** Opens the header "Draft" menu and picks one of its rows (section 6.1). */
async function pickDraft(row: "From the platforms" | "With AI") {
  fireEvent.pointerDown(screen.getByRole("button", { name: "Draft" }), { button: 0, ctrlKey: false });
  fireEvent.click(await screen.findByRole("menuitem", { name: row }));
}
/** Draft > From the platforms opens the import stepper (#11780); its "Quick setup" runs the one-click draft. */
async function draftFromPlatforms() {
  await pickDraft("From the platforms");
  fireEvent.click(await screen.findByRole("button", { name: "Quick setup" }));
}
/** The cards CTEM > Inventory mounts in place of the three tabs that left Settings (#1178, #1690). */
const INVENTORY_CARDS = {
  business: ["primary_assets", "shipped_products", "tracked_organizations"],
  technology: ["software"],
  exposure: ["prioritization_policy", "outside_lens", "evidence_health"],
} as const;
/**
 * Renders the page on one of its tabs (#12329); none is the first tab. The Technology and Exposure cards render
 * as CTEM > Inventory mounts them (#1178), since Settings no longer draws them.
 */
function renderPage(tab?: string) {
  const embedded = tab === "business" || tab === "technology" || tab === "exposure" ? INVENTORY_CARDS[tab] : null;
  return renderWithProviders(
    <MemoryRouter initialEntries={[embedded ? "/ctem/inventory" : tab ? `/settings/organization/profile/${tab}` : "/settings/organization"]}>
      <OrganizationSection embed={embedded ? { cards: embedded } : undefined} />
      <LocationProbe />
    </MemoryRouter>,
  );
}

const get = vi.mocked(api.get);
const put = vi.mocked(api.put);
const patch = vi.mocked(api.patch);
const post = vi.mocked(api.post);
const postForm = vi.mocked(api.postForm);

const CONTEXT: OrganizationContextResponse = {
  version: 3,
  source: "form",
  created_at: "2026-11-05T10:00:00Z",
  author_user_id: null,
  profile: {
    name: "Acme",
    sector: "Finance",
    sub_sector: "",
    regions: ["FR"],
    size: "",
    primary_assets: [{ name: "Payments", business_service: "Cards", owner: "CISO", criticality: "critical", opencrq_asset_id: "" }],
    software: [],
    pir_ids: [],
    risk_appetite: { annual_loss_threshold: null, approval_threshold: null },
    loaded_hourly_rate: null,
    currency: "EUR",
    ticketing: { integration: "", project: "" },
  },
  missing_fields: ["size", "pir_ids", "loaded_hourly_rate"],
};

beforeEach(() => {
  vi.clearAllMocks();
  agenticEnabled = true;
  get.mockImplementation(async (path: string) => {
    if (path === "/organization-context") return CONTEXT;
    if (path === "/organizations") return [];
    if (path === "/integrations") return [];
    if (path === "/organization-context/pirs") return { pirs: [{ id: "pir-1", name: "Ransomware on energy" }], unavailable: null };
    if (path === "/organization-context/import/sources") return { sections: [] };
    if (path === "/organization-context/attack-surface/providers") return { providers: ["shodan"] };
    // The Sources and syncs table of the Knowledge and sources tab (#1276).
    if (path.startsWith("/organization-context/syncs") || path.startsWith("/organization-context/sources")) return [];
    throw new Error(`unexpected GET ${path}`);
  });
});

describe("OrganizationSection primary asset widths at 1080 (#1174 UX r2 B1 and B2)", () => {
  // The primary assets table measures 810 px in its 72rem card at 1080; Assets and Source step aside there.
  const TABLE_AT_1080 = 810;
  it("leaves Business service and Owner at least 120 px each in edit mode", () => {
    const fixed = JEWEL_WIDTHS.select + JEWEL_WIDTHS.name + JEWEL_WIDTHS.criticality + JEWEL_WIDTHS.actions;
    expect((TABLE_AT_1080 - fixed) / 2).toBeGreaterThanOrEqual(120);
  });
  it("leaves Business service room for its header in read mode and keeps OpenCRQ's header whole", () => {
    const fixed = JEWEL_WIDTHS.select + JEWEL_WIDTHS.name + JEWEL_WIDTHS.criticality + JEWEL_WIDTHS.opencrq;
    // "Business service" is 94 px of text, plus 24 px of padding and the sort mark.
    expect((TABLE_AT_1080 - fixed) / 2).toBeGreaterThanOrEqual(94 + 24 + 16);
    // "OpenCRQ" is 56 px of text, plus the padding and the sort mark.
    expect(JEWEL_WIDTHS.opencrq).toBeGreaterThanOrEqual(56 + 24 + 16);
  });
});

describe("OrganizationSection without reader rows (#1878)", () => {
  it("shows no Used by or Read by agents row on any card, in read mode or in edit, and never asks who reads the profile", async () => {
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    expect((await screen.findByTestId("settings.organization.identity")).textContent).toContain("Acme");
    expect(screen.queryByText("Used by")).toBeNull();
    expect(screen.queryByText("Read by agents")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    expect(await screen.findByTestId("settings.save-bar")).toBeTruthy();
    expect(screen.queryByText("Used by")).toBeNull();
    expect(get.mock.calls.map(([path]) => path)).not.toContain("/organization-context/readers");
  });
});

describe("OrganizationSection identity header and threshold previews (#9173 S2)", () => {
  const withProfile = (profile: Partial<OrganizationContextResponse["profile"]>) =>
    get.mockImplementation(async (path: string) => {
      if (path === "/organization-context") return { ...CONTEXT, profile: { ...CONTEXT.profile, ...profile } };
      if (path === "/organizations" || path === "/integrations") return [];
      if (path === "/organization-context/pirs") return { pirs: [], unavailable: null };
      throw new Error(`unexpected GET ${path}`);
    });

  it("reads the identity as tiles at rest, never as disabled fields, and turns into the form on Edit (#10855)", async () => {
    withProfile({ sub_sector: "Retail banking", size: "251-1000", regions: ["FR", "US"] });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    const identity = await screen.findByTestId("settings.organization.identity");
    expect(identity.textContent).toContain("Acme");
    expect(identity.textContent).toContain("Retail banking");
    expect(identity.textContent).toContain("251-1000 employees");
    const regions = within(identity).getByRole("list", { name: "Regions of operation" });
    expect(within(regions).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["France", "United States"]);
    // Real flags: an SVG named after the country, never a regional-indicator emoji.
    expect(within(regions).getByRole("img", { name: "France" }).tagName.toLowerCase()).toBe("svg");
    expect(identity.textContent).not.toMatch(/\p{Regional_Indicator}/u);
    // Nothing disabled at rest on the page.
    expect(document.querySelectorAll("input:disabled, textarea:disabled, select:disabled, button[role=combobox]:disabled")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    await waitFor(() => expect(screen.queryByTestId("settings.organization.identity")).toBeNull());
    expect(screen.getByLabelText("Organization name")).not.toBeDisabled();
  });

  it("reads an empty part as Not set, never leaves it blank", async () => {
    withProfile({ name: "", sector: "", size: "", regions: [] });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    const identity = await screen.findByTestId("settings.organization.identity");
    expect(within(identity).getAllByText("Not set").length).toBeGreaterThanOrEqual(4);
  });

  it("shows eight regions, then +N more in place", async () => {
    withProfile({ regions: ["FR", "US", "AU", "SA", "GB", "SG", "JP", "DE", "ES", "IT"] });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    const identity = await screen.findByTestId("settings.organization.identity");
    const regions = within(identity).getByRole("list", { name: "Regions of operation" });
    expect(within(regions).getAllByRole("img")).toHaveLength(8);
    fireEvent.click(within(identity).getByRole("button", { name: "+2 more" }));
    expect(within(regions).getAllByRole("img")).toHaveLength(10);
    expect(within(identity).getByRole("button", { name: "Show fewer" })).toHaveAttribute("aria-expanded", "true");
  });

  it("reads an amount with its unit and currency as text", async () => {
    withProfile({ currency: "", risk_appetite: { annual_loss_threshold: 250000, approval_threshold: null } });
    renderPage("risk");
    const tile = await screen.findByTestId("settings.organization.read.annual_loss_threshold");
    expect(tile.textContent).toContain("250,000 per year, currency not set");
    expect(screen.getByTestId("settings.organization.read.approval_threshold").textContent).toContain("Not set");
  });

  it("says what each saved threshold does, in the profile currency, in read mode only", async () => {
    withProfile({ risk_appetite: { annual_loss_threshold: 250000, approval_threshold: 1000000 } });
    renderPage("risk");
    expect(await screen.findByText(/Scenarios above .*250,000.* per year are ranked first/)).toBeTruthy();
    expect(screen.getByText(/Scenarios above .*1,000,000.* per year are flagged for approval/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    await waitFor(() => expect(screen.queryByText(/per year are ranked first/)).toBeNull());
  });
});

describe("OrganizationSection panels (#12329, #1690)", () => {
  it("draws two tabs in Settings, Organization then Risk and response; the Business cards live on CTEM > Inventory (#1975, #1690)", async () => {
    renderPage();
    const panel = await screen.findByTestId("settings.organization.panel-organization");
    // Each tab's name leads with its label; its missing count follows (UX r1 B1 on #1984).
    expect(screen.getAllByRole("tab").map((tab) => tab.getAttribute("data-testid"))).toEqual(["settings.organization.tab-organization", "settings.organization.tab-risk"]);
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent?.split(/\d/)[0])).toEqual(["Organization", "Risk and response"]);
    expect(screen.getByTestId("settings.organization.tabs")).toBeTruthy();
    // Organization: the identity, the enterprise knowledge and the agents' context (#1975: no Knowledge and sources page).
    expect(within(panel).getByTestId("settings.organization.identity")).toBeTruthy();
    expect(within(panel).getByTestId("settings.organization.enterprise-knowledge")).toBeTruthy();
    expect(within(panel).getByTestId("settings.organization.injection")).toBeTruthy();
    expect(within(panel).queryByTestId(`settings.organization.risk-appetite`)).toBeNull();
    expect(screen.queryByTestId("settings.organization.knowledge")).toBeNull();
    expect(screen.queryByRole("button", { name: /Back to Organization Profile/ })).toBeNull();
    expect(screen.queryByTestId("settings.organization.primary-assets")).toBeNull();
    expect(screen.queryByTestId("settings.organizations")).toBeNull();
  });

  it("draws the risk appetite, the case sync, the rates and the PIR details on Risk and response (#1975)", async () => {
    renderPage("risk");
    const panel = await screen.findByTestId("settings.organization.panel-risk");
    expect(screen.getByRole("tab", { name: /^Risk and response/ }).getAttribute("aria-selected")).toBe("true");
    expect(within(panel).getByRole("heading", { name: /Risk appetite/ })).toBeTruthy();
    expect(screen.queryByTestId("settings.organization.panel-organization")).toBeNull();
    expect(screen.queryByTestId("settings.organization.enterprise-knowledge")).toBeNull();
    // The profile's header, Draft and Edit stay on both tabs.
    expect(screen.getByTestId("settings.organization.edit")).toBeTruthy();
  });

  it("mounts only the cards an embedding page names, with no header, tab bar or Sources panel (#1178)", async () => {
    renderPage("exposure");
    expect(await screen.findByTestId("settings.organization.panel-exposure")).toBeTruthy();
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
    // The asset and group tables live on the Inventory row APIs, never in the profile cards (#1752 slice 3b).
    expect(screen.queryByTestId("settings.organization.assets-exposure")).toBeNull();
    expect(screen.queryByTestId("settings.organization.tech-stack")).toBeNull();
    expect(screen.queryByTestId("settings.organization.primary-assets")).toBeNull();
  });

  it("mounts the three Business cards on CTEM > Inventory, never folding (#1690)", async () => {
    renderPage("business");
    expect(await screen.findByTestId("settings.organization.primary-assets")).toBeTruthy();
    expect(screen.getByTestId("settings.organization.shipped-products")).toBeTruthy();
    expect(screen.getByTestId("settings.organizations")).toBeTruthy();
    expect(screen.queryByTestId("settings.organization.identity")).toBeNull();
    expect(screen.queryAllByRole("button", { name: /^(Collapse|Expand)/ })).toHaveLength(0);
  });

  it("reads the Business cards with no write action for a reader (#1690)", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/ctem/inventory/business"]}>
        <OrganizationSection embed={{ cards: INVENTORY_CARDS.business, readOnly: true }} />
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("settings.organization.primary-assets")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Edit/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Check against OpenCRQ/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Restore from a past version/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Add an organization/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Draft with AI/ })).toBeNull();
  });

  it("reads a former Business, Technology or Exposure tab path as an unknown tab, with no redirect (#797)", async () => {
    for (const path of ["/settings/organization/profile/business", "/settings/organization/profile/technology", "/settings/organization/profile/exposure?card=software"]) {
      const view = renderWithProviders(
        <MemoryRouter initialEntries={[path]}>
          <OrganizationSection />
        </MemoryRouter>,
      );
      expect(await screen.findByTestId("settings.organization.identity")).toBeTruthy();
      expect(screen.queryByTestId("settings.organization.primary-assets")).toBeNull();
      expect(screen.queryByTestId("settings.organization.tech-stack")).toBeNull();
      view.unmount();
    }
  });

  it("keeps Identity alone and no fields banner in Copilot mode (#1690)", async () => {
    xtmOneMode = false;
    renderPage();
    expect(await screen.findByTestId("settings.organization.identity")).toBeTruthy();
    expect(screen.queryByText("Risk appetite and costs")).toBeNull();
    expect(screen.queryByText("Ticketing and intelligence")).toBeNull();
    expect(screen.queryByTestId("settings.organization.missing")).toBeNull();
    expect(screen.queryByTestId("settings.organization.overview")).toBeNull();
  });

  it("lists only the missing fields Settings draws, and a refused Save focuses the bad field", async () => {
    renderPage("risk");
    const missing = await screen.findByTestId("settings.organization.missing");
    expect(missing.textContent).toContain("Loaded hourly rate");
    expect(missing.textContent).not.toContain("Primary assets");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Loaded hourly rate" }), { target: { value: "abc" } });
    fireEvent.click(within(await screen.findByTestId("settings.save-bar")).getByRole("button", { name: "Save" }));
    const rate = await screen.findByRole("textbox", { name: "Loaded hourly rate" });
    await waitFor(() => expect(document.activeElement).toBe(rate));
    expect(put).not.toHaveBeenCalled();
  });
});

describe("OrganizationSection", () => {
  it("names the fields not filled, as the agents report them, each on its tab (#1975)", async () => {
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    expect((await screen.findByTestId("settings.organization.identity")).textContent).toContain("Acme");
    // size is an Identity field: its chip is on the Organization tab, the loaded hourly rate's on Risk and response.
    expect((await screen.findByTestId("settings.organization.missing")).textContent).not.toContain("Loaded hourly rate");
    cleanup();
    renderPage("risk");
    const missing = await screen.findByTestId("settings.organization.missing");
    // The PIR list is reported on CTEM > Inventory with the Prioritization policy (#1889), never on Settings.
    expect(missing.textContent).not.toContain("Priority intelligence requirements (PIRs)");
    expect(missing.textContent).toContain("Loaded hourly rate");
  });

  it("marks each tab with its own missing count and its own unsaved edits (UX r1 B1 on #1984)", async () => {
    renderPage("risk");
    await screen.findByTestId("settings.organization.overview");
    // size on Organization, loaded_hourly_rate on Risk and response: one each, on its own tab.
    expect(screen.getByTestId("settings.organization.tab-organization-missing").textContent).toContain("1 field not filled");
    expect(screen.getByTestId("settings.organization.tab-risk-missing").textContent).toContain("1 field not filled");
    fireEvent.click(screen.getByTestId("settings.organization.edit"));
    fireEvent.change(await screen.findByRole("textbox", { name: "Loaded hourly rate" }), { target: { value: "120" } });
    // Only the tab whose field changed reads Unsaved; the count markers leave while the form is open.
    await waitFor(() => expect(screen.getByTestId("settings.organization.tab-risk-unsaved")).toBeTruthy());
    expect(screen.queryByTestId("settings.organization.tab-organization-unsaved")).toBeNull();
    expect(screen.queryByTestId("settings.organization.tab-risk-missing")).toBeNull();
  });

  it("shows the primary assets on CTEM > Inventory with no Used by row above them (#1690, #1878)", async () => {
    renderPage("business");
    expect(await screen.findByText("Payments")).toBeTruthy();
    expect(screen.queryByText("Used by")).toBeNull();
  });

  it("counts the fields not filled Settings draws and opens the form on a missing field (#9173 S1c, #1690)", async () => {
    renderPage("risk");
    const overview = await screen.findByTestId("settings.organization.overview");
    // loaded_hourly_rate here, size on Organization; pir_ids is reported on CTEM > Inventory with the Prioritization
    // policy (#1889). The row counts this tab and names the other one (UX r1 B1 on #1984).
    // The first sentence ends before the other tab's count (UX r2 B1 on #1984).
    expect(overview.textContent).toContain("1 field not filled on this tab, reported by every agent that needs it. 1 more on Organization.");
    expect(screen.getByTestId("settings.organization.overview-organization").textContent).toContain("1 more on Organization.");
    expect(screen.queryByTestId("settings.save-bar")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Loaded hourly rate" }));
    const rate = await screen.findByRole("textbox", { name: "Loaded hourly rate" });
    await waitFor(() => expect(document.activeElement).toBe(rate));
    expect(rate).not.toBeDisabled();
    expect(screen.getByTestId("settings.save-bar")).toBeTruthy();
  });

  it("lists the shipped products on the Business tab, empty and filled, and marks a repository that is not owner/name (#1432)", async () => {
    renderPage("business");
    const card = await screen.findByTestId("settings.organization.shipped-products");
    expect(card.textContent).toContain("No shipped product yet");
    cleanup();
    const base = get.getMockImplementation()!;
    get.mockImplementation(async (path: string) =>
      path === "/organization-context"
        ? { ...CONTEXT, profile: { ...CONTEXT.profile, shipped_products: [{ product: "Acme Server", vendor: "Acme", cpe: "", repositories: ["acme/server"] }] } }
        : base(path),
    );
    renderPage("business");
    expect(await screen.findByText("acme/server")).toBeTruthy();
    expect(screen.getAllByTestId("settings.organization.shipped-row")).toHaveLength(1);
    fireEvent.click(screen.getByTestId("settings.organization.edit-shipped_products"));
    fireEvent.click(await screen.findByRole("button", { name: /Add a shipped product/ }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Product" }), { target: { value: "Acme Agent" } });
    const repos = screen.getByRole("textbox", { name: "Source repositories" });
    fireEvent.change(repos, { target: { value: "agent" } });
    fireEvent.keyDown(repos, { key: "Enter" });
    // Not owner/name: the field is marked and the row counts as needing attention.
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Source repositories" }).getAttribute("aria-invalid")).toBe("true"));
  });

  it("refuses a shipped product with a repository that is not owner/name inline: nothing sent, no toast, the field focused (#1483 UX r1 B1)", async () => {
    renderPage("business");
    fireEvent.click(await screen.findByTestId("settings.organization.edit-shipped_products"));
    fireEvent.click(await screen.findByRole("button", { name: /Add a shipped product/ }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Product" }), { target: { value: "Acme Agent" } });
    const repos = screen.getByRole("textbox", { name: "Source repositories" });
    fireEvent.change(repos, { target: { value: "not a repo" } });
    fireEvent.keyDown(repos, { key: "Enter" });
    fireEvent.click(within(await screen.findByTestId("settings.organization.list-save-bar")).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Source repositories" })));
    expect(screen.getByRole("textbox", { name: "Source repositories" }).getAttribute("aria-invalid")).toBe("true");
    expect(put).not.toHaveBeenCalled();
    expect(screen.queryByText("The organization profile was not saved")).toBeNull();
  });

  it("lists the primary assets in a table with column headers", async () => {
    renderPage("business");
    await screen.findByText("Payments");
    expect(screen.getByRole("columnheader", { name: "Business service" })).toBeTruthy();
    expect(screen.getAllByTestId("settings.organization.primary-asset-row")).toHaveLength(1);
    // The row opens the primary asset's page, by name (#1980 S2).
    fireEvent.click(screen.getByTestId("settings.organization.primary-asset-row"));
    expect(screen.getByTestId("location-probe").textContent).toMatch(/^\/ctem\/inventory\/primary-assets\/[^/?]+$/);
  });

  it("keeps a list card on CTEM > Inventory always expanded, its count in the header (#1690, Sam 24 Nov 2026)", async () => {
    for (const tab of ["business", "technology"]) {
      const view = renderPage(tab);
      await waitFor(() => expect(screen.getAllByTestId("settings.card-count").length).toBeGreaterThan(0));
      expect(screen.queryAllByTestId("settings.card-collapse")).toHaveLength(0);
      view.unmount();
    }
    renderPage("business");
    const card = await screen.findByTestId("settings.organization.primary-assets");
    expect(within(card).getByTestId("settings.card-count").textContent).toBe("1");
    expect(within(card).getAllByTestId("settings.organization.primary-asset-row")).toHaveLength(1);
  });

  it("reads the row tables as text and edits them as fields; an empty value reads Not set, never a placeholder (UX r1 B1, B2 on #10804)", async () => {
    const base = get.getMockImplementation()!;
    get.mockImplementation(async (path: string) =>
      path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, software: [{ product: "CrowdStrike Falcon", vendor: "CrowdStrike", cpe: "" }] } } : base(path),
    );
    renderPage("business");
    const row = await screen.findByTestId("settings.organization.primary-asset-row");
    expect(within(row).getByText("Payments")).toBeTruthy();
    expect(within(row).getByText(/^Critical/)).toBeTruthy();
    expect(within(row).queryByRole("textbox")).toBeNull();
    expect(within(row).queryByRole("combobox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Edit Primary assets/ }));
    // In edit a row still reads as text until opened (#12256): one row of fields at a time.
    const jewel = await screen.findByTestId("settings.organization.primary-asset-row");
    await waitFor(() => expect(within(jewel).queryByRole("textbox")).toBeNull());
    fireEvent.click(within(screen.getByTestId("settings.organization.primary-asset-row")).getByText("Payments"));
    expect(await screen.findByDisplayValue("Payments")).toBeTruthy();
    expect(screen.getAllByRole("combobox", { name: "Criticality" })).toHaveLength(1);
  });

  it("reads the software rows as text on CTEM > Inventory and edits a row as fields (#1178)", async () => {
    const base = get.getMockImplementation()!;
    get.mockImplementation(async (path: string) =>
      path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, software: [{ product: "CrowdStrike Falcon", vendor: "CrowdStrike", cpe: "" }] } } : base(path),
    );
    renderPage("technology");
    const tech = (await screen.findByText("CrowdStrike Falcon")).closest("tr")!;
    // CPE and Version (CTEM-11, #928) are both empty: each reads Not set.
    expect(within(tech).getAllByText("Not set")).toHaveLength(2);
    expect(within(tech).queryByRole("textbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Edit Software/ }));
    fireEvent.click(await screen.findByText("CrowdStrike Falcon"));
    expect(await screen.findByRole("textbox", { name: "CPE (optional)" })).toBeTruthy();
  });

  it("bounds the columns so a long name is cut inside the card, in read mode and while editing (UX r2 B2 on #10804, UX r1 B2 on #12261)", async () => {
    const long = "Customer identity and payments platform for every retail and corporate banking region";
    const base = get.getMockImplementation()!;
    get.mockImplementation(async (path: string) =>
      path === "/organization-context"
        ? { ...CONTEXT, profile: { ...CONTEXT.profile, primary_assets: [{ name: long, business_service: "", owner: "", criticality: "critical", opencrq_asset_id: "" }] } }
        : base(path),
    );
    renderPage("business");
    const row = await screen.findByTestId("settings.organization.primary-asset-row");
    const table = row.closest("table")!;
    expect(table.dataset.layout).toBe("fixed");
    expect(within(table).getByRole("columnheader", { name: /Primary asset/ }).style.width).toBe("240px");
    expect(within(table).getByRole("columnheader", { name: /Criticality/ }).style.width).toBe("120px");
    expect(within(table).getByRole("columnheader", { name: /OpenCRQ/ }).style.width).toBe("112px");
    expect(within(row).getByText(long)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Edit Primary assets/ }));
    // The editor keeps the read widths, so the Actions column never leaves the card (#12261 UX r1 B2; was: auto).
    await screen.findAllByTestId("settings.organization.list-editor");
    const editTable = screen.getByTestId("settings.organization.primary-asset-row").closest("table")!;
    expect(editTable.dataset.layout).toBe("fixed");
    expect(within(editTable).getByRole("columnheader", { name: /Primary asset/ }).style.width).toBe("240px");
    fireEvent.click(within(screen.getByTestId("settings.organization.primary-asset-row")).getByText(long));
    await screen.findByDisplayValue(long);
  });

  it("reads the stored links of a primary asset as a count in the Assets column, a synced link included (#1978 S2)", async () => {
    const base = get.getMockImplementation()!;
    get.mockImplementation(async (path: string) => {
      if (path === "/organization-context")
        return {
          ...CONTEXT,
          profile: {
            ...CONTEXT.profile,
            primary_assets: [
              { name: "Employee  Workstations", business_service: "", owner: "", criticality: "high", opencrq_asset_id: "pa-1" },
              { name: "Payments", business_service: "", owner: "", criticality: "critical", opencrq_asset_id: "", asset_names: ["pay-db"] },
              { name: "Laptops", business_service: "", owner: "", criticality: "high", opencrq_asset_id: "pa-2" },
            ],
          },
        };
      if (path === "/organization-context/primary-assets/link-counts")
        return { items: { "employee workstations": { total: 263, manual: 0, opencrq: 263, rule: 0 }, payments: { total: 1, manual: 1, opencrq: 0, rule: 0 }, laptops: { total: 1234, manual: 0, opencrq: 1234, rule: 0 } } };
      return base(path);
    });
    renderPage("business");
    // A sync writes links, never asset_names: the row reads their count, never "Not set" (#1978 cause 4).
    expect(await screen.findByText("263 linked assets")).toBeTruthy();
    // The names typed by hand stay as they are while they are all the links.
    expect(screen.getByText("pay-db")).toBeTruthy();
    // From 1,000 the count is compact, never the grouped full number (UX r1 B1 on #2000).
    expect(screen.getByText("1.2K linked assets")).toBeTruthy();
    expect(screen.queryByText("1,234 linked assets")).toBeNull();
  });

  it("edits one list card alone, counts its changes, undoes a removal and patches that list alone (#12256, #1752)", async () => {
    const base = get.getMockImplementation()!;
    const tech = Array.from({ length: 150 }, (_, i) => ({ product: `Product ${String(i).padStart(3, "0")}`, vendor: i === 7 ? "" : "Vendor", cpe: "" }));
    get.mockImplementation(async (path: string) => (path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, software: tech } } : base(path)));
    patch.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderPage("technology");
    // Read mode names the row that needs attention on the card header.
    expect((await screen.findByTestId("settings.organization.attention-software")).textContent).toBe("1 needs attention");
    fireEvent.click(screen.getByRole("button", { name: /Edit Software/ }));
    const bar = await screen.findByTestId("settings.organization.list-save-bar");
    // 25 rows a page, never the 150.
    expect(screen.getAllByTestId("settings.organization.tech-row")).toHaveLength(25);
    const save = within(bar).getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    const first = screen.getAllByTestId("settings.organization.tech-row")[0];
    fireEvent.click(within(first).getByRole("button", { name: "Remove" }));
    expect(bar.textContent).toContain("1 removed");
    fireEvent.click(within(screen.getAllByTestId("settings.organization.tech-row")[0]).getByRole("button", { name: /Undo/ }));
    expect(bar.textContent).toContain("No change yet");
    fireEvent.click(screen.getByRole("button", { name: "Add a product" }));
    const product = await screen.findByRole("textbox", { name: "Product" });
    await waitFor(() => expect(document.activeElement).toBe(product));
    fireEvent.change(product, { target: { value: "Jira" } });
    // The vendor field suggests the vendors already in the list (slice 2): an input with a list is a combobox.
    fireEvent.change(screen.getByRole("combobox", { name: "Vendor" }), { target: { value: "Atlassian" } });
    fireEvent.keyDown(product, { key: "Enter" });
    expect(bar.textContent).toContain("1 added");
    fireEvent.click(save);
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(put).not.toHaveBeenCalled();
    const [path, body] = patch.mock.calls[0] as [string, { fields: Record<string, unknown> & { software: { product: string }[] }; based_on_version: number }];
    expect(path).toBe("/organization-context");
    // The network check of #1752: the card's own list alone travels, never another field or an asset row.
    expect(Object.keys(body.fields)).toEqual(["software"]);
    expect(body.based_on_version).toBe(CONTEXT.version);
    expect(body.fields.software).toHaveLength(151);
    expect(body.fields.software[150].product).toBe("Jira");
  });

  it("draws no asset or group table on the Exposure tab: the rows are on their row APIs, never on the profile (#1752 slice 3b)", async () => {
    renderPage("exposure");
    await screen.findByTestId("settings.organization.panel-exposure");
    expect(screen.queryByTestId("settings.organization.asset-groups")).toBeNull();
    expect(screen.queryByTestId("settings.organization.assets-exposure")).toBeNull();
    expect(put).not.toHaveBeenCalled();
  });

  it("drops an exposure card's edit on Cancel, nothing sent (#12256 slice 2)", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Edit Enterprise knowledge/ }));
    const card = screen.getByTestId("settings.organization.enterprise-knowledge");
    fireEvent.click(within(card).getByRole("button", { name: "Link a knowledge base" }));
    fireEvent.click(within(within(card).getByTestId("settings.save-bar")).getByRole("button", { name: "Cancel" }));
    expect(within(card).queryByTestId("settings.save-bar")).toBeNull();
    expect(put).not.toHaveBeenCalled();
    // The focus goes back to the card's Edit, never to the page body (#12271 UX r1 B3).
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: /Edit Enterprise knowledge/ })));
  });

  it("returns the focus to a list card's Edit on Cancel (#12271 UX r1 B3)", async () => {
    renderPage("business");
    fireEvent.click(await screen.findByRole("button", { name: /Edit Primary assets/ }));
    const bar = await screen.findByTestId("settings.organization.list-save-bar");
    fireEvent.click(within(bar).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: /Edit Primary assets/ })));
  });


  it("pastes a list into a card in edit, skips the rows already there, sets a vendor on the selected rows and saves (#12256 slice 2)", async () => {
    const base = get.getMockImplementation()!;
    const tech = [{ product: "Jira", vendor: "Atlassian", cpe: "" }, { product: "Slack", vendor: "", cpe: "" }];
    get.mockImplementation(async (path: string) => (path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, software: tech } } : base(path)));
    patch.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderPage("technology");
    // Paste a list waits for the card's Edit.
    expect(screen.queryByTestId("settings.organization.paste-software")).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: /Edit Software/ }));
    const bar = await screen.findByTestId("settings.organization.list-save-bar");
    fireEvent.click(screen.getByTestId("settings.organization.paste-software"));
    const dialog = await screen.findByTestId("settings.organization.paste-dialog");
    fireEvent.change(within(dialog).getByTestId("settings.organization.paste-text"), { target: { value: "jira, atlassian\nNotion; Notion Labs\n, nobody" } });
    const preview = within(dialog).getByTestId("settings.organization.paste-preview");
    expect(preview.textContent).toContain("1 row to add");
    expect(preview.textContent).toContain("1 already in the list");
    expect(preview.textContent).toContain("1 line not read");
    fireEvent.click(within(dialog).getByTestId("settings.organization.paste-add"));
    await waitFor(() => expect(screen.queryByTestId("settings.organization.paste-dialog")).toBeNull());
    expect(bar.textContent).toContain("1 added");
    // The added row takes the focus, never the page body (#12271 UX r1 B3).
    await waitFor(() => expect(document.activeElement?.tagName).toBe("TR"));
    expect(document.activeElement?.textContent).toContain("Notion");
    // Select Slack and Notion, set their vendor in one go.
    for (const name of ["Slack", "Notion"]) {
      const row = screen.getAllByTestId("settings.organization.tech-row").find((line) => line.textContent?.includes(name))!;
      fireEvent.click(within(row).getByRole("checkbox"));
    }
    const bulk = screen.getByTestId("settings.organization.bulk-set");
    fireEvent.change(within(bulk).getByRole("combobox", { name: "Value of Vendor" }), { target: { value: "Acme" } });
    fireEvent.click(within(bulk).getByTestId("settings.organization.bulk-apply"));
    expect(bar.textContent).toContain("1 added, 1 edited");
    fireEvent.click(within(bar).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const [, body] = patch.mock.calls[0] as [string, { fields: { software: { product: string; vendor: string }[] } }];
    expect(body.fields.software).toEqual([
      { product: "Jira", vendor: "Atlassian", cpe: "" },
      { product: "Slack", vendor: "Acme", cpe: "" },
      { product: "Notion", vendor: "Acme", cpe: "" },
    ]);
  });

  it("reads and pastes the version the organization runs on a software row, and saves it only when set (CTEM-11, #928)", async () => {
    const base = get.getMockImplementation()!;
    const tech = [{ product: "Jira", vendor: "Atlassian", cpe: "", version: "9.12.2" }];
    get.mockImplementation(async (path: string) => (path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, software: tech } } : base(path)));
    patch.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderPage("technology");
    const row = (await screen.findByText("Jira")).closest("tr")!;
    expect(within(row).getByText("9.12.2")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: /Edit Software/ }));
    const bar = await screen.findByTestId("settings.organization.list-save-bar");
    fireEvent.click(screen.getByTestId("settings.organization.paste-software"));
    const dialog = await screen.findByTestId("settings.organization.paste-dialog");
    fireEvent.change(within(dialog).getByTestId("settings.organization.paste-text"), { target: { value: "nginx, F5, , 1.25.3\nSlack, Salesforce" } });
    fireEvent.click(within(dialog).getByTestId("settings.organization.paste-add"));
    await waitFor(() => expect(screen.queryByTestId("settings.organization.paste-dialog")).toBeNull());
    fireEvent.click(within(bar).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const [, body] = patch.mock.calls[0] as [string, { fields: { software: Record<string, string>[] } }];
    expect(body.fields.software).toEqual([
      { product: "Jira", vendor: "Atlassian", cpe: "", version: "9.12.2" },
      { product: "nginx", vendor: "F5", cpe: "", version: "1.25.3" },
      // No version typed: none saved, so a profile that never declares one keeps its shape.
      { product: "Slack", vendor: "Salesforce", cpe: "" },
    ]);
  });

  it("keeps a card's unsaved rows: page Edit and the field jumps wait, and Next still pages with an added row open (#12261 UX r1 B1, B3)", async () => {
    const base = get.getMockImplementation()!;
    const tech = Array.from({ length: 60 }, (_, i) => ({ product: `Product ${String(i).padStart(3, "0")}`, vendor: "Vendor", cpe: "" }));
    get.mockImplementation(async (path: string) => (path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, software: tech } } : base(path)));
    renderPage("technology");
    fireEvent.click(await screen.findByRole("button", { name: /Edit Software/ }));
    const editor = await screen.findByTestId("settings.organization.list-editor");
    fireEvent.click(screen.getByRole("button", { name: "Add a product" }));
    const product = await screen.findByRole("textbox", { name: "Product" });
    fireEvent.change(product, { target: { value: "Jira" } });
    // B1: the card keeps its row (CTEM > Inventory draws no page-wide Edit, #1178).
    expect(screen.queryByTestId("settings.organization.edit")).toBeNull();
    expect(editor.getAttribute("data-changes")).toBe("1");
    // B3: the pager moves while the added row is open.
    fireEvent.click(within(editor).getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(within(editor).getByText(/Page 2 of/)).toBeTruthy());
  });

  it("filters the rows that need attention while editing, and Esc puts an open row back (#12256)", async () => {
    const base = get.getMockImplementation()!;
    get.mockImplementation(async (path: string) =>
      path === "/organization-context"
        ? { ...CONTEXT, profile: { ...CONTEXT.profile, software: [{ product: "Concord", vendor: "", cpe: "" }, { product: "Jira", vendor: "Atlassian", cpe: "" }] } }
        : base(path),
    );
    renderPage("technology");
    fireEvent.click(await screen.findByRole("button", { name: /Edit Software/ }));
    await screen.findByTestId("settings.organization.list-save-bar");
    const marks = screen.getAllByRole("img", { name: "Vendor is empty" });
    expect(marks).toHaveLength(1);
    fireEvent.click(screen.getByText("Jira"));
    const product = await screen.findByDisplayValue("Jira");
    fireEvent.change(product, { target: { value: "Jirra" } });
    fireEvent.keyDown(product, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Product" })).toBeNull());
    expect(screen.getByText("Jira")).toBeTruthy();
    expect(screen.getByTestId("settings.organization.list-save-bar").textContent).toContain("No change yet");
  });

  it("keeps search, sort and the pager on the primary assets while editing (#12256; was: none while editing)", async () => {
    renderPage("business");
    await screen.findByText("Payments");
    // The primary assets card: its own search box in read mode, none while editing. The form's pickers
    // (ticketing, regions) render only while editing since #10855, so the count is read inside the card.
    const jewelsTable = () => screen.getByTestId("settings.organization.primary-asset-row").closest("table")!;
    const searchNear = () => jewelsTable().parentElement?.parentElement?.querySelectorAll("input[placeholder='Search...']").length ?? 0;
    expect(searchNear()).toBeGreaterThan(0);
    expect(jewelsTable().querySelector("th[aria-sort]")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Edit Primary assets/ }));
    await screen.findByTestId("settings.organization.list-save-bar");
    expect(searchNear()).toBeGreaterThan(0);
    expect(jewelsTable().querySelector("th[aria-sort]")).toBeTruthy();
    expect(screen.getAllByTestId("settings.organization.primary-asset-row")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Remove" }).length).toBeGreaterThan(0);
  });

  it("saves the edited profile as a form version", async () => {
    put.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderPage("risk");
    await screen.findByTestId("settings.organization.panel-risk");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    fireEvent.change(screen.getByLabelText("Loaded hourly rate"), { target: { value: "120" } });
    fireEvent.click(screen.getByRole("button", { name: /Save/ }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const [path, body] = put.mock.calls[0] as [string, { profile: { loaded_hourly_rate: number; regions: string[] }; source: string }];
    expect(path).toBe("/organization-context");
    expect(body.source).toBe("form");
    expect(body.profile.loaded_hourly_rate).toBe(120);
    // The picked regions travel as the list of codes, unchanged.
    expect(body.profile.regions).toEqual(["FR"]);
  });

  it("refuses an amount that is not a number without calling the API", async () => {
    renderPage("risk");
    await screen.findByTestId("settings.organization.panel-risk");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    fireEvent.change(screen.getByLabelText("Loaded hourly rate"), { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: /Save/ }));
    // The error is inline under the field and tied to it, never a toast (UX B4).
    const error = await screen.findByRole("alert");
    expect(error.textContent).toBe("Not a valid amount: type a number, without letters, 0 or more.");
    expect(screen.getByLabelText("Loaded hourly rate").getAttribute("aria-describedby")).toBe(error.id);
    expect(put).not.toHaveBeenCalled();
  });

  it("marks the refused amount invalid and focuses it, until it is edited", async () => {
    renderPage("risk");
    await screen.findByTestId("settings.organization.panel-risk");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    const rate = screen.getByRole("textbox", { name: "Loaded hourly rate" });
    fireEvent.change(rate, { target: { value: "12O" } });
    fireEvent.click(screen.getByRole("button", { name: /Save/ }));
    await waitFor(() => expect(rate).toHaveAttribute("aria-invalid", "true"));
    expect(document.activeElement).toBe(rate);
    expect(screen.getByRole("textbox", { name: "Escalation threshold" })).not.toHaveAttribute("aria-invalid", "true");
    fireEvent.change(rate, { target: { value: "120" } });
    expect(rate).not.toHaveAttribute("aria-invalid", "true");
  });

  it("draws the amount's unit inside the field's own box, hidden from assistive tech (UX round 2 B1)", async () => {
    renderPage("risk");
    await screen.findByTestId("settings.organization.panel-risk");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    const rate = await screen.findByRole("textbox", { name: "Loaded hourly rate" });
    // The unit is a decoration beside the input, in the same positioned box: the
    // screen reader reads the field's label, never the suffix.
    const unit = screen.getByText("EUR per hour");
    expect(unit).toHaveAttribute("aria-hidden");
    expect(unit.parentElement?.contains(rate)).toBe(true);
    expect(rate.contains(unit)).toBe(false);
  });

  it("labels the currency field with its own key, not the chart number format (UX round 2 B3)", async () => {
    renderPage("risk");
    await screen.findByTestId("settings.organization.panel-risk");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    expect(await screen.findByText("Currency of the amounts", { selector: "label" })).toBeTruthy();
  });

  it("shows no region picker at rest, and the picker tied to its help line on Edit (UX round 2 B2, #10855)", async () => {
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
    expect(screen.queryByRole("combobox", { name: "Regions of operation" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    const regions = await screen.findByRole("combobox", { name: "Regions of operation" });
    expect(regions).not.toBeDisabled();
    expect(document.getElementById(regions.getAttribute("aria-describedby") ?? "")).toBeTruthy();
  });

  // The PIR list lives on the Prioritization policy of CTEM > Inventory > Exposure policy (#1889).
  const renderPolicy = () =>
    renderWithProviders(
      <MemoryRouter initialEntries={["/ctem/inventory/exposure"]}>
        <OrganizationSection embed={{ cards: ["prioritization_policy"] }} />
      </MemoryRouter>,
    );
  const editPolicy = async () => fireEvent.click(await screen.findByTestId("settings.organization.edit-prioritization_policy"));

  it("draws the PIR list on the Prioritization policy only, never on Settings, edited with the card (#1889)", async () => {
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
    expect(screen.queryByText("Priority intelligence requirements (PIRs)")).toBeNull();
    expect(screen.queryByText("Ticketing and intelligence")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    await screen.findByRole("combobox", { name: "Regions of operation" });
    expect(screen.queryByRole("combobox", { name: "Priority intelligence requirements (PIRs)" })).toBeNull();
    cleanup();
    renderPolicy();
    const card = await screen.findByTestId("settings.organization.prioritization-policy");
    expect(await within(card).findByText("Priority intelligence requirements (PIRs)")).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "Priority intelligence requirements (PIRs)" })).toBeNull();
    await editPolicy();
    expect(await screen.findByRole("combobox", { name: "Priority intelligence requirements (PIRs)" })).not.toBeDisabled();
  });

  const withPirs = (pirs: unknown, profile: Partial<OrganizationContextResponse["profile"]>) =>
    get.mockImplementation(async (path: string) => {
      if (path === "/organization-context") return { ...CONTEXT, profile: { ...CONTEXT.profile, ...profile } };
      if (path === "/organizations" || path === "/integrations") return [];
      // The PIR sources of the card (#1938).
      if (path.startsWith("/organization-context/syncs") || path.startsWith("/organization-context/sources")) return [];
      if (path === "/organization-context/pirs") {
        if (pirs instanceof Error) throw pirs;
        return pirs;
      }
      throw new Error(`unexpected GET ${path}`);
    });

  it("names the saved PIRs in read mode, linked to OpenCTI, never by id (#9615)", async () => {
    withPirs(
      { pirs: [{ id: "1ef07227-6f37", name: "Ransomware on energy" }], unavailable: null, opencti_url: "https://cti.example" },
      { pir_ids: ["1ef07227-6f37"] },
    );
    renderPolicy();
    const link = await screen.findByRole("link", { name: "Ransomware on energy" });
    expect(link.getAttribute("href")).toBe("https://cti.example/dashboard/pirs/1ef07227-6f37");
    expect(screen.queryByText("1ef07227-6f37")).toBeNull();
    expect(screen.queryByPlaceholderText("Search a PIR")).toBeNull();
  });

  it("marks a PIR OpenCTI no longer has, with its last known name, and removes it with the card's Save (#9615, #1889)", async () => {
    withPirs({ pirs: [], unavailable: null, opencti_url: "https://cti.example" }, { pir_ids: ["pir-gone"], pir_names: { "pir-gone": "Old campaign" } });
    patch.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderPolicy();
    // The state leads, so a chip cut at its width still says it; the chip carries the whole label as its title (UX round 1 B2).
    const chip = await screen.findByText("No longer in OpenCTI: Old campaign");
    expect(screen.getByTitle("No longer in OpenCTI: Old campaign")).toBeTruthy();
    expect(chip).toBeTruthy();
    expect(screen.queryByText("pir-gone")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    fireEvent.click(within(await screen.findByTestId("settings.save-bar")).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    const [, body] = patch.mock.calls[0] as [string, { fields: { pir_ids: string[] } }];
    expect(body.fields.pir_ids).toEqual([]);
    expect(put).not.toHaveBeenCalled();
  });

  it("writes a missing PIR out whole under the edit picker, state first (UX round 1 B2)", async () => {
    withPirs({ pirs: [{ id: "pir-1", name: "Ransomware on energy" }], unavailable: null, opencti_url: "" }, {
      pir_ids: ["pir-1", "pir-gone"],
      pir_names: { "pir-gone": "Initial access brokers selling our domains" },
    });
    renderPolicy();
    await screen.findByText("No longer in OpenCTI: Initial access brokers selling our domains");
    await editPolicy();
    const list = await screen.findByRole("list", { name: "PIR no longer in OpenCTI" });
    expect(within(list).getByText("No longer in OpenCTI: Initial access brokers selling our domains")).toBeTruthy();
    expect(within(list).queryByText("Ransomware on energy")).toBeNull();
  });

  it("keeps the last known names and offers Retry when OpenCTI cannot be read (#9615)", async () => {
    withPirs({ pirs: [], unavailable: "unreadable", opencti_url: "https://cti.example" }, { pir_ids: ["pir-1", "pir-2"], pir_names: { "pir-1": "Ransomware on energy" } });
    renderPolicy();
    expect(await screen.findByRole("link", { name: "Ransomware on energy" })).toBeTruthy();
    expect(screen.getByText("PIR name not available")).toBeTruthy();
    expect(screen.queryByText("pir-2")).toBeNull();
    expect(screen.getByText("OpenCTI could not be read, names may be out of date")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(get.mock.calls.filter(([path]) => path === "/organization-context/pirs").length).toBeGreaterThan(1));
  });

  it("draws skeleton chips while the names load, never placeholder text, in read and edit mode (#11589)", async () => {
    get.mockImplementation(async (path: string) => {
      if (path === "/organization-context") return { ...CONTEXT, profile: { ...CONTEXT.profile, pir_ids: ["pir-1", "pir-2"] } };
      if (path === "/organizations" || path === "/integrations") return [];
      if (path === "/organization-context/pirs") return new Promise(() => {});
      throw new Error(`unexpected GET ${path}`);
    });
    renderPolicy();
    expect(await screen.findByText("Loading the PIR names")).toBeTruthy();
    expect(screen.queryByText("PIR name not available")).toBeNull();
    // The outlined chip skeleton (UX #11595 R1 B1) is guarded on the source: tests_unit/organization-pirs-badge.test.ts.
    expect(screen.getAllByTestId("settings.organization.pir-chip-skeleton").length).toBe(2);
    await editPolicy();
    expect(await screen.findByTestId("settings.organization.pirs-loading")).toBeTruthy();
    expect(screen.queryByText("PIR name not available")).toBeNull();
  });

  it("offers Retry in edit mode too when OpenCTI cannot be read (#11589)", async () => {
    withPirs({ pirs: [], unavailable: "unreadable", opencti_url: "" }, { pir_ids: ["pir-1"], pir_names: { "pir-1": "Ransomware on energy" } });
    renderPolicy();
    await screen.findByText("Ransomware on energy");
    await editPolicy();
    const line = await screen.findByTestId("settings.organization.pirs-unreadable");
    expect(within(line).getByText("OpenCTI could not be read, names may be out of date")).toBeTruthy();
    const before = get.mock.calls.filter(([path]) => path === "/organization-context/pirs").length;
    fireEvent.click(within(line).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(get.mock.calls.filter(([path]) => path === "/organization-context/pirs").length).toBeGreaterThan(before));
  });

  it("shows each PIR once and tells apart two PIRs of the same name (#11589)", async () => {
    withPirs(
      {
        pirs: [
          { id: "aaaa1111-x", name: "Retail threats" },
          { id: "bbbb2222-y", name: "Retail threats" },
        ],
        unavailable: null,
        opencti_url: "",
      },
      { pir_ids: ["aaaa1111-x", "bbbb2222-y", "aaaa1111-x"] },
    );
    renderPolicy();
    expect(await screen.findByText("Retail threats (aaaa1111)")).toBeTruthy();
    expect(screen.getAllByText("Retail threats (aaaa1111)")).toHaveLength(1);
    expect(screen.getByText("Retail threats (bbbb2222)")).toBeTruthy();
  });

  it("draws no PIR details card on the profile: the details open from each PIR of the policy card (#1974)", async () => {
    withPirs({ pirs: [{ id: "pir-1", name: "Ransomware on energy" }], unavailable: null }, { pir_ids: ["pir-1"] });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
    expect(screen.queryByTestId("settings.organization.pir-details")).toBeNull();
    expect(screen.queryByText("PIR details")).toBeNull();
  });

  it("shows each PIR's completeness and saves its details from the dialog, the others kept (#1974)", async () => {
    withPirs(
      { pirs: [{ id: "pir-1", name: "Ransomware on energy" }, { id: "pir-2", name: "Retail threats" }], unavailable: null },
      {
        pir_ids: ["pir-1", "pir-2"],
        pir_details: {
          "pir-1": { question: "Who targets us?", owner: "CTI", review_cadence: "", primary_assets: [] },
          "pir-2": { question: "Which retail groups?", owner: "", review_cadence: "monthly", primary_assets: ["Payments"] },
        },
      },
    );
    patch.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderPolicy();
    const open = await screen.findByRole("button", { name: "Details of Ransomware on energy, 2 of 4 set" });
    expect(screen.getByRole("button", { name: "Details of Retail threats, 3 of 4 set" })).toBeTruthy();
    fireEvent.click(open);
    const dialog = await screen.findByTestId("settings.organization.pir-details-dialog");
    expect(within(dialog).getByText("PIR details: Ransomware on energy")).toBeTruthy();
    const question = within(dialog).getByLabelText("Question") as HTMLInputElement;
    expect(question.value).toBe("Who targets us?");
    fireEvent.change(within(dialog).getByLabelText("Owner"), { target: { value: "SOC" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    const [, body] = patch.mock.calls[0] as [string, { fields: { pir_details: Record<string, { owner: string }> } }];
    expect(body.fields.pir_details["pir-1"].owner).toBe("SOC");
    expect(body.fields.pir_details["pir-2"]).toEqual({ question: "Which retail groups?", owner: "", review_cadence: "monthly", primary_assets: ["Payments"] });
    expect(Object.keys(body.fields)).toEqual(["pir_details"]);
    expect(put).not.toHaveBeenCalled();
  });

  it("opens the same details read-only for a reader, with no Save (#1974)", async () => {
    // A reader never reads the administrators-only PIR list: the name saved with the PIR names it.
    withPirs({ pirs: [], unavailable: null }, { pir_ids: ["pir-1"], pir_names: { "pir-1": "Ransomware on energy" } });
    renderWithProviders(
      <MemoryRouter initialEntries={["/ctem/inventory/exposure"]}>
        <OrganizationSection embed={{ cards: ["prioritization_policy"], readOnly: true }} />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Details of Ransomware on energy, 0 of 4 set" }));
    const dialog = await screen.findByTestId("settings.organization.pir-details-dialog");
    expect(within(dialog).getByTestId("settings.organization.pir-details-read")).toBeTruthy();
    expect(within(dialog).queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("shows eight PIRs, then +N more in place (#9615)", async () => {
    const ids = Array.from({ length: 11 }, (_, i) => `pir-${i}`);
    withPirs({ pirs: ids.map((id) => ({ id, name: `Name ${id}` })), unavailable: null, opencti_url: "" }, { pir_ids: ids });
    renderPolicy();
    expect(await screen.findByText("Name pir-7")).toBeTruthy();
    expect(screen.queryByText("Name pir-8")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "+3 more" }));
    expect(await screen.findByText("Name pir-10")).toBeTruthy();
  });

  it("saves the names OpenCTI gives with the PIRs, from the whole form and from the policy card (#9615, #1889)", async () => {
    withPirs({ pirs: [{ id: "pir-1", name: "Ransomware on energy" }], unavailable: null }, { pir_ids: ["pir-1"] });
    put.mockResolvedValue({ ...CONTEXT, version: 4 });
    patch.mockResolvedValue({ ...CONTEXT, version: 4 });
    const view = renderPage("risk");
    await screen.findByTestId("settings.organization.panel-risk");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    fireEvent.change(screen.getByLabelText("Loaded hourly rate"), { target: { value: "120" } });
    fireEvent.click(await screen.findByRole("button", { name: /Save/ }));
    await waitFor(() => expect(put).toHaveBeenCalled());
    const [, body] = put.mock.calls[0] as [string, { profile: { pir_names: Record<string, string> } }];
    expect(body.profile.pir_names).toEqual({ "pir-1": "Ransomware on energy" });
    view.unmount();
    // No OpenCTI address answered: the name is shown, unlinked.
    renderPolicy();
    expect(await screen.findByText("Ransomware on energy")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Ransomware on energy" })).toBeNull();
  });

  it("says the PIRs could not be listed, never that OpenCTI has none, when OpenCTI did not answer (UX round 2 B4)", async () => {
    get.mockImplementation(async (path: string) => {
      if (path === "/organization-context") return CONTEXT;
      if (path === "/organizations" || path === "/integrations") return [];
      if (path === "/organization-context/pirs") return { pirs: [], unavailable: "unreadable" };
      throw new Error(`unexpected GET ${path}`);
    });
    renderPolicy();
    await editPolicy();
    // Since #11589 an unreadable OpenCTI shows the Retry line in edit mode too, the helper stays the field's own.
    expect(await screen.findByTestId("settings.organization.pirs-unreadable")).toBeTruthy();
    fireEvent.click(screen.getByRole("combobox", { name: "Priority intelligence requirements (PIRs)" }));
    expect(await screen.findByText("The PIRs could not be listed: OpenCTI did not answer.")).toBeTruthy();
    expect(screen.queryByText("No PIR in OpenCTI")).toBeNull();
  });

  it("has no OpenCRQ button on the primary assets card: the OpenCRQ source is run from Inventory > Sources (#1754)", async () => {
    renderPage("business");
    await screen.findByTestId("settings.organization.primary-assets");
    expect(screen.queryByRole("button", { name: /Check against OpenCRQ/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Sync with OpenCRQ now/ })).toBeNull();
    expect(screen.queryByTestId("settings.organization.primary-assets-source")).toBeNull();
    expect(post).not.toHaveBeenCalledWith("/organization-context/opencrq-sync", expect.anything());
  });

  it("names no source on the primary assets card, even with an OpenCRQ primary-asset sync saved (#1883)", async () => {
    const opencrqSync = { id: "s-crq", section: "primary_assets", source: "opencrq", filters: {}, batch_type: "", remove_gone: false, frequency: "daily", hour: 6, minute: 0, weekday: 0, timezone: "UTC", enabled: true, paused_reason: "", owner_email: "admin@example.com", next_run_at: null, last_run_at: new Date().toISOString(), last_result: { added: 1, updated: 0, removed: 0, version: 4, error: null } };
    const base = get.getMockImplementation();
    get.mockImplementation(async (path: string) => (path.startsWith("/organization-context/syncs") ? [opencrqSync] : base?.(path)));
    renderPage("business");
    const card = await screen.findByTestId("settings.organization.primary-assets");
    expect(screen.queryByTestId("settings.organization.primary-assets-source")).toBeNull();
    expect(card.textContent).not.toMatch(/Synced from OpenCRQ|not run yet|OpenCRQ sync/);
    expect(get).not.toHaveBeenCalledWith(expect.stringMatching(/^\/organization-context\/syncs\?.*scope=inventory/));
  });

  it("draws the OpenCTI PIR sources under the PIR list of the Prioritization policy, with Add a source opening the PIR import (#1938)", async () => {
    const base = get.getMockImplementation();
    const pirSync = { id: "s-pir", section: "pirs", source: "opencti", filters: {}, batch_type: "", remove_gone: false, frequency: "daily", hour: 6, minute: 0, weekday: 0, timezone: "UTC", enabled: true, paused_reason: "", owner_email: "admin@example.com", next_run_at: null, last_run_at: null, last_result: null };
    get.mockImplementation(async (path: string) => (path.startsWith("/organization-context/syncs") ? [pirSync] : path.startsWith("/organization-context/sources") ? [] : base?.(path)));
    renderPolicy();
    const card = await screen.findByTestId("settings.organization.prioritization-policy");
    const section = await within(card).findByTestId("organization-sources");
    expect(section.textContent).toContain("PIR sources");
    await waitFor(() => expect(within(section).getAllByTestId("organization-sources.row")).toHaveLength(1));
    expect(get).toHaveBeenCalledWith(expect.stringMatching(/^\/organization-context\/syncs\?.*scope=profile/));
    // Another card's edit never turns it off: only the policy's own save does.
    const add = within(section).getByTestId("organization-sources.add");
    expect(add).not.toBeDisabled();
    await editPolicy();
    expect(within(section).getByTestId("organization-sources.add")).not.toBeDisabled();
    fireEvent.click(within(section).getByTestId("organization-sources.add"));
    expect(await screen.findByRole("dialog")).toBeTruthy();
  });

  it("never mounts the PIR sources for a reader of the Prioritization policy: no administrators-only syncs or sources read (#1938)", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/ctem/inventory/exposure"]}>
        <OrganizationSection embed={{ cards: ["prioritization_policy"], readOnly: true }} />
      </MemoryRouter>,
    );
    const card = await screen.findByTestId("settings.organization.prioritization-policy");
    expect(within(card).queryByTestId("organization-sources")).toBeNull();
    expect(get).not.toHaveBeenCalledWith(expect.stringMatching(/^\/organization-context\/(syncs|sources)/));
  });

  it("reads a former Knowledge and sources link as the Organization tab, which holds its cards, with no Sources and syncs (#1975, #1938)", async () => {
    renderPage("knowledge");
    const panel = await screen.findByTestId("settings.organization.panel-organization");
    expect(within(panel).getByTestId("settings.organization.enterprise-knowledge")).toBeTruthy();
    expect(within(panel).queryByTestId("organization-sources")).toBeNull();
    expect(screen.queryByTestId("settings.organization.knowledge-header")).toBeNull();
    expect(screen.getByRole("tab", { name: /^Organization/ }).getAttribute("aria-selected")).toBe("true");
  });

  it("opens ?setup on the first tab with a field not filled, with the save bar (#1975)", async () => {
    get.mockImplementation(async (path: string) =>
      path === "/organizations" || path === "/integrations" ? [] : { ...CONTEXT, missing_fields: ["risk_appetite.approval_threshold"] },
    );
    renderWithProviders(
      <MemoryRouter initialEntries={["/settings/organization?setup=1"]}>
        <OrganizationSection />
        <LocationProbe />
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("settings.organization.panel-risk")).toBeTruthy();
    expect(screen.getByTestId("settings.organization.setup-guide")).toBeTruthy();
    expect(screen.getByRole("button", { name: /^Save/ })).toBeTruthy();
  });

  it("marks a primary asset linked to OpenCRQ with a named icon", async () => {
    get.mockImplementation(async (path: string) =>
      path === "/organizations" || path === "/integrations"
        ? []
        : {
            ...CONTEXT,
            profile: { ...CONTEXT.profile, primary_assets: [{ ...CONTEXT.profile.primary_assets[0], opencrq_asset_id: "a-1" }] },
          },
    );
    renderPage("business");
    expect(await screen.findByRole("img", { name: "Linked to OpenCRQ" })).toBeTruthy();
  });

  it("restores the primary assets of a past version alone, from the version it starts from (#1558)", async () => {
    const base = get.getMockImplementation()!;
    get.mockImplementation(async (path: string) => {
      if (path === "/organization-context") return { ...CONTEXT, version: 46, profile: { ...CONTEXT.profile, primary_assets: [] } };
      if (path.startsWith("/organization-context/versions?"))
        return [
          { version: 46, source: "form", created_at: "2026-11-23T10:00:00Z", author_user_id: null, missing_fields: [], primary_assets: 0 },
          { version: 28, source: "form", created_at: "2026-11-20T10:00:00Z", author_user_id: null, missing_fields: [], primary_assets: 2 },
        ];
      if (path === "/organization-context/versions/28")
        return {
          ...CONTEXT,
          version: 28,
          profile: {
            ...CONTEXT.profile,
            primary_assets: [
              { name: "Payments", business_service: "", owner: "", criticality: "critical", opencrq_asset_id: "" },
              { name: "Ledger", business_service: "", owner: "", criticality: "high", opencrq_asset_id: "" },
            ],
          },
        };
      return base(path);
    });
    post.mockResolvedValue({ ...CONTEXT, version: 47, profile: { ...CONTEXT.profile, primary_assets: [{ name: "Payments" }, { name: "Ledger" }] } });
    renderPage("business");
    fireEvent.click(await screen.findByRole("button", { name: /Restore from a past version/ }));
    const dialog = await screen.findByTestId("settings.organization.restore-dialog");
    // The current version is never offered; the newest one holding primary assets is picked and previewed.
    expect(within(dialog).queryByText("Version 46")).toBeNull();
    expect(await within(dialog).findByText("Ledger")).toBeTruthy();
    // The preview badges carry the criticality words of the card, never the bare "High" key (#1567 UX r2 B2).
    expect(within(dialog).getByText("High (criticality)")).toBeTruthy();
    expect(within(dialog).getByText("Critical (criticality)")).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore version 28" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/organization-context/versions/28/restore", { fields: ["primary_assets"], based_on_version: 46 }),
    );
  });

  it("shows a refused restore inline in the dialog and offers a reload of the versions on a 409 (#1567 UX B2)", async () => {
    const base = get.getMockImplementation()!;
    get.mockImplementation(async (path: string) => {
      if (path === "/organization-context") return { ...CONTEXT, version: 46, profile: { ...CONTEXT.profile, primary_assets: [] } };
      if (path.startsWith("/organization-context/versions?"))
        return [{ version: 28, source: "form", created_at: "2026-11-20T10:00:00Z", author_user_id: null, missing_fields: [], primary_assets: 1 }];
      if (path === "/organization-context/versions/28")
        return { ...CONTEXT, version: 28, profile: { ...CONTEXT.profile, primary_assets: [{ name: "Ledger", business_service: "", owner: "", criticality: "high", opencrq_asset_id: "" }] } };
      return base(path);
    });
    post.mockRejectedValue(Object.assign(new Error("The profile changed since version 46 (now version 47)."), { status: 409 }));
    renderPage("business");
    fireEvent.click(await screen.findByRole("button", { name: /Restore from a past version/ }));
    const dialog = await screen.findByTestId("settings.organization.restore-dialog");
    expect(await within(dialog).findByText("Ledger")).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore version 28" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert.textContent).toContain("The profile changed since version 46");
    expect(within(alert).getByRole("button", { name: "Reload the versions" })).toBeTruthy();
  });

  it("opens the form on the draft from the platforms, names each source, and saves it as a draft version", async () => {
    post.mockImplementation(async (path: string) => {
      if (path !== "/organization-context/draft") throw new Error(`unexpected POST ${path}`);
      return {
        based_on_version: 3,
        profile: { ...CONTEXT.profile, pir_ids: ["pir-1"], software: [{ product: "CrowdStrike Falcon", vendor: "", cpe: "" }] },
        filled: [
          { field: "pir_ids", source: "opencti", detail: "1 PIR(s)" },
          { field: "software", source: "openaev", detail: "1 product(s)" },
        ],
        unavailable: ["OpenCRQ: no OpenCRQ integration is connected (tenant id 42)"],
        missing_fields: ["size", "loaded_hourly_rate"],
      };
    });
    put.mockResolvedValue({ ...CONTEXT, version: 4, source: "draft" });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
    await draftFromPlatforms();
    const panel = await screen.findByTestId("settings.organization.draft");
    expect(panel.textContent).toContain("Priority intelligence requirements (PIRs) from OpenCTI");
    expect(panel.textContent).toContain("Software from OpenAEV");
    expect(panel.textContent).toContain("OpenCRQ could not be read");
    // The backend's error text is never shown as is.
    expect(panel.textContent).not.toContain("tenant id");
    // The drafted software inventory is named in the panel and saved with the profile, its card living on CTEM > Inventory (#1178).
    expect(put).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Save/ }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const [, body] = put.mock.calls[0] as [string, { profile: { pir_ids: string[] }; source: string }];
    expect(body.source).toBe("draft");
    expect(body.profile.pir_ids).toEqual(["pir-1"]);
    await waitFor(() => expect(screen.queryByTestId("settings.organization.draft")).toBeNull());
  });

  it("says so when the platforms had nothing to add, and Cancel drops the draft unsaved", async () => {
    post.mockResolvedValue({ based_on_version: 3, profile: CONTEXT.profile, filled: [], unavailable: [], missing_fields: CONTEXT.missing_fields });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
    await draftFromPlatforms();
    expect(await screen.findByText("The platforms had nothing to add to the empty fields.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Cancel/ }));
    await waitFor(() => expect(screen.queryByTestId("settings.organization.draft")).toBeNull());
    expect(put).not.toHaveBeenCalled();
  });

  it("keeps Edit disabled while a draft is pending, so the draft never replaces what was typed", async () => {
    let resolveDraft: (value: unknown) => void = () => {};
    post.mockImplementation(() => new Promise((resolve) => (resolveDraft = resolve)));
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
    const edit = screen.getByRole("button", { name: /^Edit$/ }) as HTMLButtonElement;
    expect(edit.disabled).toBe(false);
    await draftFromPlatforms();
    await waitFor(() => expect((screen.getByRole("button", { name: /^Edit$/ }) as HTMLButtonElement).disabled).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    // The click did not open the form: Save appears only once the draft resolves.
    expect(screen.queryByRole("button", { name: /Save/ })).toBeNull();
    resolveDraft({ based_on_version: 3, profile: CONTEXT.profile, filled: [], unavailable: [], missing_fields: CONTEXT.missing_fields });
    expect(await screen.findByTestId("settings.organization.draft")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Save/ })).toBeTruthy();
  });

  it("opens in edit mode with the Draft menu and the setup guide when the first-login setup sends ?setup=1 (#9173 S3)", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/settings/organization?setup=1"]}>
        <OrganizationSection />
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("settings.organization.setup-guide")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Draft" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.getByRole("button", { name: /Save/ })).toBeTruthy();
    // Nothing is saved before the form's Save.
    expect(post).not.toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
    // Cancel leaves the setup: the page is back in read mode and the guide is gone.
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByTestId("settings.organization.setup-guide")).toBeNull());
    expect(screen.getByRole("button", { name: /^Edit$/ })).toBeTruthy();
  });

  it("a link to one card (?card=) focuses the card on CTEM > Inventory > Business (CTEM calls to action, #1690)", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/ctem/inventory/business?card=primary_assets"]}>
        <OrganizationSection embed={{ cards: INVENTORY_CARDS.business }} />
      </MemoryRouter>,
    );
    await waitFor(() => expect((document.activeElement as HTMLElement | null)?.id ?? "").toMatch(/-primary_assets$/));
  });

  it("lands ?card=tracked_organizations on the card root, never on a wrapper (UX r1 B2 on #108)", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/ctem/inventory/business?card=tracked_organizations"]}>
        <OrganizationSection embed={{ cards: INVENTORY_CARDS.business }} />
      </MemoryRouter>,
    );
    await waitFor(() => expect((document.activeElement as HTMLElement | null)?.id ?? "").toMatch(/-tracked_organizations$/));
    // The focused element is the card itself, so the focus ring keeps the card's radius.
    expect(document.activeElement?.getAttribute("data-testid")).toBe("settings.organizations");
    expect(document.activeElement?.getAttribute("data-anchor")).toBe("card");
  });

  it("lands ?card=outside_lens on the outside lens card root (UX r1 B1 on #1159)", async () => {
    // The card lives on CTEM > Inventory (#1178), which mounts the Exposure cards.
    renderWithProviders(
      <MemoryRouter initialEntries={["/ctem/inventory?card=outside_lens"]}>
        <OrganizationSection embed={{ cards: INVENTORY_CARDS.exposure }} />
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("settings.organization.panel-exposure")).toBeTruthy();
    await waitFor(() => expect((document.activeElement as HTMLElement | null)?.id ?? "").toMatch(/-outside_lens$/));
    expect(document.activeElement?.getAttribute("data-testid")).toBe("settings.organization.outside-lens");
    expect(document.activeElement?.getAttribute("data-anchor")).toBe("card");
    // Like its siblings it never folds on CTEM > Inventory (#1690).
    const card = screen.getByTestId("settings.organization.outside-lens");
    expect(within(card).queryAllByRole("button").some((button) => button.hasAttribute("aria-expanded"))).toBe(false);
  });

  it("marks the Draft button aria-busy while the draft is pending (#9102 A2)", async () => {
    let resolveDraft: (value: unknown) => void = () => {};
    post.mockImplementation(() => new Promise((resolve) => (resolveDraft = resolve)));
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
    const draftButton = () => screen.getByRole("button", { name: "Draft" });
    expect(draftButton().getAttribute("aria-busy")).toBeNull();
    await draftFromPlatforms();
    await waitFor(() => expect(draftButton().getAttribute("aria-busy")).toBe("true"));
    resolveDraft({ based_on_version: 3, profile: CONTEXT.profile, filled: [], unavailable: [], missing_fields: CONTEXT.missing_fields });
    expect(await screen.findByTestId("settings.organization.draft")).toBeTruthy();
  });

  it("never puts the backend's text in the toast when the draft fails (#9102 A1)", async () => {
    post.mockRejectedValue(new Error("upstream 502 from tenant id 42"));
    // The app MutationCache raises the global error toast: mount it, so a second toast would show here.
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>, { client: testQueryClient({ appMutationCache: true }) });
    await screen.findByTestId("settings.organization.identity");
    await draftFromPlatforms();
    expect(await screen.findAllByText("The draft could not be made")).toHaveLength(1);
    expect(screen.getAllByText("Try again in a moment.")).toHaveLength(1);
    expect(screen.queryByText(/tenant id/)).toBeNull();
  });

  it("names the source of the saved version in the header (#9102 A3)", async () => {
    get.mockImplementation(async (path: string) => {
      if (path === "/organization-context") return { ...CONTEXT, source: "draft" };
      if (path === "/integrations") return [];
      throw new Error(`unexpected GET ${path}`);
    });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    expect(await screen.findByText(/Version 3, saved .* from a draft of the platforms/)).toBeTruthy();
  });

  describe("Draft with AI (#9484, S-b)", () => {
    const AI_DRAFT = {
      based_on_version: 3,
      proposals: [
        { field: "size", value: "1001-5000", source: "description", confidence: "high", basis: "4,000 people", check: false, current: "", conflict: false },
        { field: "sector", value: "Insurance", source: "description", confidence: "high", basis: "insurer", check: false, current: "Finance", conflict: true },
        { field: "loaded_hourly_rate", value: 90, source: "description", confidence: "medium", basis: "", check: true, current: null, conflict: false },
        { field: "regions", value: ["FR"], source: "description", confidence: "high", basis: "France", check: false, current: ["FR"], conflict: false },
      ],
      not_supported: ["pir_ids"],
      suggested_pirs: [],
      unavailable: [],
      notes: [],
      usage: {},
    };

    it("calls no model before the Draft click", async () => {
      renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
      await screen.findByTestId("settings.organization.identity");
      await pickDraft("With AI");
      expect(await screen.findByRole("dialog", { name: "Draft with AI" })).toBeTruthy();
      expect(postForm).not.toHaveBeenCalled();
      expect(post).not.toHaveBeenCalled();
    });

    it("Accept all skips conflicts and Check this, and saves only the accepted rows as ai_draft", async () => {
      postForm.mockResolvedValue(AI_DRAFT);
      put.mockResolvedValue({ ...CONTEXT, version: 4, source: "ai_draft" });
      renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
      await screen.findByTestId("settings.organization.identity");
      await pickDraft("With AI");
      fireEvent.change(await screen.findByLabelText(/^Description/), { target: { value: "insurer, 4,000 people, France" } });
      fireEvent.click(screen.getByRole("button", { name: "Draft", hidden: false }));
      await screen.findByTestId("ai-draft.review");
      expect(postForm).toHaveBeenCalledTimes(1);
      // The unchanged proposal is collapsed, never a row to accept; an unsupported field is named.
      expect(screen.queryByTestId("ai-draft.row.regions")).toBeNull();
      expect(screen.getByText(/Not supported:/).textContent).toContain("Priority intelligence requirements");
      fireEvent.click(screen.getByRole("button", { name: "Accept all" }));
      expect(screen.getByRole("status").textContent).toBe("Accepted 1, skipped 2 to review");
      expect(put).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Save accepted (1)" }));
      await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
      const [, body] = put.mock.calls[0] as [string, { profile: { size: string; sector: string }; source: string; sources: Record<string, unknown> }];
      expect(body.source).toBe("ai_draft");
      expect(body.profile.size).toBe("1001-5000");
      // The typed sector is not replaced without its own accept.
      expect(body.profile.sector).toBe("Finance");
      expect(Object.keys(body.sources)).toEqual(["size"]);
    });

    it("shows amounts with their unit and labels the row editor with the field name", async () => {
      postForm.mockResolvedValue(AI_DRAFT);
      renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
      await screen.findByTestId("settings.organization.identity");
      await pickDraft("With AI");
      fireEvent.change(await screen.findByLabelText(/^Description/), { target: { value: "insurer" } });
      fireEvent.click(screen.getByRole("button", { name: "Draft", hidden: false }));
      const row = await screen.findByTestId("ai-draft.row.loaded_hourly_rate");
      expect(row.textContent).toMatch(/Proposed: .*90.* per hour/);
      fireEvent.click(within(row).getByRole("button", { name: /^Edit$/ }));
      const editor = within(row).getByRole("textbox", { name: "Loaded hourly rate" });
      expect(editor.getAttribute("aria-labelledby")).toBe("ai-draft-edit-loaded_hourly_rate-label");
    });

    it("shows the licence call to action instead of the dialog when agentic AI is not available", async () => {
      agenticEnabled = false;
      renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
      await screen.findByTestId("settings.organization.identity");
      await pickDraft("With AI");
      expect(screen.queryByRole("dialog", { name: "Draft with AI" })).toBeNull();
      expect(postForm).not.toHaveBeenCalled();
    });

    it("hides the summary cards in edit mode so the form starts at the top", async () => {
      renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
      expect(await screen.findByTestId("settings.organization.overview")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
      await waitFor(() => expect(screen.queryByTestId("settings.organization.overview")).toBeNull());
    });
  });
});
