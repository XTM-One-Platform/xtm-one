import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { TrackedDraftResponse, TrackedDraftRow } from "@/hooks/api/useOrganizationsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

import TrackedOrganizationsAiDraftReview from "./TrackedOrganizationsAiDraftReview";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

function row(name: string, patch: Partial<TrackedDraftRow> = {}): TrackedDraftRow {
  return {
    name,
    relationship: "provider",
    criticality: "medium",
    sector: "Cloud",
    regions: [],
    website: "",
    confidence: "high",
    basis: "Known cloud provider",
    opencti_matches: [],
    opencti_organization_id: "",
    already_tracked_id: "",
    error: "",
    notes: [],
    status: "pending",
    ...patch,
  };
}

const DRAFT: TrackedDraftResponse = {
  proposal_id: "p1",
  status: "pending",
  rows: [
    row("Acme Cloud", { opencti_matches: [{ id: "o1", name: "Acme Cloud" }], opencti_organization_id: "o1" }),
    row("Globex"),
    row("Initech", { status: "conflict", already_tracked_id: "t1" }),
    row("Umbrella", { status: "error", error: "The model answered no proposal for this batch." }),
  ],
  names_read: 4,
  names_drafted: 4,
  requests: 1,
  opencti_unavailable: null,
  created_at: "2026-11-07T14:00:00Z",
};

function renderPage() {
  return renderWithProviders(
    <MemoryRouter initialEntries={["/settings/organization/tracked/ai-draft/p1"]}>
      <Routes>
        <Route path="/settings/organization/tracked/ai-draft/:proposalId" element={<TrackedOrganizationsAiDraftReview />} />
        <Route path="/settings/organization" element={<p>organization page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("TrackedOrganizationsAiDraftReview", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    get.mockResolvedValue(DRAFT);
  });

  it("shows the count line, the conflict row and the failed batch, and creates nothing before an accept", async () => {
    renderPage();
    expect(await screen.findByTestId("tracked-ai-draft.count")).toHaveTextContent("4 proposals, 0 accepted");
    expect(screen.getAllByText("Already tracked").length).toBeGreaterThan(0);
    // The backend's English text is mapped to a translated line, kept only behind Details (UX round 1, B7).
    expect(screen.getByText("1 name was not drafted: The model proposed nothing for these names")).toBeInTheDocument();
    expect(screen.queryByText(/answered no proposal/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create accepted (0)" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Accept Initech" })).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("sends only the accepted rows with their OpenCTI link, then goes back to the organization page", async () => {
    post.mockResolvedValue({ status: "accepted", version: null, created: ["x"] });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Accept Acme Cloud" }));
    fireEvent.click(screen.getByRole("button", { name: "Create accepted (1)" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/organization-context/proposals/p1/decide", { accept: ["Acme Cloud"], opencti_ids: { "Acme Cloud": "o1" } }),
    );
    expect(await screen.findByText("organization page")).toBeInTheDocument();
  });

  it("disables the selection box of rows that cannot be accepted and shows - for their relationship and criticality (UX round 1, B3, B4)", async () => {
    renderPage();
    await screen.findByTestId("tracked-ai-draft.count");
    const rows = screen.getAllByTestId("tracked-ai-draft.row");
    const boxOf = (name: string) => {
      const tr = rows.find((r) => r.textContent?.includes(name));
      return tr?.querySelector('[role="checkbox"]') as HTMLButtonElement;
    };
    expect(boxOf("Initech")).toBeDisabled();
    expect(boxOf("Umbrella")).toBeDisabled();
    expect(boxOf("Globex")).not.toBeDisabled();
    expect(screen.queryByText(/Criticality:/)).not.toBeInTheDocument();
  });

  it("draws every column header and gives the truncated conflict link a tooltip (UX round 2 B1, round 3 B4)", async () => {
    renderPage();
    await screen.findByTestId("tracked-ai-draft.count");
    // Layout (columns hidden below 4xl and 7xl, one row height) is measured in the lab, never through class names.
    const headers = screen.getAllByRole("columnheader").map((th) => th.textContent ?? "");
    for (const name of ["Name", "Review", "Match confidence", "Relationship", "Criticality", "Sector"]) {
      expect(headers.some((text) => text.includes(name))).toBe(true);
    }
    // The hint is plain text; the conflict row opens Tracked organizations (#12070).
    const hint = screen.getByTestId("tracked-ai-draft.conflict-hint");
    expect(hint).toHaveAttribute("title", "See it in Tracked organizations");
    expect(hint.closest("a")).toHaveAttribute("href", "/settings/organization");
  });

  it("discards the draft with an empty accept list", async () => {
    post.mockResolvedValue({ status: "rejected", version: null, created: [] });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Discard draft" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/organization-context/proposals/p1/decide", { accept: [], opencti_ids: {} }));
  });
});
