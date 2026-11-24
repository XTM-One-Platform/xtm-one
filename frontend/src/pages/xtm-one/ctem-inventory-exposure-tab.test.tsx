// The Exposure policy tab of CTEM > Inventory (#1511, UX round 1 on #1540): a reader sees the three cards at rest
// and the page never meets an administrators-only route (B1); an administrator edits the policy thresholds from the
// policy card's own Edit (B2).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import CtemInventory from "./CtemInventory";

const auth = vi.hoisted(() => ({ isAdmin: false, isLoading: false }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => auth }));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

vi.mock("@/lib/commands/CommandsProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/commands/CommandsProvider")>()),
  useCommandSections: () => {},
}));

const get = vi.mocked(api.get);

/** The routes of the organization context the API answers 403 to a reader (require_admin). */
const ADMIN_ONLY = ["/organization-context/syncs", "/organization-context/sources", "/organization-context/pirs", "/organization-context/proposals"];

const PROFILE = {
  version: 2,
  source: "form",
  created_at: "2026-11-20T10:00:00Z",
  author_user_id: null,
  profile: {
    name: "Acme",
    sector: "Finance",
    sub_sector: "",
    regions: ["FR"],
    size: "",
    crown_jewels: [],
    tech_stack: [],
    pir_ids: [],
    risk_appetite: { annual_loss_threshold: null, approval_threshold: null },
    loaded_hourly_rate: null,
    currency: "EUR",
    ticketing: { integration: "", project: "" },
  },
  missing_fields: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(async (path: string) => {
    if (!auth.isAdmin && ADMIN_ONLY.some((route) => path.startsWith(route))) {
      throw Object.assign(new Error("Admin privileges required"), { status: 403 });
    }
    if (path === "/organization-context") return PROFILE;
    if (path === "/organization-context/readers") return { readers: {}, agents: {} };
    if (path === "/organization-context/attack-surface/providers") return { providers: ["shodan"] };
    if (path === "/organization-context/pirs") return { pirs: [], unavailable: null };
    if (path.startsWith("/organization-context/syncs") || path.startsWith("/organization-context/sources")) return [];
    return [];
  });
});

function page() {
  // As the app: a query error with no data on screen throws to the page's error boundary (main.tsx).
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity, throwOnError: true }, mutations: { retry: false } } });
  return renderWithProviders(
    <MemoryRouter initialEntries={["/ctem/inventory/exposure"]}>
      <Routes>
        <Route path="/ctem/inventory/:tab?" element={<CtemInventory />} />
      </Routes>
    </MemoryRouter>,
    client,
  );
}

describe("CtemInventory Exposure policy tab", () => {
  it("shows a reader the three cards at rest and never reads an administrators-only route (UX r1 B1 on #1540)", async () => {
    auth.isAdmin = false;
    page();
    expect(await screen.findByText("Owned domains and outside lens")).toBeTruthy();
    expect(screen.getByText("Prioritization policy")).toBeTruthy();
    expect(screen.getByText("Evidence health")).toBeTruthy();
    expect(screen.queryByText("Admin privileges required")).toBeNull();
    const called = get.mock.calls.map(([path]) => String(path));
    expect(called.filter((path) => ADMIN_ONLY.some((route) => path.startsWith(route)))).toEqual([]);
    // No Edit for a reader, and no sentence pointing at one.
    expect(screen.queryByTestId("settings.organization.edit-prioritization_policy")).toBeNull();
    expect(screen.queryByText(/Edit to start from the recommended policy/)).toBeNull();
  });

  it("opens the policy thresholds from the policy card's own Edit for an administrator (UX r1 B2 on #1540)", async () => {
    auth.isAdmin = true;
    page();
    const card = await screen.findByTestId("settings.organization.prioritization-policy");
    fireEvent.click(await within(card).findByTestId("settings.organization.edit-prioritization_policy"));
    expect(within(card).getByLabelText(/CVSS minimum/)).toBeTruthy();
    expect(within(card).getByRole("button", { name: "Use the recommended policy" })).toBeTruthy();
    auth.isAdmin = false;
  });
});
