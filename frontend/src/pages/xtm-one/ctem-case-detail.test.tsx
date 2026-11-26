// The case page /ctem/cases/:id renders what the API returns for the caller
// (#11613): a member the caller cannot see is a "Restricted" row in the same
// table, with no detail about it, and a case they see nothing of (404) is a
// not-found page, never an empty case.

import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { QueryClient } from "@tanstack/react-query";
import { renderWithProviders } from "@/testing/providers";
import { CTEM_CASE_LIST_KEY } from "@/hooks/api/useCtemCasesApi";
import CtemCaseDetail from "./CtemCaseDetail";

const state = vi.hoisted(() => ({ notFound: false, aggregatesRestricted: false, loading: false, waitingGate: false }));

vi.mock("@/hooks/api/useAuthApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAuthApi")>()),
  useMe: () => ({ data: { id: "u1", ctem_vertical: "cyber" } }),
  useUserDirectoryByIds: () => ({ data: [{ id: "u1", full_name: "Ada Lovelace", email: "ada@example.com" }] }),
}));

vi.mock("@/hooks/api/useCtemCasesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemCasesApi")>()),
  useUpdateCtemCase: () => ({ isPending: false, mutate: () => undefined }),
  useCtemCase: () =>
    state.loading
      ? { isLoading: true, isError: false, error: null, data: undefined, refetch: () => undefined }
      : state.notFound
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
            state: state.waitingGate ? "waiting_decision" : "needs_attention",
            ...(state.waitingGate
              ? {
                  gates: [
                    {
                      key: "ctem.accept_risk",
                      phase: null,
                      status: "waiting",
                      asked_at: "2026-11-26T06:00:00Z",
                      asked_of: { type: "admins", name: null },
                      asked_by_agent: null,
                      deadline: null,
                      answered_at: null,
                      answered_by: null,
                      surface: null,
                      option: null,
                      comment: null,
                      reason: null,
                      passed_at: null,
                      wait_seconds: 600,
                      decision_id: null,
                      work_item_id: null,
                      decision_filed: false,
                    },
                  ],
                }
              : {}),
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

function page(client?: QueryClient) {
  return renderWithProviders(
    <MemoryRouter initialEntries={["/ctem/cases/c1"]}>
      <Routes>
        <Route path="/ctem/cases/:id" element={<CtemCaseDetail />} />
      </Routes>
    </MemoryRouter>,
    ...(client ? [client] : []),
  );
}

describe("CtemCaseDetail first paint (#1986)", () => {
  it("draws the header from the cached list row while the case is read", () => {
    state.loading = true;
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(CTEM_CASE_LIST_KEY, {
      vertical: "cyber",
      counts: {},
      cases: [{ id: "c1", title: "Upgrade Apache HTTP Server to 2.4.62", state: "new", priority: "high", owner_user_id: null }],
    });
    page(client);
    expect(screen.getByTestId("ctem-case-listed-header")).toBeInTheDocument();
    expect(screen.getByText("Upgrade Apache HTTP Server to 2.4.62")).toBeInTheDocument();
    state.loading = false;
  });

  it("keeps the page skeleton when the case is not in a cached list", () => {
    state.loading = true;
    page();
    expect(screen.queryByTestId("ctem-case-listed-header")).not.toBeInTheDocument();
    state.loading = false;
  });
});

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

  it("draws the waiting decisions once, above the work card (#2477 UX round 2, B1)", () => {
    state.notFound = false;
    state.waitingGate = true;
    page();
    expect(screen.getAllByTestId("ctem-case-waiting-gates")).toHaveLength(1);
    expect(screen.getAllByTestId("ctem-case-waiting-gate")).toHaveLength(1);
    state.waitingGate = false;
  });

  it("answers a case the caller sees nothing of with the not-found page", () => {
    state.notFound = true;
    page();
    expect(screen.getByText("Case not found")).toBeInTheDocument();
  });
});
