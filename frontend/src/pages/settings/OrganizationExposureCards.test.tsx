// Settings > Organization, the #11543 S3 cards: Assets and exposure, Prioritization policy, Enterprise knowledge.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { api } from "@/lib/api";
import type { ProfileForm } from "@/lib/organization-profile";
import { ASSET_EDIT_WIDTHS, ASSET_READ_WIDTHS, EDIT_ACTIONS_WIDTH, type ExposureIssue, OrganizationExposureCards, RECOMMENDED_POLICY, expiryState, exposureIssueFromRefusal, exposureIssues } from "./OrganizationExposureCards";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const get = vi.mocked(api.get);

const BASE: ProfileForm = {
  shipped_products: [],
  name: "Acme",
  sector: "Finance",
  sub_sector: "",
  regions: [],
  size: "",
  primary_assets: [],
  tech_stack: [],
  pir_ids: [],
  annual_loss_threshold: "",
  approval_threshold: "",
  loaded_hourly_rate: "",
  currency: "EUR",
  ticketing: { integration: "", project: "" },
};

function Harness({
  initial,
  editing,
  onChange,
  issues,
  onScoreValidity,
}: {
  initial: ProfileForm;
  editing: boolean;
  onChange?: (form: ProfileForm) => void;
  issues?: ExposureIssue[];
  onScoreValidity?: (invalid: boolean) => void;
}) {
  const [form, setForm] = useState(initial);
  // Read-mode tables keep their search, sort and page in the URL.
  return (
    <MemoryRouter>
    <OrganizationExposureCards
      current={form}
      editing={editing}
      fieldId="org"
      onImport={() => {}}
      issues={issues}
      onScoreValidity={onScoreValidity}
      update={(patch) =>
        setForm((prev) => {
          const next = { ...prev, ...patch };
          onChange?.(next);
          return next;
        })
      }
    />
    </MemoryRouter>
  );
}

function mockApi(links: { knowledge_base_id: string; label: string; kind: string; readable: boolean }[] = []) {
  get.mockImplementation(async (path: string) => {
    if (path.startsWith("/knowledge-bases")) return [{ id: "kb-1", name: "SOC runbooks" }];
    if (path === "/organization-context/enterprise-knowledge") return links;
    return [];
  });
}

describe("expiryState (#11543 S3 reminder before expiry)", () => {
  const today = new Date(2026, 10, 13);
  it("reads expired, soon within 30 days, ok after, unset on an empty day", () => {
    expect(expiryState("2026-11-12", today)).toBe("expired");
    expect(expiryState("2026-11-13", today)).toBe("soon");
    expect(expiryState("2026-12-13", today)).toBe("soon");
    expect(expiryState("2026-12-14", today)).toBe("ok");
    expect(expiryState("", today)).toBe("unset");
  });
});

