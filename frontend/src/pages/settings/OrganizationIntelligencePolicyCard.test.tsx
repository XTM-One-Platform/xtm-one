// The Intelligence policy (#2435): the stored OpenCTI PIRs, a minimum score each, enabled in signals or not.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { IntelligencePolicyPir } from "@/hooks/api/useOrganizationContextApi";
import { draftOf, draftPatch, draftProblem, OrganizationIntelligencePolicyCard, parseMinScore } from "./OrganizationIntelligencePolicyCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

let admin = true;
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: admin }) }));

const get = vi.mocked(api.get);
const patch = vi.mocked(api.patch);

const PIR: IntelligencePolicyPir = {
  id: "row-1",
  pir_id: "pir-1",
  integration_id: null,
  name: "Ransomware on energy",
  description: "",
  pir_type: "THREAT_LANDSCAPE",
  criteria_summary: "",
  question: "",
  owner: "CTI team",
  review_cadence: "quarterly",
  primary_assets: [],
  min_score: 70,
  enabled: true,
  last_seen_at: null,
  gone_at: null,
};
const GONE: IntelligencePolicyPir = { ...PIR, id: "row-2", pir_id: "pir-2", name: "Old campaign", enabled: false, min_score: 40, gone_at: "2026-11-20T00:00:00Z" };

beforeEach(() => {
  admin = true;
  get.mockReset();
  patch.mockReset();
  get.mockImplementation(async (path: string) => {
    if (path.startsWith("/organization-context/intelligence-policy")) return { pirs: [PIR, GONE], total: 2 };
    if (path === "/organization-context") return { profile: { primary_assets: [{ name: "Payment platform" }] }, version: 3 };
    if (path === "/integrations") return [];
    throw new Error(`unexpected GET ${path}`);
  });
});

describe("Intelligence policy helpers", () => {
  it("reads the minimum as a whole number from 0 to 100", () => {
    expect(parseMinScore("80")).toBe(80);
    expect(parseMinScore(" 0 ")).toBe(0);
    expect(parseMinScore("101")).toBeNull();
    expect(parseMinScore("60.5")).toBeNull();
    expect(parseMinScore("")).toBeNull();
  });

  it("sends only what the draft changed, and refuses a bad minimum", () => {
    const draft = draftOf(PIR);
    expect(draftPatch(PIR, draft)).toEqual({});
    expect(draftPatch(PIR, { ...draft, min_score: "80", enabled: false })).toEqual({ min_score: 80, enabled: false });
    expect(draftProblem({ ...draft, min_score: "abc" })).not.toBeNull();
    expect(draftProblem(draft)).toBeNull();
  });
});

describe("OrganizationIntelligencePolicyCard", () => {
  it("lists the stored PIRs with their minimum, marks a gone PIR and needs no sync", async () => {
    renderWithProviders(<MemoryRouter><OrganizationIntelligencePolicyCard id="ip" /></MemoryRouter>);
    const card = await screen.findByTestId("settings.organization.intelligence-policy");
    expect(await within(card).findByText("Ransomware on energy")).toBeTruthy();
    expect(within(card).getByText("No longer in OpenCTI")).toBeTruthy();
    expect(within(card).getByText("70")).toBeTruthy();
    expect(within(card).getByText("40")).toBeTruthy();
    expect(get).not.toHaveBeenCalledWith(expect.stringMatching(/^\/organization-context\/syncs/));
  });

  it("disables a PIR in signals from its row switch", async () => {
    patch.mockResolvedValue({ ...PIR, enabled: false });
    renderWithProviders(<MemoryRouter><OrganizationIntelligencePolicyCard id="ip" /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("switch", { name: "Enable Ransomware on energy in signals" }));
    await waitFor(() => expect(patch).toHaveBeenCalledWith("/organization-context/intelligence-policy/row-1", { enabled: false }));
  });

  it("saves a minimum score of 80 from the edit dialog, the other fields kept", async () => {
    patch.mockResolvedValue({ ...PIR, min_score: 80 });
    renderWithProviders(<MemoryRouter><OrganizationIntelligencePolicyCard id="ip" /></MemoryRouter>);
    fireEvent.click(await screen.findByText("Ransomware on energy"));
    const dialog = await screen.findByTestId("settings.organization.intelligence-policy.dialog");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Minimum score" }), { target: { value: "80" } });
    fireEvent.click(within(dialog).getByTestId("settings.organization.intelligence-policy.save"));
    await waitFor(() => expect(patch).toHaveBeenCalledWith("/organization-context/intelligence-policy/row-1", { min_score: 80 }));
  });

  it("reads the policy with no switch, no refresh and no Save for a reader", async () => {
    admin = false;
    renderWithProviders(<MemoryRouter><OrganizationIntelligencePolicyCard id="ip" /></MemoryRouter>);
    await screen.findByText("Ransomware on energy");
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByTestId("settings.organization.intelligence-policy.refresh")).toBeNull();
    fireEvent.click(screen.getByText("Ransomware on energy"));
    const dialog = await screen.findByTestId("settings.organization.intelligence-policy.dialog");
    expect(within(dialog).queryByTestId("settings.organization.intelligence-policy.save")).toBeNull();
  });
});
