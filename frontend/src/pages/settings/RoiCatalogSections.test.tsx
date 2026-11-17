// Settings > Organization > Units of work and Rates by role (#9448): the
// lists, the states of design 6.3, and what each form sends.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { RoiRates, RoiUnit } from "@/hooks/api/useRoiCatalogApi";
import { RoiRatesSection } from "./RoiRatesSection";
import { RoiUnitsSection } from "./RoiUnitsSection";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
// Copilot mode by default; a test sets xtmOne for the XTM One mode wording.
const mode = vi.hoisted(() => ({ xtmOne: false }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useIsXtmOne: () => mode.xtmOne,
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);
const put = vi.mocked(api.put);

const minutes = { manual_min_low: 20, manual_min_likely: 30, manual_min_high: 45, review_min: 5 };
const UNITS: RoiUnit[] = [
  { key: "threat_report_triaged", name: "Threat report triaged", role: "CTI analyst", rate_role: "analyst", builtin: true, archived_at: null, overridden: false, origin: "default", ...minutes },
  {
    key: "cve_assessed", name: "CVE assessed", role: "Vulnerability analyst", rate_role: "analyst", builtin: true, archived_at: null, overridden: true,
    origin: "customized", customized_by: "Jane Admin", customized_at: "2026-11-07T10:00:00Z",
    default: { manual_min_low: 10, manual_min_likely: 15, manual_min_high: 30, review_min: 5 },
    ...minutes,
  },
  { key: "contract_reviewed", name: "Contract reviewed", role: "Legal counsel", rate_role: "legal", builtin: false, archived_at: null, version: 2, source: "", ...minutes },
  { key: "old_unit", name: "Old unit", role: "", rate_role: "analyst", builtin: false, archived_at: "2026-11-01T10:00:00Z", version: 1, source: "", ...minutes },
];
const RATES: RoiRates = {
  rates: [
    { role: "legal", currency: "EUR", amount: 120, source: "Salary grid", builtin_role: false, updated_at: null, origin: "custom", customized_by: "Jane Admin", customized_at: null },
    {
      role: "analyst", currency: "USD", amount: 90, source: "Our grid", builtin_role: true, updated_at: null, origin: "customized", customized_by: "Jane Admin",
      customized_at: "2026-11-07T10:00:00Z", default: { amount: 75, label: "Analyst", source: "BLS" },
    },
  ],
  builtin: [{ role: "analyst", currency: "USD", amount: 75, label: "Analyst", source: "BLS", origin: "customized" }],
};

beforeEach(() => {
  vi.clearAllMocks();
  mode.xtmOne = false;
  get.mockImplementation(async (path: string) => {
    if (path === "/roi/units") return UNITS;
    if (path === "/roi/rates") return RATES;
    throw new Error(`unexpected GET ${path}`);
  });
  post.mockResolvedValue({});
  put.mockResolvedValue({});
});
afterEach(cleanup);

/** The tables keep their state in the URL (UX round 4 B1), so the sections render inside a router; the probe shows the query string. */
function LocationProbe() {
  return <output data-testid="location-search">{useLocation().search}</output>;
}
function InRouter({ children, url = "/" }: { children: ReactNode; url?: string }) {
  return (
    <MemoryRouter initialEntries={[url]}>
      {children}
      <LocationProbe />
    </MemoryRouter>
  );
}

// The app's QueryClient throws query errors to the ErrorBoundary by default
// (UX round 2 B1): these sections must keep a read error in their own alert line.
const appLikeClient = () => new QueryClient({ defaultOptions: { queries: { retry: false, throwOnError: true } } });

describe("RoiUnitsSection", () => {
  it("lists custom units, built-ins read-only and archived units apart", async () => {
    renderWithProviders(<InRouter><RoiUnitsSection /></InRouter>);
    expect(await screen.findByText("Contract reviewed")).toBeTruthy();
    expect(screen.getByText("Threat report triaged")).toBeTruthy();
    expect(screen.getByText("Archived units (1)")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Customize Threat report triaged" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit Threat report triaged" })).toBeNull();
    expect(screen.getByRole("button", { name: "Edit Contract reviewed" })).toBeTruthy();
  });

  it("titles the built-in card for the corporate catalog in Copilot mode, with estimated minutes (UX round 1 B1, #9553)", async () => {
    renderWithProviders(<InRouter><RoiUnitsSection /></InRouter>);
    expect(await screen.findByRole("heading", { name: "Built-in units" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Security units" })).toBeNull();
    expect(screen.getByText(/^Built into the platform with estimated minutes\./)).toBeTruthy();
    expect(screen.queryByText(/published minutes/)).toBeNull();
  });

  it("keeps the Security units title in XTM One mode, with estimated minutes and no source claim the table does not show (UX round 2 B1, #9553)", async () => {
    mode.xtmOne = true;
    renderWithProviders(<InRouter><RoiUnitsSection /></InRouter>);
    expect(await screen.findByRole("heading", { name: "Security units" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Built-in units" })).toBeNull();
    expect(screen.getByText(/^Built into the platform with estimated minutes\./)).toBeTruthy();
    expect(screen.queryByText(/source of its minutes/)).toBeNull();
    expect(screen.queryByText(/published minutes/)).toBeNull();
  });

  it("says when the catalog cannot be read", async () => {
    get.mockRejectedValue(new Error("boom"));
    renderWithProviders(<InRouter><RoiUnitsSection /></InRouter>);
    expect((await screen.findByRole("alert")).textContent).toBe("The units of work could not be read.");
  });

  it("keeps a 500 on the catalog in its alert line under the app's throwOnError default", async () => {
    get.mockRejectedValue(Object.assign(new Error("boom"), { status: 500 }));
    renderWithProviders(<InRouter><RoiUnitsSection /></InRouter>, appLikeClient());
    expect((await screen.findByRole("alert")).textContent).toBe("The units of work could not be read.");
  });

  it("reads the catalog again from Retry after a read error (UX round 3 B2)", async () => {
    get.mockRejectedValueOnce(Object.assign(new Error("boom"), { status: 500 }));
    renderWithProviders(<InRouter><RoiUnitsSection /></InRouter>, appLikeClient());
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Contract reviewed")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("creates a custom unit with a key from its name, and checks the minutes first", async () => {
    renderWithProviders(<InRouter><RoiUnitsSection /></InRouter>);
    await screen.findByText("Contract reviewed");
    fireEvent.click(screen.getByRole("button", { name: "New unit of work" }));
    fireEvent.change(await screen.findByLabelText(/^Name/), { target: { value: "Ticket resolved" } });
    fireEvent.change(screen.getByLabelText(/^Manual minutes, low/), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText(/^Manual minutes, likely/), { target: { value: "15" } });
    fireEvent.change(screen.getByLabelText(/^Manual minutes, high/), { target: { value: "30" } });
    fireEvent.change(screen.getByLabelText(/^Review minutes/), { target: { value: "15" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Less than the likely manual minutes.")).toBeTruthy();
    expect(post).not.toHaveBeenCalled();
    // UX round 1 B3: the field in error says so and takes the focus.
    const review = screen.getByLabelText(/^Review minutes/);
    expect(review.getAttribute("aria-invalid")).toBe("true");
    await waitFor(() => expect(document.activeElement).toBe(review));
    expect(screen.getByLabelText(/^Name/).getAttribute("aria-invalid")).toBeNull();
    fireEvent.change(screen.getByLabelText(/^Review minutes/), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const [path, body] = post.mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe("/roi/units");
    expect(body).toMatchObject({ key: "ticket_resolved", name: "Ticket resolved", manual_min_low: 10, manual_min_likely: 15, manual_min_high: 30, review_min: 2 });
  });

  it("shows the Origin of each built-in unit in its own column, never 'Overridden' (#9836 R3b)", async () => {
    renderWithProviders(<InRouter><RoiUnitsSection /></InRouter>);
    await screen.findByText("CVE assessed");
    expect(screen.getByRole("columnheader", { name: /Origin/ })).toBeTruthy();
    const badges = screen.getAllByTestId("roi-origin-badge").map((b) => b.textContent);
    expect(badges).toContain("Default");
    expect(badges).toContain("Customized");
    expect(screen.queryByText("Overridden")).toBeNull();
    // The who and when of a customization is named on its badge's focus stop.
    expect(screen.getByLabelText(/^Customized\. Changed by Jane Admin on /)).toBeTruthy();
  });

  it("opens a customized unit on the fields it changed from the default (#9836 R3b)", async () => {
    renderWithProviders(<InRouter><RoiUnitsSection /></InRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Customize CVE assessed" }));
    const diff = await screen.findByTestId("roi-default-diff");
    expect(diff.textContent).toContain("Manual minutes, likely");
    expect(diff.textContent).toContain("15");
    expect(diff.textContent).toContain("30");
    // Review minutes are the default's: not listed.
    expect(diff.textContent).not.toContain("Review minutes");
  });

  it("customizes a built-in unit's minutes under /baselines", async () => {
    renderWithProviders(<InRouter><RoiUnitsSection /></InRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Customize Threat report triaged" }));
    fireEvent.change(await screen.findByLabelText(/^Manual minutes, likely/), { target: { value: "40" } });
    fireEvent.change(screen.getByLabelText(/^Manual minutes, high/), { target: { value: "60" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const [path, body] = put.mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe("/roi/baselines/threat_report_triaged");
    expect(body).toMatchObject({ manual_min_likely: 40, manual_min_high: 60 });
    expect(body).not.toHaveProperty("name");
  });
});

describe("RoiRatesSection", () => {
  it("lists the organization's rates and the catalog rates", async () => {
    renderWithProviders(<InRouter><RoiRatesSection /></InRouter>);
    expect(await screen.findByText("legal")).toBeTruthy();
    // UX round 1 B1: the catalog's Role column names the role, its label is the Basis.
    expect(screen.getAllByText("analyst").length).toBeGreaterThan(0);
    expect(screen.getByText("Analyst")).toBeTruthy();
    // UX round 1 B2: both rates tables are searchable.
    expect(screen.getAllByRole("searchbox", { name: "Search rates" })).toHaveLength(2);
    // #9553 UX round 1 B2: catalog rates mix published and assumed bases, so the card does not call them published.
    expect(screen.getByText("Rates the platform falls back to, each with its basis and source. Read-only.")).toBeTruthy();
    expect(screen.queryByText(/^Published rates/)).toBeNull();
  });

  it("marks the hourly rate invalid on a refused Save", async () => {
    renderWithProviders(<InRouter><RoiRatesSection /></InRouter>);
    await screen.findByText("legal");
    fireEvent.click(screen.getByRole("button", { name: "Add a rate" }));
    fireEvent.change(await screen.findByLabelText(/^Role/), { target: { value: "finance" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Enter an hourly rate.")).toBeTruthy();
    const amount = screen.getByLabelText(/^Hourly rate/);
    expect(amount.getAttribute("aria-invalid")).toBe("true");
    await waitFor(() => expect(document.activeElement).toBe(amount));
    expect(put).not.toHaveBeenCalled();
  });

  it("shows the empty state with its action when no rate is set", async () => {
    get.mockImplementation(async () => ({ rates: [], builtin: RATES.builtin }));
    renderWithProviders(<InRouter><RoiRatesSection /></InRouter>);
    expect(await screen.findByText("Every role uses the organization rate.")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Add a rate/ })).toBeTruthy();
  });

  it("says when the rates cannot be read", async () => {
    get.mockRejectedValue(new Error("boom"));
    renderWithProviders(<InRouter><RoiRatesSection /></InRouter>);
    expect((await screen.findByRole("alert")).textContent).toBe("The rates could not be read.");
  });

  it("keeps a 500 on the rates in its alert line under the app's throwOnError default", async () => {
    get.mockRejectedValue(Object.assign(new Error("boom"), { status: 500 }));
    renderWithProviders(<InRouter><RoiRatesSection /></InRouter>, appLikeClient());
    expect((await screen.findByRole("alert")).textContent).toBe("The rates could not be read.");
  });

  it("reads the rates again from Retry after a read error (UX round 3 B2)", async () => {
    get.mockRejectedValueOnce(Object.assign(new Error("boom"), { status: 500 }));
    renderWithProviders(<InRouter><RoiRatesSection /></InRouter>, appLikeClient());
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("legal")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps a long source on one line with the whole citation as its title (UX round 3 B3)", async () => {
    const source = "A published survey of security analyst salaries, 2026 edition, table 4, loaded with benefits and overhead";
    get.mockImplementation(async () => ({ rates: [], builtin: [{ ...RATES.builtin[0], source }] }));
    renderWithProviders(<InRouter><RoiRatesSection /></InRouter>);
    const cell = await screen.findByTitle(source);
    expect(cell.textContent).toBe(source);
  });

  it("keeps a table's filter in the URL under its own prefix (UX round 4 B1)", async () => {
    renderWithProviders(<InRouter><RoiRatesSection /></InRouter>);
    await screen.findByText("legal");
    const pill = screen.getAllByRole("button", { name: /Currency of the amounts/ }).find((button) => button.hasAttribute("aria-expanded"));
    fireEvent.click(pill as HTMLElement);
    fireEvent.click(screen.getByRole("button", { name: "EUR" }));
    await waitFor(() => expect(screen.getByTestId("location-search").textContent).toContain("yr_f_currency=EUR"));
  });

  it("opens a table on the filter its URL prefix names (UX round 4 B1)", async () => {
    renderWithProviders(<InRouter url="/?cr_f_currency=EUR"><RoiRatesSection /></InRouter>);
    await screen.findByText("legal");
    // The catalog table holds only a USD rate: its EUR filter leaves no row, the organization's table is untouched.
    expect(screen.getAllByText("analyst")).toHaveLength(1);
  });

  it("says Custom, Customized and Default in an Origin column of both tables (#9836 R3b)", async () => {
    renderWithProviders(<InRouter><RoiRatesSection /></InRouter>);
    await screen.findByText("legal");
    expect(screen.getAllByRole("columnheader", { name: /Origin/ })).toHaveLength(2);
    const badges = screen.getAllByTestId("roi-origin-badge").map((b) => b.textContent);
    expect(badges.filter((b) => b === "Customized")).toHaveLength(2);
    expect(badges).toContain("Custom");
    // A custom role keeps Remove; a customized rate resets to default from its menu.
    expect(screen.getByRole("button", { name: "Remove the rate of legal" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Remove the rate of analyst" })).toBeNull();
    expect(screen.getByRole("button", { name: "Actions for analyst" })).toBeTruthy();
  });

  it("shows the catalog's rate next to a customized one in its dialog (#9836 R3b)", async () => {
    renderWithProviders(<InRouter><RoiRatesSection /></InRouter>);
    await screen.findByText("legal");
    fireEvent.click(screen.getByRole("button", { name: "Edit the rate of analyst" }));
    const diff = await screen.findByTestId("roi-default-diff");
    expect(diff.textContent).toContain("75");
    expect(diff.textContent).toContain("90");
    expect(diff.textContent).toContain("BLS");
  });

  it("sets a rate under its role", async () => {
    renderWithProviders(<InRouter><RoiRatesSection /></InRouter>);
    await screen.findByText("legal");
    fireEvent.click(screen.getByRole("button", { name: "Add a rate" }));
    fireEvent.change(await screen.findByLabelText(/^Role/), { target: { value: "finance" } });
    fireEvent.change(screen.getByLabelText(/^Hourly rate/), { target: { value: "95" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put.mock.calls[0]).toEqual(["/roi/rates/finance", { currency: "EUR", amount: 95, source: "" }]);
  });
});