describe("OrganizationExposureCards (#11543 S3)", () => {
  it("shows the three empty states in read mode, with the CSV import offered and nothing to edit", async () => {
    mockApi();
    renderWithProviders(<Harness initial={BASE} editing={false} />);
    const assets = screen.getByTestId("settings.organization.assets-exposure");
    expect(within(assets).getByText(/No asset yet/)).toBeTruthy();
    expect(within(assets).getByRole("button", { name: /Import CSV/ })).toBeTruthy();
    expect(within(screen.getByTestId("settings.organization.prioritization-policy")).getByText(/No policy yet/)).toBeTruthy();
    expect(within(screen.getByTestId("settings.organization.enterprise-knowledge")).getByText(/No knowledge base linked/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Add an asset/ })).toBeNull();
  });

  it("adds an asset in edit mode, to classify, criticality and exposure unknown by default", async () => {
    mockApi();
    let last: ProfileForm | null = null;
    renderWithProviders(<Harness initial={BASE} editing onChange={(form) => (last = form)} />);
    fireEvent.click(screen.getByRole("button", { name: /Add an asset/ }));
    await waitFor(() => expect(screen.getAllByTestId("settings.organization.asset-row")).toHaveLength(1));
    fireEvent.change(screen.getByRole("textbox", { name: "Asset" }), { target: { value: "payments-api" } });
    expect(last!.assets_exposure).toEqual([expect.objectContaining({ name: "payments-api", category: "generic_asset", subcategory: "", criticality: "unknown", internet_facing: "unknown", source: "manual" })]);
  });

  it("offers the recommended policy on an unset policy and keeps the accepted risks", async () => {
    mockApi();
    let last: ProfileForm | null = null;
    renderWithProviders(<Harness initial={BASE} editing onChange={(form) => (last = form)} />);
    fireEvent.click(screen.getByRole("button", { name: "Use the recommended policy" }));
    expect(last!.prioritization_policy).toMatchObject({ cvss_min: RECOMMENDED_POLICY.cvss_min, epss_min: RECOMMENDED_POLICY.epss_min, kev_always_in: true });
    // Once set, the offer is gone.
    await waitFor(() => expect(screen.queryByRole("button", { name: "Use the recommended policy" })).toBeNull());
  });

  it("keeps a decimal being typed in a score and refuses one out of range", async () => {
    mockApi();
    let last: ProfileForm | null = null;
    renderWithProviders(<Harness initial={BASE} editing onChange={(form) => (last = form)} />);
    const epss = screen.getByRole("textbox", { name: "EPSS minimum" }) as HTMLInputElement;
    fireEvent.change(epss, { target: { value: "0." } });
    expect(epss.value).toBe("0.");
    fireEvent.change(epss, { target: { value: "0.25" } });
    expect(last!.prioritization_policy?.epss_min).toBe(0.25);
    fireEvent.change(epss, { target: { value: "3" } });
    expect(epss.getAttribute("aria-invalid")).toBe("true");
    expect(last!.prioritization_policy?.epss_min).toBe(0.25);
  });

  it("shows an out-of-range score's error inline and reports it so Save is blocked (UX B4)", async () => {
    mockApi();
    let last: ProfileForm | null = null;
    const validity = vi.fn();
    renderWithProviders(<Harness initial={BASE} editing onChange={(form) => (last = form)} onScoreValidity={validity} />);
    const cvss = screen.getByRole("textbox", { name: "CVSS minimum" });
    fireEvent.change(cvss, { target: { value: "7" } });
    fireEvent.change(cvss, { target: { value: "12" } });
    expect(screen.getByRole("alert").textContent).toBe("Type a number from 0 to 10, or leave it empty.");
    expect(cvss.getAttribute("aria-invalid")).toBe("true");
    await waitFor(() => expect(validity).toHaveBeenLastCalledWith(true));
    // The last valid value is kept in the form, but the page refuses Save while the field is invalid.
    expect(last!.prioritization_policy?.cvss_min).toBe(7);
    fireEvent.change(cvss, { target: { value: "8" } });
    await waitFor(() => expect(validity).toHaveBeenLastCalledWith(false));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the PIR score thresholds with their defaults and edits them as whole numbers from 0 to 100 (CTEM-08-UI)", async () => {
    mockApi();
    const { unmount } = renderWithProviders(<Harness initial={BASE} editing={false} />);
    const card = screen.getByTestId("settings.organization.prioritization-policy");
    expect(within(card).getByText("PIR score minimum: vulnerabilities")).toBeTruthy();
    expect(within(card).getByText("70")).toBeTruthy();
    expect(within(card).getByText("75")).toBeTruthy();
    // The defaults alone never make a policy "set".
    expect(within(card).getByText(/No policy yet/)).toBeTruthy();
    unmount();

    let last: ProfileForm | null = null;
    const validity = vi.fn();
    renderWithProviders(<Harness initial={BASE} editing onChange={(form) => (last = form)} onScoreValidity={validity} />);
    const vuln = screen.getByRole("textbox", { name: "PIR score minimum: vulnerabilities" }) as HTMLInputElement;
    expect(vuln.placeholder).toBe("70");
    fireEvent.change(vuln, { target: { value: "60" } });
    expect(last!.prioritization_policy?.pir_min_score_vulnerability).toBe(60);
    fireEvent.change(vuln, { target: { value: "60.5" } });
    expect(vuln.getAttribute("aria-invalid")).toBe("true");
    await waitFor(() => expect(validity).toHaveBeenLastCalledWith(true));
    fireEvent.change(vuln, { target: { value: "101" } });
    expect(screen.getByRole("alert").textContent).toBe("Type a whole number from 0 to 100, or leave it empty for the default.");
    expect(last!.prioritization_policy?.pir_min_score_vulnerability).toBe(60);
    fireEvent.change(vuln, { target: { value: "" } });
    expect(last!.prioritization_policy?.pir_min_score_vulnerability).toBeUndefined();
    const threat = screen.getByRole("textbox", { name: "PIR score minimum: threat activity" });
    fireEvent.change(threat, { target: { value: "90" } });
    expect(last!.prioritization_policy?.pir_min_score_threat).toBe(90);
    // Every field of the policy card explains itself (#1000 UX round 1 B1):
    // CVSS, EPSS, both PIR scores, primary asset criticality, KEV, Internet exposure.
    const card2 = screen.getByTestId("settings.organization.prioritization-policy");
    expect(within(card2).getAllByRole("img", { name: "About this field" })).toHaveLength(7);
  });

  it("counts a customized PIR threshold as a set policy, a default one not (#1000 review)", () => {
    mockApi();
    const empty = { cvss_min: null, epss_min: null, kev_always_in: true, internet_exposure_required: false, min_primary_asset_criticality: "", exclusions: [], accepted_risks: [] };
    const atDefault: ProfileForm = { ...BASE, prioritization_policy: { ...empty, pir_min_score_vulnerability: 70, pir_min_score_threat: 75 } };
    const { unmount } = renderWithProviders(<Harness initial={atDefault} editing={false} />);
    expect(within(screen.getByTestId("settings.organization.prioritization-policy")).getByText(/No policy yet/)).toBeTruthy();
    unmount();
    const custom: ProfileForm = { ...BASE, prioritization_policy: { ...empty, pir_min_score_vulnerability: 60 } };
    renderWithProviders(<Harness initial={custom} editing={false} />);
    expect(within(screen.getByTestId("settings.organization.prioritization-policy")).queryByText(/No policy yet/)).toBeNull();
  });

  it("ignores the policy keys the backend adds with their defaults (scope, SLA) when telling a set policy", () => {
    mockApi();
    const empty = { cvss_min: null, epss_min: null, kev_always_in: true, internet_exposure_required: false, min_primary_asset_criticality: "", exclusions: [], accepted_risks: [] };
    const stored = { ...empty, scope_min_criticality: "high", require_risk_scenario: false, sla_hours: { p1: 24, p2: 72, p3: 720 }, pir_min_score_vulnerability: 70, pir_min_score_threat: 75 };
    const form = { ...BASE, prioritization_policy: stored } as ProfileForm;
    renderWithProviders(<Harness initial={form} editing={false} />);
    expect(within(screen.getByTestId("settings.organization.prioritization-policy")).getByText(/No policy yet/)).toBeTruthy();
  });

  it("maps a refused PIR score to its field", () => {
    expect(exposureIssueFromRefusal(BASE, "prioritization_policy.pir_min_score_threat: Input should be less than or equal to 100")).toMatchObject({ section: "score", field: "pir_min_score_threat" });
  });

  it("picks an asset group among the Asset groups rows or None, never free text (UX B2, #11781)", async () => {
    mockApi();
    const initial: ProfileForm = {
      ...BASE,
      asset_groups: [{ name: "dmz", description: "", environment: "", internet_facing: "yes", owner: "", tech_stack: [], tags: [], source: "manual", external_id: "" }],
      assets_exposure: [
        { name: "web-1", category: "host", subcategory: "", criticality: "unknown", environment: "", internet_facing: "yes", owner: "", group: "", tech_stack: [], tags: [], source: "manual", external_id: "" },
      ],
    };
    renderWithProviders(<Harness initial={initial} editing />);
    const rows = screen.getAllByTestId("settings.organization.asset-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).queryByRole("textbox", { name: "Asset group" })).toBeNull();
    const trigger = within(rows[0]).getByRole("combobox", { name: "Asset group" });
    expect(trigger.textContent).toContain("None");
    expect(screen.getAllByTestId("settings.organization.asset-group-row")).toHaveLength(1);
  });

  it("reads an asset's type as category > subcategory and its criticality (#11781)", () => {
    mockApi();
    const initial: ProfileForm = {
      ...BASE,
      assets_exposure: [
        { name: "db-1", category: "cloud_resource", subcategory: "database", criticality: "very_high", environment: "", internet_facing: "no", owner: "", group: "", tech_stack: [], tags: [], source: "manual", external_id: "" },
      ],
    };
    renderWithProviders(<Harness initial={initial} editing={false} />);
    const row = screen.getByTestId("settings.organization.asset-row");
    // The test catalog draws the keys: "Database (asset subcategory)" reads "Database" in en.json.
    expect(within(row).getByText("Cloud resource > Database (asset subcategory)")).toBeTruthy();
    expect(within(row).getByText("Very high (criticality)")).toBeTruthy();
  });

  it("sizes the read columns to fit the 816 px table at 1080, the name keeping the rest (UX r1 B1, #11781)", () => {
    const widths = Object.values(ASSET_READ_WIDTHS);
    // The table of the card at 1080 is 816 px; the name keeps at least 136 px.
    expect(widths.reduce((sum, width) => sum + width, 0)).toBeLessThanOrEqual(816 - 136);
    // Owner and Asset group hold their header in fr and ja with the sort mark.
    expect(Math.min(...widths)).toBeGreaterThanOrEqual(120);
  });

  it("sizes the edit columns so the table fits the 816 px card at 1080, every picker holds its longest option and the name keeps 200 px (#12271 UX r3 B2)", () => {
    // At 1080 Owner and Asset group step aside (hideBelow 6xl): the rest, the remove column and a
    // 200 px name fit the card, so the table never scrolls sideways and the name reads whole.
    const { owner: _owner, group: _group, ...shown } = ASSET_EDIT_WIDTHS;
    expect(Object.values(shown).reduce((sum, width) => sum + width, 0) + EDIT_ACTIONS_WIDTH).toBeLessThanOrEqual(816 - 200);
    // A picker cell holds its longest English button whole, as measured in the lab at 1080
    // ("Criticality unknown" 174 px, "Exposure unknown" 172 px), plus the 24 px cell padding.
    // A longer translation ends in an ellipsis, its chevron kept, and reads whole in the title.
    expect(ASSET_EDIT_WIDTHS.criticality).toBeGreaterThanOrEqual(174 + 24);
    expect(ASSET_EDIT_WIDTHS.internet_facing).toBeGreaterThanOrEqual(172 + 24);
    // Type is a search field: a long Category > Subcategory is cut and named whole by its tooltip.
    expect(ASSET_EDIT_WIDTHS.type).toBeGreaterThanOrEqual(160);
    // From 1152 px, with Owner and Asset group back, the name keeps 200 px too.
    expect(Object.values(ASSET_EDIT_WIDTHS).reduce((sum, width) => sum + width, 0) + EDIT_ACTIONS_WIDTH).toBeLessThanOrEqual(1152 - 200);
  });

  it("draws the asset table in edit at its fixed widths with Owner in the Columns pill below 1152 px (#12271 UX r2 B2)", async () => {
    mockApi();
    renderWithProviders(<Harness initial={BASE} editing />);
    fireEvent.click(screen.getByRole("button", { name: /Add an asset/ }));
    const row = (await screen.findAllByTestId("settings.organization.asset-row"))[0];
    const table = row.closest("table")!;
    // Layout fixed, as in read mode: the table never grows past its card.
    expect(table.dataset.layout).toBe("fixed");
    expect(within(table).getByRole("columnheader", { name: "Exposure" }).style.width).toBe(`${ASSET_EDIT_WIDTHS.internet_facing}px`);
    // The name column has no width: it takes what the sized columns leave.
    expect(within(table).getByRole("columnheader", { name: "Asset" }).style.width).toBe("");
  });

  it("keeps the Internet-facing badge on one line and the Asset group header whole in en, fr, es and ja (UX r2 B1/B3, #11781)", () => {
    // Widths the UX review measured at 1080 (round 2) for the longest locales: the badge and the
    // header text in px. A cell leaves 24 px of padding; a sortable header 44 px.
    const badge = { fr: 122, es: 133, ja: 143 };
    const groupHeader = { fr: 85, es: 94 };
    expect(ASSET_READ_WIDTHS.internet_facing - 24).toBeGreaterThanOrEqual(Math.max(...Object.values(badge)));
    expect(ASSET_READ_WIDTHS.group - 44).toBeGreaterThanOrEqual(Math.max(...Object.values(groupHeader)));
  });

  it("names a cut Type whole in the design-system tooltip in edit mode, never a native title (UX r1 B2, #11781)", async () => {
    mockApi();
    const initial: ProfileForm = {
      ...BASE,
      assets_exposure: [
        { name: "db-1", category: "cloud_resource", subcategory: "database", criticality: "unknown", environment: "", internet_facing: "no", owner: "", group: "", tech_stack: [], tags: [], source: "manual", external_id: "" },
      ],
    };
    renderWithProviders(<Harness initial={initial} editing />);
    const field = within(screen.getByTestId("settings.organization.asset-row")).getByRole("combobox", { name: "Type" });
    expect(field).not.toHaveAttribute("title");
    fireEvent.focus(field);
    const tips = await screen.findAllByRole("tooltip");
    expect(tips.some((tip) => tip.textContent?.includes("Cloud resource > Database (asset subcategory)"))).toBe(true);
  });

  it("marks an asset whose group is not an asset group, and an accepted risk without owner, on their row (UX B3)", async () => {
    mockApi();
    const form: ProfileForm = {
      ...BASE,
      assets_exposure: [
        { name: "web-1", category: "host", subcategory: "", criticality: "unknown", environment: "", internet_facing: "yes", owner: "", group: "web-2", tech_stack: [], tags: [], source: "manual", external_id: "" },
        { name: "", category: "host", subcategory: "", criticality: "unknown", environment: "", internet_facing: "unknown", owner: "", group: "ghost", tech_stack: [], tags: [], source: "manual", external_id: "" },
      ],
      prioritization_policy: { ...RECOMMENDED_POLICY, accepted_risks: [{ subject: "CVE-2024-3094", scope: "", owner: "", expires_on: "", reason: "" }] },
    };
    const issues = exposureIssues(form);
    // The blank row is dropped at save, so it is not checked.
    expect(issues).toEqual([
      expect.objectContaining({ section: "asset", index: 0, field: "group" }),
      expect.objectContaining({ section: "risk", index: 0, field: "owner" }),
    ]);
    renderWithProviders(<Harness initial={form} editing issues={issues} />);
    const group = within(screen.getAllByTestId("settings.organization.asset-row")[0]).getByRole("combobox", { name: "Asset group" });
    expect(group.getAttribute("aria-invalid")).toBe("true");
    const owner = within(screen.getByTestId("settings.organization.accepted-risk-row")).getByRole("textbox", { name: "Decision owner" });
    expect(owner.getAttribute("aria-invalid")).toBe("true");
    const alerts = screen.getAllByRole("alert").map((alert) => alert.textContent);
    expect(alerts).toEqual(["Not an asset group of this list: pick a group or None.", "A decision owner is required."]);
  });

  it("maps the save's 422 to the row it names, in catalogued words (UX B3)", () => {
    const form: ProfileForm = {
      ...BASE,
      assets_exposure: [
        { name: "", category: "host", subcategory: "", criticality: "unknown", environment: "", internet_facing: "unknown", owner: "", group: "", tech_stack: [], tags: [], source: "manual", external_id: "" },
        { name: "web-1", category: "host", subcategory: "", criticality: "unknown", environment: "", internet_facing: "yes", owner: "", group: "web-2", tech_stack: [], tags: [], source: "manual", external_id: "" },
      ],
    };
    expect(exposureIssueFromRefusal(form, "asset 'web-1' is in group 'web-2', which is not an asset group")).toEqual(
      expect.objectContaining({ section: "asset", index: 1, field: "group", message: "Not an asset group of this list: pick a group or None." }),
    );
    // Index 0 of the saved list is row 1 of the form (the blank row is dropped).
    expect(exposureIssueFromRefusal(form, "assets_exposure.0.owner: String should have at most 200 characters")).toEqual(
      expect.objectContaining({ section: "asset", index: 1, field: "owner" }),
    );
    expect(exposureIssueFromRefusal(form, "prioritization_policy.cvss_min: Input should be less than or equal to 10")).toEqual(
      expect.objectContaining({ section: "score", field: "cvss_min" }),
    );
    expect(exposureIssueFromRefusal(form, "currency 'EU' is not an ISO 4217 code")).toBeNull();
  });

  it("marks an accepted risk expired, and counts it in the reminder", async () => {
    mockApi();
    const initial: ProfileForm = {
      ...BASE,
      prioritization_policy: { ...RECOMMENDED_POLICY, accepted_risks: [{ subject: "CVE-2024-3094", scope: "", owner: "CISO", expires_on: "2020-01-01", reason: "" }] },
    };
    renderWithProviders(<Harness initial={initial} editing={false} />);
    expect(screen.getByTestId("settings.organization.accepted-risk-expiry").dataset.state).toBe("expired");
    expect(screen.getByTestId("settings.organization.accepted-risks-reminder").textContent).toMatch(/1/);
  });

  it("names a linked knowledge base the reader cannot read by its saved label, with the badge", async () => {
    mockApi([{ knowledge_base_id: "kb-9", label: "Patch policy (HQ)", kind: "patch_policy", readable: false }]);
    const initial: ProfileForm = { ...BASE, enterprise_knowledge: [{ knowledge_base_id: "kb-9", label: "Patch policy (HQ)", kind: "patch_policy" }] };
    renderWithProviders(<Harness initial={initial} editing={false} />);
    const row = screen.getByTestId("settings.organization.knowledge-row");
    expect(within(row).getByText("Patch policy (HQ)")).toBeTruthy();
    await waitFor(() => expect(within(row).getByText("Not readable by you")).toBeTruthy());
  });
});
