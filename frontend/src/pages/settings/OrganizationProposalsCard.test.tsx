// Settings > Organization > Exposure, "Proposed changes" (CTEM-30, UX round 1 B2 on #941): a pending scope
// proposal from the loop trace is listed with its current and proposed value and decided here.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { OrganizationProposalsCard } from "./OrganizationProposalsCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

const SCOPE = {
  id: "p-1",
  kind: "fields",
  status: "pending",
  created_at: "2026-11-21T19:00:00Z",
  proposals: [
    {
      field: "prioritization_policy.scope_min_criticality",
      value: "medium",
      current: "high",
      conflict: true,
      basis: "Watchlisted CTEM signal on portal: criticality medium below the scope threshold",
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  post.mockResolvedValue({ status: "accepted", version: 4, created: [] });
});

describe("OrganizationProposalsCard", () => {
  it("lists a pending scope proposal with the current and proposed threshold and its basis", async () => {
    get.mockResolvedValue([SCOPE]);
    renderWithProviders(<OrganizationProposalsCard />);
    const card = await screen.findByTestId("settings.organization.proposals");
    expect(card.textContent).toContain("Scope threshold");
    expect(card.textContent).toContain("From High (criticality) to Medium (criticality)");
    expect(card.textContent).toContain("Watchlisted CTEM signal on portal");
    expect(get).toHaveBeenCalledWith("/organization-context/proposals?status=pending");
  });

  it("accepts every field of the proposal", async () => {
    get.mockResolvedValue([SCOPE]);
    renderWithProviders(<OrganizationProposalsCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/organization-context/proposals/p-1/decide", {
        accept: ["prioritization_policy.scope_min_criticality"],
      }),
    );
  });

  it("rejects it with no field", async () => {
    get.mockResolvedValue([SCOPE]);
    renderWithProviders(<OrganizationProposalsCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Reject" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/organization-context/proposals/p-1/decide", { accept: [] }));
  });

  it("renders nothing when no field proposal waits", async () => {
    get.mockResolvedValue([{ ...SCOPE, kind: "new_tracked" }]);
    renderWithProviders(<OrganizationProposalsCard />);
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(screen.queryByTestId("settings.organization.proposals")).toBeNull();
  });
});
