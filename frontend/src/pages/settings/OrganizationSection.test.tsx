// Settings > Organization (#8987): the profile every agent reads, its missing fields, the save and the OpenCRQ check.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

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
}));

// The tab bar registers palette commands (DetailTabs); the palette is not under test here.
vi.mock("@/lib/commands/CommandsProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/commands/CommandsProvider")>()),
  useCommandSections: () => {},
}));

let agenticEnabled = true;

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
/** The cards CTEM > Inventory mounts in place of the two tabs that left Settings (#1178). */
const INVENTORY_CARDS = {
  technology: ["tech_stack"],
  exposure: ["assets_exposure", "asset_groups", "prioritization_policy", "outside_lens", "evidence_health"],
} as const;
/**
 * Renders the page on one of its tabs (#12329); none is the first tab. The Technology and Exposure cards render
 * as CTEM > Inventory mounts them (#1178), since Settings no longer draws them.
 */
function renderPage(tab?: string) {
  const embedded = tab === "technology" || tab === "exposure" ? INVENTORY_CARDS[tab] : null;
  return renderWithProviders(
    <MemoryRouter initialEntries={[embedded ? "/ctem/inventory" : tab ? `/settings/organization/profile/${tab}` : "/settings/organization"]}>
      <OrganizationSection embed={embedded ? { cards: embedded } : undefined} />
    </MemoryRouter>,
  );
}
/** Opens a tab as a pointer does: each tab is a link to its path (UX r1 B1 on #12332). */
function openTab(name: RegExp) {
  fireEvent.click(screen.getByRole("tab", { name }), { button: 0 });
}

const get = vi.mocked(api.get);
const put = vi.mocked(api.put);
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
    crown_jewels: [{ name: "Payments", business_service: "Cards", owner: "CISO", criticality: "critical", opencrq_asset_id: "" }],
    tech_stack: [],
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
    if (path === "/organization-context/readers") return READERS;
    if (path === "/organization-context/import/sources") return { sections: [] };
    if (path === "/organization-context/attack-surface/providers") return { providers: ["shodan"] };
    // The Sources and syncs table of the Knowledge and sources tab (#1276).
    if (path.startsWith("/organization-context/syncs") || path.startsWith("/organization-context/sources")) return [];
    throw new Error(`unexpected GET ${path}`);
  });
});

const READERS = {
  readers: {
    sector: [{ fleet_id: "f-1", name: "Threat response" }],
    regions: [{ fleet_id: "f-1", name: "Threat response" }],
    crown_jewels: [
      { fleet_id: "f-1", name: "Threat response" },
      { fleet_id: "f-2", name: "Vulnerability prioritization" },
    ],
  },
  agents: {
    risk_appetite: [{ agent_id: "a-1", name: "Risk Quantifier" }],
    currency: [{ agent_id: "a-1", name: "Risk Quantifier" }],
    pir_ids: [{ agent_id: "a-2", name: "Threat Relevance Analyst" }],
  },
};

