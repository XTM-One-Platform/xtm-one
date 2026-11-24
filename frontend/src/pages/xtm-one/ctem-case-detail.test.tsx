// The case page /ctem/cases/:id renders what the API returns for the caller
// (#11613): a member the caller cannot see is a "Restricted" row in the same
// table, with no detail about it, and a case they see nothing of (404) is a
// not-found page, never an empty case.

import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import CtemCaseDetail from "./CtemCaseDetail";

const state = vi.hoisted(() => ({ notFound: false, aggregatesRestricted: false }));

vi.mock("@/hooks/api/useAuthApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAuthApi")>()),
  useMe: () => ({ data: { id: "u1", ctem_vertical: "cyber" } }),
  useUserDirectoryByIds: () => ({ data: [{ id: "u1", full_name: "Ada Lovelace", email: "ada@example.com" }] }),
}));

vi.mock("@/hooks/api/useCtemCasesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemCasesApi")>()),
  useUpdateCtemCase: () => ({ isPending: false, mutate: () => undefined }),
  useCtemCase: () =>
    state.notFound
      ? { isLoading: false, isError: true, error: { status: 404 }, data: undefined, refetch: () => undefined }
      : {
          isLoading: false,
          isError: false,
          error: null,
          refetch: () => undefined,
          data: {
            id: "c1",
            title: "Upgrade Apache HTTP Server to 2.4.62",
            remediation_kind: "version",
            product_label: "Apache HTTP Server",
            fixed_version: "2.4.62",
            state: "needs_attention",
            state_since: null,
            owner_user_id: "u1",
            due_at: null,
            accepted_reason: null,
            accepted_until: null,
            priority: "high",
            priority_basis: "",
            issue_count: 3,
            open_issue_count: 3,
            ...(state.aggregatesRestricted
              ? {
                  restricted_count: 0,
                  members_without_details: 1,
                  aggregates_restricted: true,
                  aggregates_reason: "members_without_details",
                  asset_count: null,
                  primary_asset_count: null,
                  kev_count: null,
                  max_cvss: null,
                  max_epss: null,
                }
              : { restricted_count: 1, asset_count: 2, primary_asset_count: 1, kev_count: 1, max_cvss: 9.8, max_epss: 0.94 }),
            first_seen_at: null,
            details_unavailable: [],
            keys: [],
            issues: [
              { source: "opencti", external_id: "v1", open: true, name: "CVE-2024-0001", cve: "CVE-2024-0001", cvss: 9.8, epss: 0.94, kev: true, assets: ["a"] },
              { source: "openaev", external_id: "f2", open: true, name: "CVE-2024-0002", cve: "CVE-2024-0002", cvss: 7.5, epss: 0.1, kev: false, assets: ["b"] },
              { restricted: true },
            ],
            history: [{ at: "2026-11-13T10:00:00Z", actor: "system", kind: "issue_added", issue: "Restricted", from: null, to: null, reason: null, evidence: null }],
          },
        },
}));

function page() {
  return renderWithProviders(
    <MemoryRouter initialEntries={["/ctem/cases/c1"]}>
      <Routes>
        <Route path="/ctem/cases/:id" element={<CtemCaseDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("CtemCaseDetail restricted members (#11613)", () => {
  it("draws a hidden member as a Restricted row of the issues table, with no detail", () => {
    state.notFound = false;
    page();
    const rows = screen.getAllByTestId("ctem-case-issue-row");
    expect(rows).toHaveLength(3);
    const restricted = rows.find((r) => within(r).queryByTestId("ctem-case-restricted"));
    expect(restricted).toBeDefined();
    // The restricted row is a row of the same table: same cells, empty but for the word.
    expect(restricted!.querySelectorAll("td").length).toBe(rows[0].querySelectorAll("td").length);
    expect(restricted!.textContent).toBe("Restricted");
  });

  it("shows the visible issues and the restricted count in the summary", () => {
    state.notFound = false;
    page();
    expect(screen.getByText("CVE-2024-0001")).toBeInTheDocument();
    expect(screen.getByText("1 restricted")).toBeInTheDocument();
  });

  it("says why KEV, CVSS and assets are unknown when a member is read without details (UX round 1 on #1931, B1)", () => {
    state.notFound = false;
    state.aggregatesRestricted = true;
    page();
    expect(screen.getAllByText("Unknown: 1 open issue not readable with details")).toHaveLength(3);
    expect(screen.getByText("1 without details")).toBeInTheDocument();
    expect(screen.queryByText("All visible to you")).not.toBeInTheDocument();
    expect(screen.queryByText("On the issues you can see")).not.toBeInTheDocument();
    state.aggregatesRestricted = false;
  });

  it("answers a case the caller sees nothing of with the not-found page", () => {
    state.notFound = true;
    page();
    expect(screen.getByText("Case not found")).toBeInTheDocument();
  });
});
