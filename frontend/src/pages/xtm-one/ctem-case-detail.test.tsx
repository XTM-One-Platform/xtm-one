// The case page /ctem/cases/:id renders what the API returns for the caller
// (#11613): a member the caller cannot see is a "Restricted" row in the same
// table, with no detail about it, and a case they see nothing of (404) is a
// not-found page, never an empty case.

import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { QueryClient } from "@tanstack/react-query";
import { renderWithProviders } from "@/testing/providers";
import { CTEM_CASE_LIST_KEY, ctemCaseMaxCvss, ctemCasePollInterval } from "@/hooks/api/useCtemCasesApi";
import CtemCaseDetail from "./CtemCaseDetail";

const state = vi.hoisted(() => ({
  notFound: false,
  aggregatesRestricted: false,
  loading: false,
  waitingGate: false,
  noCvss: false,
  issuesCvss: true,
  factsUnread: 0,
  closed: false,
  closedState: "resolved",
  resolutionKind: "closed_by_decision" as string | null,
  withAssets: false,
  withSignal: false,
}));

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
            state: state.closed ? state.closedState : state.waitingGate ? "waiting_decision" : "needs_attention",
            ...(state.closed ? { resolution_kind: state.resolutionKind, open_at_source_count: 2 } : {}),
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
            open_issue_count: state.closed ? 0 : 3,
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
              : { restricted_count: 1, asset_count: 2, primary_asset_count: 1, kev_count: 1, max_cvss: state.noCvss ? null : 9.8, max_epss: 0.94, members_facts_unread: state.factsUnread }),
            first_seen_at: null,
            details_unavailable: [],
            ...(state.withAssets
              ? {
                  affected_assets: {
                    version_source: null,
                    asset_count: 2,
                    more_assets: 0,
                    assets: [
                      { name: "a", issue_count: 1, open_issue_count: 1, kev: true, max_cvss: 9.8 },
                      { name: "b", issue_count: 1, open_issue_count: 1, kev: false, max_cvss: 7.5 },
                    ],
                  },
                }
              : {}),
            keys: [],
            issues: [
              { source: "opencti", external_id: "v1", open: true, name: "CVE-2024-0001", cve: "CVE-2024-0001", cvss: state.issuesCvss ? 9.8 : null, epss: 0.94, kev: true, assets: ["a"] },
              { source: "openaev", external_id: "f2", open: true, name: "CVE-2024-0002", cve: "CVE-2024-0002", cvss: state.issuesCvss ? 7.5 : null, epss: 0.1, kev: false, assets: ["b"] },
              { restricted: true },
              ...(state.withSignal
                ? [{ source: "signal", external_id: "s1", open: true, name: "CVE-2024-21762", lens: "outside", source_provider: "opencti", cvss: 9.8, kev: true, assets: ["a"] }]
                : []),
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

describe("CtemCaseDetail context row (Sam, 28 Nov 2026)", () => {
  it("puts Affected assets and Issues side by side right after the stat cards, Issues drawn once", () => {
    state.withAssets = true;
    page();
    const context = screen.getByTestId("ctem-case-context");
    expect(within(context).getByTestId("ctem-case-affected-assets")).toBeInTheDocument();
    expect(within(context).getByTestId("ctem-case-issues")).toBeInTheDocument();
    expect(screen.getAllByTestId("ctem-case-issues")).toHaveLength(1);
    // The context comes before the History card in the page order.
    const history = screen.getByTestId("ctem-audit-trail");
    expect(context.compareDocumentPosition(history) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    state.withAssets = false;
  });

  it("opens the Issues table on its context columns, the rest one click away under Columns", () => {
    page();
    const issues = screen.getByTestId("ctem-case-issues");
    for (const name of ["Issue", "CVSS", "KEV", "Assets", "State"]) {
      expect(within(issues).getByRole("columnheader", { name: new RegExp(name) })).toBeInTheDocument();
    }
    for (const name of ["Source", "EPSS", "Fixed version"]) {
      expect(within(issues).queryByRole("columnheader", { name: new RegExp(name) })).toBeNull();
    }
  });

  it("never cuts the CVE of a signal row: the label holds its width, the chips go to their own line (UX round 1 B1 on #2738)", () => {
    state.withSignal = true;
    page();
    const label = screen.getByTestId("ctem-case-issue-signal-label");
    // The whole CVE as its own text, never a TruncatedText that may cut it.
    expect(label.textContent).toBe("CVE-2024-21762");
    // The lens and source chips sit on a line of their own, not beside the CVE.
    const source = screen.getByTestId("ctem-case-issue-signal-source");
    expect(label.parentElement?.contains(source)).toBe(false);
    state.withSignal = false;
  });

  it("draws a signal row on two lines: the CVE, then the chips and the caption on one line (UX round 2 B1 on #2738)", () => {
    state.withSignal = true;
    page();
    const cell = screen.getByTestId("ctem-case-issue-signal");
    // Exactly two lines: the label line and the meta line, never a third caption line.
    expect(cell.children).toHaveLength(2);
    const meta = screen.getByTestId("ctem-case-issue-signal-meta");
    expect(meta.contains(screen.getByTestId("ctem-case-issue-signal-source"))).toBe(true);
    const caption = screen.queryByTestId("ctem-case-issue-signal-caption");
    if (caption) {
      expect(meta.contains(caption)).toBe(true);
      // The caption takes the width left, never a fixed cap.
      expect(caption.innerHTML).not.toContain("max-w-[11rem]");
    }
    state.withSignal = false;
  });

  it("draws the Issues card alone, full width, when no asset is named", () => {
    page();
    expect(screen.queryByTestId("ctem-case-affected-assets")).toBeNull();
    expect(within(screen.getByTestId("ctem-case-context")).getByTestId("ctem-case-issues")).toBeInTheDocument();
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

  it("counts the members of a closed case open at source apart (#2675)", () => {
    state.notFound = false;
    state.closed = true;
    page();
    expect(screen.getByText("Open at source")).toBeInTheDocument();
    expect(screen.getByText("Case closed by decision")).toBeInTheDocument();
    expect(screen.queryByText("Open issues")).not.toBeInTheDocument();
    state.closed = false;
  });

  it.each([
    ["accepted_risk", "closed_by_decision", "Case closed, risk accepted"],
    ["mitigated_watching", null, "Case mitigated, watching"],
    ["resolved", "fixed", "Case closed as fixed"],
    ["resolved", "not_exposed", "Case closed as not exposed"],
    ["resolved", "not_relevant", "Case closed as not relevant"],
    ["resolved", "dismissed", "Case dismissed by a person"],
    ["resolved", null, "Case closed"],
  ])("says the true status of a %s case (kind %s) under the open-at-source count (UX round 1 on #2678, B1)", (closedState, kind, line) => {
    state.notFound = false;
    state.closed = true;
    state.closedState = closedState;
    state.resolutionKind = kind;
    page();
    expect(screen.getByText(line)).toBeInTheDocument();
    expect(screen.queryByText("Case closed by decision")).not.toBeInTheDocument();
    state.closed = false;
    state.closedState = "resolved";
    state.resolutionKind = "closed_by_decision";
  });

  it("says a case whose visible issues carry no CVSS is not scored (#2630)", () => {
    state.notFound = false;
    state.noCvss = true;
    state.issuesCvss = false;
    page();
    expect(screen.getByText("Not scored")).toBeInTheDocument();
    state.noCvss = false;
    state.issuesCvss = true;
  });

  it("agrees with the issues table when the case has no max_cvss but a visible issue has a CVSS (UX round 1 on #2645, B1)", () => {
    state.notFound = false;
    state.noCvss = true;
    page();
    expect(screen.queryByText("Not scored")).not.toBeInTheDocument();
    // KEV and Max CVSS both carry the scope subtitle: the card reads the issues' 9.8, never "-".
    expect(screen.getAllByText("On the issues you can see")).toHaveLength(2);
    state.noCvss = false;
  });

  it("says the CVSS is unknown, never not scored, when CVE facts are unread for the reader (UX round 1 on #2645, B1)", () => {
    state.notFound = false;
    state.noCvss = true;
    state.issuesCvss = false;
    state.factsUnread = 4;
    page();
    expect(screen.queryByText("Not scored")).not.toBeInTheDocument();
    expect(screen.getByText("Unknown: CVE facts not readable for you")).toBeInTheDocument();
    state.noCvss = false;
    state.issuesCvss = true;
    state.factsUnread = 0;
  });

  it("keeps the scope subtitle when the case has a CVSS (#2630)", () => {
    state.notFound = false;
    page();
    expect(screen.queryByText("Not scored")).not.toBeInTheDocument();
  });

  it("answers a case the caller sees nothing of with the not-found page", () => {
    state.notFound = true;
    page();
    expect(screen.getByText("Case not found")).toBeInTheDocument();
  });
});

describe("ctemCaseMaxCvss: the Max CVSS card agrees with the issues of the page (UX round 1 on #2645, B1)", () => {
  const open = (cvss: number | null) => ({ source: "opencti", external_id: `v${cvss}`, open: true, cvss });

  it("reads the case's own max_cvss first", () => {
    expect(ctemCaseMaxCvss({ max_cvss: 8.8, issues: [open(9.8)] })).toEqual({ value: 8.8, basis: "case" });
  });

  it("falls back to the highest CVSS of the open visible issues", () => {
    const closed = { source: "opencti", external_id: "c", open: false, cvss: 10 };
    expect(ctemCaseMaxCvss({ max_cvss: null, issues: [open(9.3), open(9.8), closed, { restricted: true }], members_facts_unread: 4 })).toEqual({
      value: 9.8,
      basis: "visible_issues",
    });
  });

  it("says unknown when facts are unread and no visible issue carries a CVSS", () => {
    expect(ctemCaseMaxCvss({ max_cvss: null, issues: [open(null)], members_facts_unread: 1 })).toEqual({ value: null, basis: "facts_unread" });
  });

  it("says not scored only with no visible CVSS and nothing unread", () => {
    expect(ctemCaseMaxCvss({ max_cvss: null, issues: [open(null)], members_facts_unread: 0 })).toEqual({ value: null, basis: "not_scored" });
    expect(ctemCaseMaxCvss({ max_cvss: null, issues: [] })).toEqual({ value: null, basis: "not_scored" });
  });

  it("keeps restricted aggregates unknown, never computed from the visible issues", () => {
    expect(ctemCaseMaxCvss({ max_cvss: null, aggregates_restricted: true, issues: [open(9.8)] })).toEqual({ value: null, basis: "restricted" });
  });
});

describe("ctemCasePollInterval: the CVSS written after the creation shows without a reload (#2630)", () => {
  const created = "2026-11-27T15:00:00Z";
  const at = (seconds: number) => Date.parse(created) + seconds * 1000;

  it("reads a fresh case without a CVSS again every 3 s", () => {
    expect(ctemCasePollInterval({ max_cvss: null, created_at: created }, "success", at(2))).toBe(3000);
  });

  it("stops once the CVSS is written", () => {
    expect(ctemCasePollInterval({ max_cvss: 8.8, created_at: created }, "success", at(5))).toBe(false);
  });

  it("stops two minutes after the creation: the case is then not scored", () => {
    expect(ctemCasePollInterval({ max_cvss: null, created_at: created }, "success", at(121))).toBe(false);
  });

  it("never polls a refused read, a case without a creation time or with restricted aggregates", () => {
    expect(ctemCasePollInterval({ max_cvss: null, created_at: created }, "error", at(2))).toBe(false);
    expect(ctemCasePollInterval({ max_cvss: null, created_at: null }, "success", at(2))).toBe(false);
    expect(ctemCasePollInterval({ max_cvss: null, created_at: created, aggregates_restricted: true }, "success", at(2))).toBe(false);
    expect(ctemCasePollInterval(undefined, "pending", at(2))).toBe(false);
  });
});