describe("OrganizationSection crown jewel widths at 1080 (#1174 UX r2 B1 and B2)", () => {
  // The crown jewels table measures 810 px in its 72rem card at 1080; Assets and Source step aside there.
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

describe("OrganizationSection used by (#9173 S2)", () => {
  it("names the fleets reading each part, once per card, linked to the fleet page, and only in read mode", async () => {
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    // The first tab holds three of the cards that name their readers (#12329).
    await waitFor(() => expect(screen.getAllByTestId("settings.organization.used-by")).toHaveLength(3));
    const [identity, risk, intelligence] = screen.getAllByTestId("settings.organization.used-by");
    // The seeded agents reading the risk appetite and the PIRs (#10063 r1 A3), once each, linked to the agent page.
    expect(risk.dataset.fields).toBe("risk_appetite currency loaded_hourly_rate");
    expect(within(risk).queryByTestId("settings.organization.used-by.fleets")).toBeNull();
    expect(within(risk).getAllByRole("link").map((a) => a.getAttribute("href"))).toEqual(["/agents/a-1"]);
    expect(within(intelligence).getAllByRole("link").map((a) => a.textContent)).toEqual(["Threat Relevance Analyst"]);
    expect(identity.dataset.fields).toBe("sector regions");
    expect(within(identity).getAllByRole("link").map((a) => a.textContent)).toEqual(["Threat response"]);
    openTab(/^Business/);
    await waitFor(() => expect(screen.getAllByTestId("settings.organization.used-by")).toHaveLength(1));
    const [jewels] = screen.getAllByTestId("settings.organization.used-by");
    expect(within(jewels).getAllByRole("link").map((a) => a.getAttribute("href"))).toEqual(["/fleets/f-1", "/fleets/f-2"]);
    openTab(/^Identity and risk/);
    await waitFor(() => expect(screen.getAllByTestId("settings.organization.used-by")).toHaveLength(3));
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    await waitFor(() => expect(screen.queryByTestId("settings.organization.used-by")).toBeNull());
  });

  it("lays every line on one shared label grid, so the chips start at the same column on every card (#10166 UX r1 B1)", async () => {
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await waitFor(() => expect(screen.getAllByTestId("settings.organization.used-by")).toHaveLength(3));
    for (const block of screen.getAllByTestId("settings.organization.used-by")) {
      expect(block.dataset.layout).toBe("label-grid");
      expect(block.dataset.align).toBe("start");
      for (const row of block.querySelectorAll<HTMLElement>("[data-row]")) {
        // Each line is a display-contents row: its label and its chip list are direct cells of the block's grid.
        expect(row.dataset.row).toBe("contents");
        expect(Array.from(row.children).map((cell) => (cell as HTMLElement).dataset.cell)).toEqual(["label", "chips"]);
        // Both labels sit in every label cell (same column width on every card); only the row's own one is exposed.
        const label = row.querySelector<HTMLElement>("[data-cell='label']")!;
        expect(Array.from(label.querySelectorAll<HTMLElement>("[data-label]")).map((el) => el.dataset.label)).toEqual(["fleets", "agents"]);
        expect(label.querySelectorAll("[data-label]:not([aria-hidden='true'])")).toHaveLength(1);
        // #10166 UX r2 B1: each label is laid out in the chip's box and the grid aligns cells at the start,
        // so the label text centres on the first chip row.
        for (const el of label.querySelectorAll<HTMLElement>("[data-label]")) expect(el.dataset.box).toBe("chip-row");
        expect(row.querySelector<HTMLElement>("[data-cell='chips'] [data-slot='badge']")!.dataset.box).toBe("chip-row");
      }
    }
  });
});

describe("OrganizationSection identity header and threshold previews (#9173 S2)", () => {
  const withProfile = (profile: Partial<OrganizationContextResponse["profile"]>) =>
    get.mockImplementation(async (path: string) => {
      if (path === "/organization-context") return { ...CONTEXT, profile: { ...CONTEXT.profile, ...profile } };
      if (path === "/organizations" || path === "/integrations") return [];
      if (path === "/organization-context/pirs") return { pirs: [], unavailable: null };
      if (path === "/organization-context/readers") return READERS;
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
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    const tile = await screen.findByTestId("settings.organization.read.annual_loss_threshold");
    expect(tile.textContent).toContain("250,000 per year, currency not set");
    expect(screen.getByTestId("settings.organization.read.approval_threshold").textContent).toContain("Not set");
  });

  it("says what each saved threshold does, in the profile currency, in read mode only", async () => {
    withProfile({ risk_appetite: { annual_loss_threshold: 250000, approval_threshold: 1000000 } });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    expect(await screen.findByText(/Scenarios above .*250,000.* per year are ranked first/)).toBeTruthy();
    expect(screen.getByText(/Scenarios above .*1,000,000.* per year are flagged for approval/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    await waitFor(() => expect(screen.queryByText(/per year are ranked first/)).toBeNull());
  });
});

describe("OrganizationSection tabs (#12329)", () => {
  it("renders the two Settings tabs, only the active panel, and opens the tab the URL names (#1178, #1367 point 7)", async () => {
    renderPage("business");
    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent?.replace(/\d+\/\d+.*$/, ""))).toEqual(["Identity and risk", "Business"]);
    expect(screen.getByRole("tab", { name: /^Business/ }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
    expect(screen.getByTestId("settings.organization.crown-jewels")).toBeTruthy();
    expect(screen.queryByTestId("settings.organization.identity")).toBeNull();
    expect(screen.queryByTestId("settings.organization.assets-exposure")).toBeNull();
    // One line points to CTEM > Inventory, where the technology and exposure cards live.
    expect(screen.getByTestId("settings.organization.inventory-pointer").textContent).toContain("CTEM > Inventory");
  });

  it("mounts only the cards an embedding page names, with no header, tab bar or Sources panel (#1178)", async () => {
    renderPage("exposure");
    expect(await screen.findByTestId("settings.organization.assets-exposure")).toBeTruthy();
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
    expect(screen.queryByTestId("settings.organization.inventory-pointer")).toBeNull();
    expect(screen.queryByTestId("settings.organization.tech-stack")).toBeNull();
    expect(screen.queryByTestId("settings.organization.crown-jewels")).toBeNull();
  });

  it("draws each tab as a link to its path, the active one the current page (UX r1 B1 on #12332)", async () => {
    renderPage("business");
    const identity = await screen.findByRole("tab", { name: /^Identity and risk/ });
    expect(identity.tagName).toBe("A");
    expect(identity.getAttribute("href")).toBe("/settings/organization");
    // Knowledge and sources is a button of the header opening its own page (#1367, point 7), never a tab.
    expect(screen.queryByRole("tab", { name: /^Knowledge and sources/ })).toBeNull();
    expect(screen.getByTestId("settings.organization.knowledge").textContent).toContain("Knowledge and sources");
    const business = screen.getByRole("tab", { name: /^Business/ });
    expect(business.getAttribute("aria-current")).toBe("page");
    expect(identity.getAttribute("aria-current")).toBeNull();
  });

  it("falls back to the first tab on an unknown tab in the URL", async () => {
    renderPage("nope");
    expect(await screen.findByTestId("settings.organization.identity")).toBeTruthy();
    expect(screen.getByRole("tab", { name: /^Identity and risk/ }).getAttribute("aria-selected")).toBe("true");
  });

  it("keeps the whole-profile edit across tabs and marks the tab holding an unsaved change", async () => {
    put.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderPage();
    await screen.findByTestId("settings.organization.identity");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Loaded hourly rate" }), { target: { value: "120" } });
    expect(screen.getByTestId("settings.organization.tab-identity.unsaved")).toBeTruthy();
    openTab(/^Business/);
    expect(await screen.findByTestId("settings.save-bar")).toBeTruthy();
    openTab(/^Identity and risk/);
    expect(((await screen.findByRole("textbox", { name: "Loaded hourly rate" })) as HTMLInputElement).value).toBe("120");
    fireEvent.click(within(screen.getByTestId("settings.save-bar")).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
  });

  it("lists the missing fields on their own tab only, and a refused Save opens the tab of the bad field", async () => {
    renderPage("business");
    await screen.findByTestId("settings.organization.crown-jewels");
    expect(screen.queryByTestId("settings.organization.missing")).toBeNull();
    openTab(/^Identity and risk/);
    expect((await screen.findByTestId("settings.organization.missing")).textContent).toContain("Loaded hourly rate");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Loaded hourly rate" }), { target: { value: "abc" } });
    openTab(/^Business/);
    fireEvent.click(within(await screen.findByTestId("settings.save-bar")).getByRole("button", { name: "Save" }));
    const rate = await screen.findByRole("textbox", { name: "Loaded hourly rate" });
    await waitFor(() => expect(document.activeElement).toBe(rate));
    expect(screen.getByRole("tab", { name: /^Identity and risk/ }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByTestId("settings.organization.tab-identity.error")).toBeTruthy();
    expect(put).not.toHaveBeenCalled();
  });
});

describe("OrganizationSection", () => {
  it("names the fields not filled, as the agents report them", async () => {
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    expect((await screen.findByTestId("settings.organization.identity")).textContent).toContain("Acme");
    const missing = await screen.findByTestId("settings.organization.missing");
    expect(missing.textContent).toContain("Priority intelligence requirements (PIRs)");
    expect(missing.textContent).toContain("Loaded hourly rate");
    openTab(/^Business/);
    expect(await screen.findByText("Payments")).toBeTruthy();
  });

  it("shows the completeness per tab and opens the form on a missing field (#9173 S1c, #12329)", async () => {
    renderPage("business");
    const overview = await screen.findByTestId("settings.organization.overview");
    expect(overview.textContent).toContain("3 fields not filled");
    // Identity and risk: 10 contract fields, 3 of them missing; the Technology tab left for CTEM > Inventory (#1178).
    expect(screen.getByTestId("settings.organization.tab-identity.completeness").textContent).toBe("7/10");
    expect(screen.queryByTestId("settings.organization.tab-technology.completeness")).toBeNull();
    expect(screen.queryByTestId("settings.save-bar")).toBeNull();
    // The missing fields sit on their own tab: the jump starts there.
    openTab(/^Identity and risk/);
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

  it("lists the crown jewels in a table with column headers", async () => {
    renderPage("business");
    await screen.findByText("Payments");
    expect(screen.getByRole("columnheader", { name: "Business service" })).toBeTruthy();
    expect(screen.getAllByTestId("settings.organization.crown-jewel-row")).toHaveLength(1);
  });

  it("folds a list card to its header and count, and unfolds it (#12256 slice 2)", async () => {
    renderPage("business");
    await screen.findByText("Payments");
    const card = screen.getByTestId("settings.organization.crown-jewels");
    expect(card.dataset.collapsed).toBe("false");
    // The count reads in the header, next to the title.
    expect(within(card).getByTestId("settings.card-count").textContent).toBe("1");
    const toggle = within(card).getByTestId("settings.card-collapse");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(card.dataset.collapsed).toBe("true");
    expect(within(card).queryAllByTestId("settings.organization.crown-jewel-row")).toHaveLength(0);
    // The anchor nav is gone with the tabs (#12329).
    expect(screen.queryByRole("navigation", { name: "On this page" })).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: "Expand" }));
    await waitFor(() => expect(card.dataset.collapsed).toBe("false"));
    expect(within(card).getAllByTestId("settings.organization.crown-jewel-row")).toHaveLength(1);
  });

  it("reads the row tables as text and edits them as fields; an empty value reads Not set, never a placeholder (UX r1 B1, B2 on #10804)", async () => {
    const base = get.getMockImplementation()!;
    get.mockImplementation(async (path: string) =>
      path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, tech_stack: [{ product: "CrowdStrike Falcon", vendor: "CrowdStrike", cpe: "" }] } } : base(path),
    );
    renderPage("business");
    const row = await screen.findByTestId("settings.organization.crown-jewel-row");
    expect(within(row).getByText("Payments")).toBeTruthy();
    expect(within(row).getByText(/^Critical/)).toBeTruthy();
    expect(within(row).queryByRole("textbox")).toBeNull();
    expect(within(row).queryByRole("combobox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    // In edit a row still reads as text until opened (#12256): one row of fields at a time.
    const jewel = await screen.findByTestId("settings.organization.crown-jewel-row");
    await waitFor(() => expect(within(jewel).queryByRole("textbox")).toBeNull());
    fireEvent.click(within(screen.getByTestId("settings.organization.crown-jewel-row")).getByText("Payments"));
    expect(await screen.findByDisplayValue("Payments")).toBeTruthy();
    expect(screen.getAllByRole("combobox", { name: "Criticality" })).toHaveLength(1);
  });

  it("reads the tech stack rows as text on CTEM > Inventory and edits a row as fields (#1178)", async () => {
    const base = get.getMockImplementation()!;
    get.mockImplementation(async (path: string) =>
      path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, tech_stack: [{ product: "CrowdStrike Falcon", vendor: "CrowdStrike", cpe: "" }] } } : base(path),
    );
    renderPage("technology");
    const tech = (await screen.findByText("CrowdStrike Falcon")).closest("tr")!;
    // CPE and Version (CTEM-11, #928) are both empty: each reads Not set.
    expect(within(tech).getAllByText("Not set")).toHaveLength(2);
    expect(within(tech).queryByRole("textbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Edit Tech stack/ }));
    fireEvent.click(await screen.findByText("CrowdStrike Falcon"));
    expect(await screen.findByRole("textbox", { name: "CPE (optional)" })).toBeTruthy();
  });

  it("bounds the columns so a long name is cut inside the card, in read mode and while editing (UX r2 B2 on #10804, UX r1 B2 on #12261)", async () => {
    const long = "Customer identity and payments platform for every retail and corporate banking region";
    const base = get.getMockImplementation()!;
    get.mockImplementation(async (path: string) =>
      path === "/organization-context"
        ? { ...CONTEXT, profile: { ...CONTEXT.profile, crown_jewels: [{ name: long, business_service: "", owner: "", criticality: "critical", opencrq_asset_id: "" }] } }
        : base(path),
    );
    renderPage("business");
    const row = await screen.findByTestId("settings.organization.crown-jewel-row");
    const table = row.closest("table")!;
    expect(table.dataset.layout).toBe("fixed");
    expect(within(table).getByRole("columnheader", { name: /Crown jewel/ }).style.width).toBe("240px");
    expect(within(table).getByRole("columnheader", { name: /Criticality/ }).style.width).toBe("120px");
    expect(within(table).getByRole("columnheader", { name: /OpenCRQ/ }).style.width).toBe("112px");
    expect(within(row).getByText(long)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    // The editor keeps the read widths, so the Actions column never leaves the card (#12261 UX r1 B2; was: auto).
    await screen.findAllByTestId("settings.organization.list-editor");
    const editTable = screen.getByTestId("settings.organization.crown-jewel-row").closest("table")!;
    expect(editTable.dataset.layout).toBe("fixed");
    expect(within(editTable).getByRole("columnheader", { name: /Crown jewel/ }).style.width).toBe("240px");
    fireEvent.click(within(screen.getByTestId("settings.organization.crown-jewel-row")).getByText(long));
    await screen.findByDisplayValue(long);
  });

  it("edits one list card alone, counts its changes, undoes a removal and saves the profile with that list changed (#12256)", async () => {
    const base = get.getMockImplementation()!;
    const tech = Array.from({ length: 150 }, (_, i) => ({ product: `Product ${String(i).padStart(3, "0")}`, vendor: i === 7 ? "" : "Vendor", cpe: "" }));
    get.mockImplementation(async (path: string) => (path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, tech_stack: tech } } : base(path)));
    put.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderPage("technology");
    // Read mode names the row that needs attention on the card header.
    expect((await screen.findByTestId("settings.organization.attention-tech_stack")).textContent).toBe("1 needs attention");
    fireEvent.click(screen.getByRole("button", { name: /Edit Tech stack/ }));
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
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const [, body] = put.mock.calls[0] as [string, { profile: { tech_stack: { product: string }[]; crown_jewels: unknown[] }; source: string }];
    expect(body.source).toBe("form");
    expect(body.profile.tech_stack).toHaveLength(151);
    expect(body.profile.tech_stack[150].product).toBe("Jira");
    expect(body.profile.crown_jewels).toEqual(CONTEXT.profile.crown_jewels);
  });

  it("edits one exposure card alone, keeps the others in read mode and saves the profile with that card changed (#12256 slice 2)", async () => {
    const base = get.getMockImplementation()!;
    const groups = [{ name: "DMZ", description: "Edge", internet_facing: "yes" }];
    get.mockImplementation(async (path: string) => (path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, asset_groups: groups } } : base(path)));
    put.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderPage("exposure");
    fireEvent.click(await screen.findByRole("button", { name: /Edit Asset groups/ }));
    const card = screen.getByTestId("settings.organization.asset-groups");
    const bar = await within(card).findByTestId("settings.save-bar");
    // The other cards wait for this one; the form is not opened.
    expect((screen.getByRole("button", { name: /Edit Assets and exposure/ }) as HTMLButtonElement).disabled).toBe(true);
    const save = within(bar).getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(within(card).getByDisplayValue("Edge"), { target: { value: "Edge network" } });
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const [, body] = put.mock.calls[0] as [string, { profile: { asset_groups: { description: string }[]; tech_stack: unknown[] }; source: string }];
    expect(body.source).toBe("form");
    expect(body.profile.asset_groups[0].description).toBe("Edge network");
    expect(body.profile.tech_stack).toEqual(CONTEXT.profile.tech_stack);
  });

  it("drops an exposure card's edit on Cancel, nothing sent (#12256 slice 2)", async () => {
    renderPage("knowledge");
    fireEvent.click(await screen.findByRole("button", { name: /Edit Enterprise knowledge/ }));
    const card = screen.getByTestId("settings.organization.enterprise-knowledge");
    fireEvent.click(within(card).getByRole("button", { name: "Link a knowledge base" }));
    fireEvent.click(within(within(card).getByTestId("settings.save-bar")).getByRole("button", { name: "Cancel" }));
    expect(within(card).queryByTestId("settings.save-bar")).toBeNull();
    // The Knowledge and sources page has no profile Edit of its own (UX round 1 on #1476, B1).
    expect(screen.queryByTestId("settings.organization.edit")).toBeNull();
    expect(put).not.toHaveBeenCalled();
    // The focus goes back to the card's Edit, never to the page body (#12271 UX r1 B3).
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: /Edit Enterprise knowledge/ })));
  });

  it("returns the focus to a list card's Edit on Cancel (#12271 UX r1 B3)", async () => {
    renderPage("business");
    fireEvent.click(await screen.findByRole("button", { name: /Edit Crown jewels/ }));
    const bar = await screen.findByTestId("settings.organization.list-save-bar");
    fireEvent.click(within(bar).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: /Edit Crown jewels/ })));
  });

  it("pages an exposure card in edit at 25 rows with its search, and pins an added row first (#12271 UX r1 B2)", async () => {
    const base = get.getMockImplementation()!;
    const asset = (i: number) => ({
      name: `asset-${String(i).padStart(3, "0")}`,
      category: "generic_asset",
      subcategory: "",
      criticality: "unknown",
      environment: "",
      internet_facing: "unknown",
      owner: "",
      group: "",
      tech_stack: [],
      tags: [],
      source: "manual",
      external_id: "",
    });
    const assets = Array.from({ length: 60 }, (_, i) => asset(i));
    get.mockImplementation(async (path: string) => (path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, assets_exposure: assets } } : base(path)));
    renderPage("exposure");
    fireEvent.click(await screen.findByRole("button", { name: /Edit Assets and exposure/ }));
    const card = screen.getByTestId("settings.organization.assets-exposure");
    await within(card).findByTestId("settings.save-bar");
    // 25 rows a page, never the 60.
    expect(within(card).getAllByTestId("settings.organization.asset-row")).toHaveLength(25);
    // The search stays while editing.
    fireEvent.change(within(card).getByRole("searchbox"), { target: { value: "asset-059" } });
    await waitFor(() => expect(within(card).getAllByTestId("settings.organization.asset-row")).toHaveLength(1));
    // An added row clears the search and is first on the first page.
    fireEvent.click(within(card).getByRole("button", { name: "Add an asset" }));
    await waitFor(() => expect(within(card).getAllByTestId("settings.organization.asset-row")).toHaveLength(25));
    expect((within(within(card).getAllByTestId("settings.organization.asset-row")[0]).getByRole("textbox", { name: "Asset" }) as HTMLInputElement).value).toBe("");
  });

  it("pastes a list into a card in edit, skips the rows already there, sets a vendor on the selected rows and saves (#12256 slice 2)", async () => {
    const base = get.getMockImplementation()!;
    const tech = [{ product: "Jira", vendor: "Atlassian", cpe: "" }, { product: "Slack", vendor: "", cpe: "" }];
    get.mockImplementation(async (path: string) => (path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, tech_stack: tech } } : base(path)));
    put.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderPage("technology");
    // Paste a list waits for the card's Edit.
    expect(screen.queryByTestId("settings.organization.paste-tech_stack")).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: /Edit Tech stack/ }));
    const bar = await screen.findByTestId("settings.organization.list-save-bar");
    fireEvent.click(screen.getByTestId("settings.organization.paste-tech_stack"));
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
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const [, body] = put.mock.calls[0] as [string, { profile: { tech_stack: { product: string; vendor: string }[] } }];
    expect(body.profile.tech_stack).toEqual([
      { product: "Jira", vendor: "Atlassian", cpe: "" },
      { product: "Slack", vendor: "Acme", cpe: "" },
      { product: "Notion", vendor: "Acme", cpe: "" },
    ]);
  });

  it("reads and pastes the version the organization runs on a tech stack row, and saves it only when set (CTEM-11, #928)", async () => {
    const base = get.getMockImplementation()!;
    const tech = [{ product: "Jira", vendor: "Atlassian", cpe: "", version: "9.12.2" }];
    get.mockImplementation(async (path: string) => (path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, tech_stack: tech } } : base(path)));
    put.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderPage("technology");
    const row = (await screen.findByText("Jira")).closest("tr")!;
    expect(within(row).getByText("9.12.2")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: /Edit Tech stack/ }));
    const bar = await screen.findByTestId("settings.organization.list-save-bar");
    fireEvent.click(screen.getByTestId("settings.organization.paste-tech_stack"));
    const dialog = await screen.findByTestId("settings.organization.paste-dialog");
    fireEvent.change(within(dialog).getByTestId("settings.organization.paste-text"), { target: { value: "nginx, F5, , 1.25.3\nSlack, Salesforce" } });
    fireEvent.click(within(dialog).getByTestId("settings.organization.paste-add"));
    await waitFor(() => expect(screen.queryByTestId("settings.organization.paste-dialog")).toBeNull());
    fireEvent.click(within(bar).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const [, body] = put.mock.calls[0] as [string, { profile: { tech_stack: Record<string, string>[] } }];
    expect(body.profile.tech_stack).toEqual([
      { product: "Jira", vendor: "Atlassian", cpe: "", version: "9.12.2" },
      { product: "nginx", vendor: "F5", cpe: "", version: "1.25.3" },
      // No version typed: none saved, so a profile that never declares one keeps its shape.
      { product: "Slack", vendor: "Salesforce", cpe: "" },
    ]);
  });

  it("keeps a card's unsaved rows: page Edit and the field jumps wait, and Next still pages with an added row open (#12261 UX r1 B1, B3)", async () => {
    const base = get.getMockImplementation()!;
    const tech = Array.from({ length: 60 }, (_, i) => ({ product: `Product ${String(i).padStart(3, "0")}`, vendor: "Vendor", cpe: "" }));
    get.mockImplementation(async (path: string) => (path === "/organization-context" ? { ...CONTEXT, profile: { ...CONTEXT.profile, tech_stack: tech } } : base(path)));
    renderPage("technology");
    fireEvent.click(await screen.findByRole("button", { name: /Edit Tech stack/ }));
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
        ? { ...CONTEXT, profile: { ...CONTEXT.profile, tech_stack: [{ product: "Concord", vendor: "", cpe: "" }, { product: "Jira", vendor: "Atlassian", cpe: "" }] } }
        : base(path),
    );
    renderPage("technology");
    fireEvent.click(await screen.findByRole("button", { name: /Edit Tech stack/ }));
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

  it("keeps search, sort and the pager on the crown jewels while editing (#12256; was: none while editing)", async () => {
    renderPage("business");
    await screen.findByText("Payments");
    // The crown jewels card: its own search box in read mode, none while editing. The form's pickers
    // (ticketing, regions) render only while editing since #10855, so the count is read inside the card.
    const jewelsTable = () => screen.getByTestId("settings.organization.crown-jewel-row").closest("table")!;
    const searchNear = () => jewelsTable().parentElement?.parentElement?.querySelectorAll("input[placeholder='Search...']").length ?? 0;
    expect(searchNear()).toBeGreaterThan(0);
    expect(jewelsTable().querySelector("th[aria-sort]")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Edit Crown jewels/ }));
    await screen.findByTestId("settings.organization.list-save-bar");
    expect(searchNear()).toBeGreaterThan(0);
    expect(jewelsTable().querySelector("th[aria-sort]")).toBeTruthy();
    expect(screen.getAllByTestId("settings.organization.crown-jewel-row")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Remove" }).length).toBeGreaterThan(0);
  });

  it("saves the edited profile as a form version", async () => {
    put.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
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
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
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
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
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
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
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
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    expect(await screen.findByText("Currency of the amounts", { selector: "label" })).toBeTruthy();
  });

  it("shows no region or PIR picker at rest, and the pickers tied to their help line on Edit (UX round 2 B2, #10855)", async () => {
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
    // Read mode shows the regions and the PIRs as names, with no picker at all (#9615, #10855).
    expect(screen.queryByRole("combobox", { name: "Regions of operation" })).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Priority intelligence requirements (PIRs)" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    const regions = await screen.findByRole("combobox", { name: "Regions of operation" });
    expect(regions).not.toBeDisabled();
    expect(document.getElementById(regions.getAttribute("aria-describedby") ?? "")).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "Priority intelligence requirements (PIRs)" })).not.toBeDisabled();
  });

  const withPirs = (pirs: unknown, profile: Partial<OrganizationContextResponse["profile"]>) =>
    get.mockImplementation(async (path: string) => {
      if (path === "/organization-context") return { ...CONTEXT, profile: { ...CONTEXT.profile, ...profile } };
      if (path === "/organizations" || path === "/integrations") return [];
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
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    const link = await screen.findByRole("link", { name: "Ransomware on energy" });
    expect(link.getAttribute("href")).toBe("https://cti.example/dashboard/pirs/1ef07227-6f37");
    expect(screen.queryByText("1ef07227-6f37")).toBeNull();
    expect(screen.queryByPlaceholderText("Search a PIR")).toBeNull();
  });

  it("marks a PIR OpenCTI no longer has, with its last known name, and offers to remove it (#9615)", async () => {
    withPirs({ pirs: [], unavailable: null, opencti_url: "https://cti.example" }, { pir_ids: ["pir-gone"], pir_names: { "pir-gone": "Old campaign" } });
    put.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    // The state leads, so a chip cut at its width still says it; the chip carries the whole label as its title (UX round 1 B2).
    const chip = await screen.findByText("No longer in OpenCTI: Old campaign");
    expect(screen.getByTitle("No longer in OpenCTI: Old campaign")).toBeTruthy();
    expect(chip).toBeTruthy();
    expect(screen.queryByText("pir-gone")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    fireEvent.click(await screen.findByRole("button", { name: /Save/ }));
    await waitFor(() => expect(put).toHaveBeenCalled());
    const [, body] = put.mock.calls[0] as [string, { profile: { pir_ids: string[] } }];
    expect(body.profile.pir_ids).toEqual([]);
  });

  it("writes a missing PIR out whole under the edit picker, state first (UX round 1 B2)", async () => {
    withPirs({ pirs: [{ id: "pir-1", name: "Ransomware on energy" }], unavailable: null, opencti_url: "" }, {
      pir_ids: ["pir-1", "pir-gone"],
      pir_names: { "pir-gone": "Initial access brokers selling our domains" },
    });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByText("No longer in OpenCTI: Initial access brokers selling our domains");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    const list = await screen.findByRole("list", { name: "PIR no longer in OpenCTI" });
    expect(within(list).getByText("No longer in OpenCTI: Initial access brokers selling our domains")).toBeTruthy();
    expect(within(list).queryByText("Ransomware on energy")).toBeNull();
  });

  it("keeps the last known names and offers Retry when OpenCTI cannot be read (#9615)", async () => {
    withPirs({ pirs: [], unavailable: "unreadable", opencti_url: "https://cti.example" }, { pir_ids: ["pir-1", "pir-2"], pir_names: { "pir-1": "Ransomware on energy" } });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
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
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    expect(await screen.findByText("Loading the PIR names")).toBeTruthy();
    expect(screen.queryByText("PIR name not available")).toBeNull();
    // The outlined chip skeleton (UX #11595 R1 B1) is guarded on the source: tests_unit/organization-pirs-badge.test.ts.
    expect(screen.getAllByTestId("settings.organization.pir-chip-skeleton").length).toBe(2);
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    expect(await screen.findByTestId("settings.organization.pirs-loading")).toBeTruthy();
    expect(screen.queryByText("PIR name not available")).toBeNull();
  });

  it("offers Retry in edit mode too when OpenCTI cannot be read (#11589)", async () => {
    withPirs({ pirs: [], unavailable: "unreadable", opencti_url: "" }, { pir_ids: ["pir-1"], pir_names: { "pir-1": "Ransomware on energy" } });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByText("Ransomware on energy");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
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
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    expect(await screen.findByText("Retail threats (aaaa1111)")).toBeTruthy();
    expect(screen.getAllByText("Retail threats (aaaa1111)")).toHaveLength(1);
    expect(screen.getByText("Retail threats (bbbb2222)")).toBeTruthy();
  });

  it("shows eight PIRs, then +N more in place (#9615)", async () => {
    const ids = Array.from({ length: 11 }, (_, i) => `pir-${i}`);
    withPirs({ pirs: ids.map((id) => ({ id, name: `Name ${id}` })), unavailable: null, opencti_url: "" }, { pir_ids: ids });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    expect(await screen.findByText("Name pir-7")).toBeTruthy();
    expect(screen.queryByText("Name pir-8")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "+3 more" }));
    expect(await screen.findByText("Name pir-10")).toBeTruthy();
  });

  it("saves the names OpenCTI gives with the PIRs (#9615)", async () => {
    withPirs({ pirs: [{ id: "pir-1", name: "Ransomware on energy" }], unavailable: null }, { pir_ids: ["pir-1"] });
    put.mockResolvedValue({ ...CONTEXT, version: 4 });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    // No OpenCTI address answered: the name is shown, unlinked.
    expect(await screen.findByText("Ransomware on energy")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Ransomware on energy" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    fireEvent.change(screen.getByLabelText("Loaded hourly rate"), { target: { value: "120" } });
    fireEvent.click(await screen.findByRole("button", { name: /Save/ }));
    await waitFor(() => expect(put).toHaveBeenCalled());
    const [, body] = put.mock.calls[0] as [string, { profile: { pir_names: Record<string, string> } }];
    expect(body.profile.pir_names).toEqual({ "pir-1": "Ransomware on energy" });
  });

  it("says the PIRs could not be listed, never that OpenCTI has none, when OpenCTI did not answer (UX round 2 B4)", async () => {
    get.mockImplementation(async (path: string) => {
      if (path === "/organization-context") return CONTEXT;
      if (path === "/organizations" || path === "/integrations") return [];
      if (path === "/organization-context/pirs") return { pirs: [], unavailable: "unreadable" };
      throw new Error(`unexpected GET ${path}`);
    });
    renderWithProviders(<MemoryRouter><OrganizationSection /></MemoryRouter>);
    await screen.findByTestId("settings.organization.identity");
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    // Since #11589 an unreadable OpenCTI shows the Retry line in edit mode too, the helper stays the field's own.
    expect(await screen.findByTestId("settings.organization.pirs-unreadable")).toBeTruthy();
    fireEvent.click(screen.getByRole("combobox", { name: "Priority intelligence requirements (PIRs)" }));
    expect(await screen.findByText("The PIRs could not be listed: OpenCTI did not answer.")).toBeTruthy();
    expect(screen.queryByText("No PIR in OpenCTI")).toBeNull();
  });

  it("names the criticality in words in the OpenCRQ plan, never the raw value", async () => {
    post.mockResolvedValue({
      applied: false,
      plan: {
        linked: [],
        pushed_to_opencrq: [{ opencrq_asset_id: "a-1", name: "Payments", criticality: "critical" }],
        pulled_from_opencrq: [],
        ambiguous: [],
        not_in_opencrq: [],
      },
      errors: [],
      version: 3,
    });
    renderPage("business");
    fireEvent.click(await screen.findByRole("button", { name: /Check against OpenCRQ/ }));
    // English renders the message ids: the criticality-specific key of the shared map.
    expect(await screen.findByText("Set Payments to Critical (criticality) in OpenCRQ")).toBeTruthy();
  });

  it("names an OpenCRQ-only level (very_high) in words on the pushed line", async () => {
    post.mockResolvedValue({
      applied: false,
      plan: {
        linked: [],
        pushed_to_opencrq: [{ opencrq_asset_id: "a-3", name: "Ledger", criticality: "very_high" }],
        pulled_from_opencrq: [],
        ambiguous: [],
        not_in_opencrq: [],
      },
      errors: [],
      version: 3,
    });
    renderPage("business");
    fireEvent.click(await screen.findByRole("button", { name: /Check against OpenCRQ/ }));
    expect(await screen.findByText("Set Ledger to Very high in OpenCRQ")).toBeTruthy();
    expect(screen.queryByText(/very_high/)).toBeNull();
  });

  it("runs the OpenCRQ crown jewels sync from the card once one exists, instead of a second check (#1134 S2)", async () => {
    const opencrqSync = { id: "s-crq", section: "crown_jewels", source: "opencrq", filters: {}, batch_type: "", remove_gone: false, frequency: "daily", hour: 6, minute: 0, weekday: 0, timezone: "UTC", enabled: true, paused_reason: "", owner_email: "admin@example.com", next_run_at: null, last_run_at: null, last_result: null };
    const base = get.getMockImplementation();
    get.mockImplementation(async (path: string) => (path.startsWith("/organization-context/syncs") ? [opencrqSync] : base?.(path)));
    post.mockResolvedValue({ ...opencrqSync, last_result: { added: 0, updated: 1, removed: 0, version: 4, error: null } });
    renderPage("business");
    fireEvent.click(await screen.findByRole("button", { name: /Sync with OpenCRQ now/ }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/organization-context/syncs/s-crq/run", {}));
    expect(screen.queryByRole("button", { name: /Check against OpenCRQ/ })).toBeNull();
  });

  it("shows the one Sources and syncs table as the last block of the Knowledge and sources tab, each section chip linked to its tab filtered on that source (#1276)", async () => {
    const base = get.getMockImplementation();
    get.mockImplementation(async (path: string) =>
      path.startsWith("/organization-context/sources")
        ? [
            { key: "opencrq", kind: "platform", display_name: "OpenCRQ", rows: { crown_jewels: 1 }, total: 1, written_by_agent: "", sync_ids: [] },
            { key: "manual", kind: "manual", display_name: "manual", rows: { crown_jewels: 2 }, total: 2, written_by_agent: "", sync_ids: [] },
          ]
        : base?.(path),
    );
    renderPage("knowledge");
    const panel = await screen.findByTestId("organization-sources");
    // Its own page (#1367, point 7): a back link to the profile, no tab bar, no header button to itself.
    expect(screen.getByRole("button", { name: /Back to Organization Profile/ })).toBeTruthy();
    expect(screen.queryByTestId("settings.organization.tabs")).toBeNull();
    expect(screen.queryByTestId("settings.organization.knowledge")).toBeNull();
    // Its own header (UX round 1 on #1476, B1): the back link first, then its title; the profile's header and
    // fields banner stay on the profile.
    const header = screen.getByTestId("settings.organization.knowledge-header");
    expect(header.firstElementChild?.textContent).toContain("Back to Organization Profile");
    expect(header.textContent).toContain("Knowledge and sources");
    expect(screen.queryByText("Organization profile", { selector: "h3" })).toBeNull();
    expect(screen.queryByTestId("settings.organization.edit")).toBeNull();
    expect(screen.queryByTestId("settings.organization.overview")).toBeNull();
    // The table's last block of its tab (UX r1 B1, B2 on #1294): no tab bar, no card below it.
    expect(within(screen.getByTestId("settings.organization.panel-knowledge")).getByTestId("organization-sources")).toBe(panel);
    const rows = await within(panel).findAllByTestId("organization-sources.row");
    expect(rows).toHaveLength(2);
    expect(panel.textContent).toContain("Entered by hand");
    expect(panel.textContent).not.toContain("Typed");
    const opencrq = rows.find((row) => row.textContent?.includes("OpenCRQ"));
    if (!opencrq) throw new Error("OpenCRQ row missing");
    // A design-system Chip (UX r1 B1 on #1232): a button that navigates to the tab filtered on the source.
    const chip = within(opencrq).getByTestId("organization-sources.chip");
    expect(chip.tagName).toBe("BUTTON");
    expect(chip.getAttribute("aria-label")).toBe("Crown jewels from OpenCRQ: 1 row");
    fireEvent.click(chip);
    await waitFor(() => expect(screen.getByRole("tab", { name: /^Business/ }).getAttribute("aria-current")).toBe("page"));
  });

  it("marks a crown jewel linked to OpenCRQ with a named icon", async () => {
    get.mockImplementation(async (path: string) =>
      path === "/organizations" || path === "/integrations"
        ? []
        : {
            ...CONTEXT,
            profile: { ...CONTEXT.profile, crown_jewels: [{ ...CONTEXT.profile.crown_jewels[0], opencrq_asset_id: "a-1" }] },
          },
    );
    renderPage("business");
    expect(await screen.findByRole("img", { name: "Linked to OpenCRQ" })).toBeTruthy();
  });

  it("shows the OpenCRQ plan from a dry run, then applies it", async () => {
    post.mockImplementation(async (_path: string, body: unknown) => ({
      applied: (body as { apply: boolean }).apply,
      plan: {
        linked: [{ crown_jewel: "Payments", opencrq_asset_id: "a-1" }],
        pushed_to_opencrq: [],
        pulled_from_opencrq: [{ opencrq_asset_id: "a-2", name: "Ledger" }],
        ambiguous: [],
        not_in_opencrq: [],
      },
      errors: [],
      version: 3,
    }));
    renderPage("business");
    fireEvent.click(await screen.findByRole("button", { name: /Check against OpenCRQ/ }));
    expect(await screen.findByText("Link Payments to its OpenCRQ asset")).toBeTruthy();
    expect(screen.getByText("Add Ledger from OpenCRQ as a crown jewel")).toBeTruthy();
    expect(post).toHaveBeenCalledWith("/organization-context/opencrq-sync", { apply: false });
    fireEvent.click(screen.getByRole("button", { name: /Apply/ }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/organization-context/opencrq-sync", { apply: true }));
  });

  it("proposes the import for an empty profile and never says the two agree while in_sync is false (#1134 S2)", async () => {
    post.mockResolvedValue({
      applied: false,
      plan: {
        linked: [],
        pushed_to_opencrq: [],
        pulled_from_opencrq: [{ opencrq_asset_id: "a-9", name: "Billing" }],
        ambiguous: [],
        not_in_opencrq: [],
        in_sync: false,
      },
      errors: [],
      version: null,
    });
    renderPage("business");
    fireEvent.click(await screen.findByRole("button", { name: /Check against OpenCRQ/ }));
    expect(await screen.findByText("Add Billing from OpenCRQ as a crown jewel")).toBeTruthy();
    expect(screen.queryByText("The crown jewels and OpenCRQ already agree.")).toBeNull();
    expect(screen.getByRole("button", { name: /Apply/ })).toBeTruthy();
  });

  it("opens the form on the draft from the platforms, names each source, and saves it as a draft version", async () => {
    post.mockImplementation(async (path: string) => {
      if (path !== "/organization-context/draft") throw new Error(`unexpected POST ${path}`);
      return {
        based_on_version: 3,
        profile: { ...CONTEXT.profile, pir_ids: ["pir-1"], tech_stack: [{ product: "CrowdStrike Falcon", vendor: "", cpe: "" }] },
        filled: [
          { field: "pir_ids", source: "opencti", detail: "1 PIR(s)" },
          { field: "tech_stack", source: "openaev", detail: "1 product(s)" },
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
    expect(panel.textContent).toContain("Tech stack from OpenAEV");
    expect(panel.textContent).toContain("OpenCRQ could not be read");
    // The backend's error text is never shown as is.
    expect(panel.textContent).not.toContain("tenant id");
    // The drafted PIR shows by its OpenCTI name in the picker (UX B1).
    expect(await screen.findByText("Ransomware on energy")).toBeTruthy();
    // The draft opened the first tab it filled, marked (#12329); the drafted tech stack is named in the panel and
    // saved with the profile, its card living on CTEM > Inventory (#1178).
    expect(screen.getByTestId("settings.organization.tab-identity.drafted")).toBeTruthy();
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

  it("a link to one card (?card=) opens the card's tab, focuses the card and drops the parameter (CTEM calls to action)", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/settings/organization?card=crown_jewels"]}>
        <OrganizationSection />
      </MemoryRouter>,
    );
    // The bare path names the first tab, but the card lives on Business: the page moves there.
    expect(await screen.findByTestId("settings.organization.panel-business")).toBeTruthy();
    await waitFor(() => expect((document.activeElement as HTMLElement | null)?.id ?? "").toMatch(/-crown_jewels$/));
  });

  it("lands ?card=tracked_organizations on the card root, never on a wrapper (UX r1 B2 on #108)", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/settings/organization/profile/business?card=tracked_organizations"]}>
        <OrganizationSection />
      </MemoryRouter>,
    );
    await waitFor(() => expect((document.activeElement as HTMLElement | null)?.id ?? "").toMatch(/-tracked_organizations$/));
    // The focused element is the card itself, so the focus ring keeps the card's radius.
    expect(document.activeElement?.getAttribute("data-testid")).toBe("settings.organizations");
    expect(document.activeElement?.getAttribute("data-anchor")).toBe("card");
  });

  it("lands ?card=outside_lens on the outside lens card root (UX r1 B1 on #1159)", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/settings/organization?card=outside_lens"]}>
        <OrganizationSection />
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("settings.organization.panel-exposure")).toBeTruthy();
    await waitFor(() => expect((document.activeElement as HTMLElement | null)?.id ?? "").toMatch(/-outside_lens$/));
    expect(document.activeElement?.getAttribute("data-testid")).toBe("settings.organization.outside-lens");
    expect(document.activeElement?.getAttribute("data-anchor")).toBe("card");
    // It folds like its siblings (UX r1 B2 on #1159).
    const card = screen.getByTestId("settings.organization.outside-lens");
    expect(within(card).getAllByRole("button").some((button) => button.hasAttribute("aria-expanded"))).toBe(true);
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

  it("closes the OpenCRQ plan when Edit starts, so Apply never runs under a stale form", async () => {
    post.mockResolvedValue({
      applied: false,
      plan: {
        linked: [{ crown_jewel: "Payments", opencrq_asset_id: "a-1" }],
        pushed_to_opencrq: [],
        pulled_from_opencrq: [],
        ambiguous: [],
        not_in_opencrq: [],
      },
      errors: [],
      version: 3,
    });
    renderPage("business");
    fireEvent.click(await screen.findByRole("button", { name: /Check against OpenCRQ/ }));
    expect(await screen.findByTestId("settings.organization.opencrq-plan")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    await waitFor(() => expect(screen.queryByTestId("settings.organization.opencrq-plan")).toBeNull());
    expect(screen.queryByRole("button", { name: /Apply/ })).toBeNull();
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
