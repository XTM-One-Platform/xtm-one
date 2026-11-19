// Settings > Provisioning in another language (#122 retest defects): the
// latest SCIM changes read in the app language (not the backend's English
// summary), dates follow the app locale (not the browser's), and a review row
// renders without a <div> inside a <p> (no React DOM nesting error).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { DeactivationEpisode, ScimActivity } from "@/hooks/api/useProvisioningApi";

let activity: ScimActivity[];
let episodes: DeactivationEpisode[];

vi.mock("@/hooks/api/useProvisioningApi", () => ({
  useScimTokens: () => ({
    data: {
      endpoint: "https://one.example/scim/v2",
      tokens: [{ id: "t1", name: "Microsoft Entra", token_prefix: "scim_ab", created_at: null, last_used_at: "2026-11-04T08:30:00Z", revoked_at: null }],
    },
    isLoading: false,
  }),
  useScimActivity: () => ({ data: activity, isLoading: false }),
  useCreateScimToken: () => ({ mutate: vi.fn(), isPending: false }),
  useRevokeScimToken: () => ({ mutate: vi.fn(), isPending: false }),
  useDeactivations: () => ({ data: episodes, isLoading: false }),
  useResolveReviewItem: () => ({ mutate: vi.fn(), isPending: false }),
}));

import { ProvisioningSection } from "./ProvisioningSection";

let consoleError: { mock: { calls: unknown[][] }; mockRestore: () => void };

beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  activity = [
    {
      id: "a1",
      token: "Microsoft Entra",
      action: "create",
      entity_type: "user",
      entity_name: "Ada Lovelace",
      summary: "SCIM provisioned ada@example.com",
      created_at: "2026-11-04T08:30:00Z",
    },
    {
      id: "a2",
      token: "Microsoft Entra",
      action: "rename",
      entity_type: "user",
      entity_name: "Ada Lovelace",
      summary: "Backend line kept as is",
      created_at: null,
    },
    {
      id: "a3",
      token: "Microsoft Entra",
      action: "toggle",
      entity_type: "user",
      entity_name: "Alan Turing",
      summary: "SCIM changed active of alan@example.com",
      active: false,
      created_at: null,
    },
    {
      id: "a4",
      token: "Microsoft Entra",
      action: "toggle",
      entity_type: "user",
      entity_name: "Grace Hopper",
      summary: "SCIM changed active of grace@example.com",
      active: true,
      created_at: null,
    },
  ];
  episodes = [
    {
      id: "e1",
      user_id: "u1",
      user_email: "ada@example.com",
      user_name: "Ada Lovelace",
      deprovisioned: true,
      source: "scim",
      actor_label: "SCIM",
      successor_user_id: null,
      counts: {},
      review_items: [{ key: "k1", kind: "agent", id: "ag1", name: "Triage agent", action: "disabled", detail: "" }],
      open_review_count: 1,
      deactivated_at: "2026-11-04T08:30:00Z",
      reactivated_at: null,
    },
  ];
});

afterEach(() => {
  consoleError.mockRestore();
});

describe("ProvisioningSection", () => {
  it("writes the latest SCIM changes in the app language, with app-locale dates", () => {
    renderWithProviders(<ProvisioningSection />, { locale: "fr" });
    expect(screen.getByText("Compte Ada Lovelace provisionné")).toBeInTheDocument();
    expect(screen.queryByText("SCIM provisioned ada@example.com")).not.toBeInTheDocument();
    // An action the table does not name keeps the backend's own line.
    expect(screen.getByText("Backend line kept as is")).toBeInTheDocument();
    expect(screen.getAllByText(/4 nov\., 08:30/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Nov 4/)).not.toBeInTheDocument();
  });

  it("names a deactivation and a reactivation by the identity provider in the app language", () => {
    renderWithProviders(<ProvisioningSection />, { locale: "fr" });
    expect(screen.getByText("Compte Alan Turing désactivé")).toBeInTheDocument();
    expect(screen.getByText("Compte Grace Hopper réactivé")).toBeInTheDocument();
    expect(screen.queryByText(/SCIM changed active/)).not.toBeInTheDocument();
  });

  it("renders a review row without a DOM nesting error", () => {
    renderWithProviders(<ProvisioningSection />);
    expect(screen.getByText("Triage agent")).toBeInTheDocument();
    const nesting = consoleError.mock.calls.filter((call: unknown[]) => String(call[0]).includes("cannot be a descendant") || String(call[0]).includes("validateDOMNesting"));
    expect(nesting).toEqual([]);
  });
});
