// The case page shows what CTEM-11 (#894) records on a case (#928): a member
// the declared tech stack version is outside every range of reads "Not
// affected" with that version, a moved upgrade target is a history row naming
// the new version, and an upgrade case with no known fixed version says so.

import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import CtemCaseDetail from "./CtemCaseDetail";
import { keyVersion, notAffectedVersion, notAffectedVersions } from "./ctem/case-version";

const state = vi.hoisted(() => ({ fixedVersion: "1.25.5" as string | null }));

vi.mock("@/hooks/api/useAuthApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAuthApi")>()),
  useMe: () => ({ data: { id: "u1", ctem_vertical: "cyber" } }),
  useUserDirectoryByIds: () => ({ data: [] }),
}));

vi.mock("@/hooks/api/useCtemCasesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemCasesApi")>()),
  useUpdateCtemCase: () => ({ isPending: false, mutate: () => undefined }),
  useCtemCase: () => ({
    isLoading: false,
    isError: false,
    error: null,
    refetch: () => undefined,
    data: {
      id: "c1",
      title: state.fixedVersion ? `Upgrade F5 nginx to ${state.fixedVersion}` : "Upgrade F5 nginx to a fixed version",
      remediation_kind: "upgrade",
      product_label: "F5 nginx",
      fixed_version: state.fixedVersion,
      state: "needs_attention",
      state_since: null,
      owner_user_id: null,
      due_at: null,
      accepted_reason: null,
      accepted_until: null,
      priority: "high",
      priority_basis: "",
      issue_count: 3,
      open_issue_count: 1,
      restricted_count: 0,
      asset_count: 1,
      crown_jewel_count: 0,
      kev_count: 0,
      max_cvss: 7.5,
      max_epss: 0.2,
      first_seen_at: null,
      details_unavailable: [],
      keys: ["upgrade:f5/nginx@1.25.4", "upgrade:f5/nginx@1.25.5"],
      issues: [
        { source: "opencti", external_id: "v1", open: true, name: "CVE-2025-0001", cve: "CVE-2025-0001", cvss: 7.5 },
        { source: "opencti", external_id: "v2", open: false, name: "CVE-2025-0002", cve: "CVE-2025-0002", cvss: 5.0 },
        { source: "opencti", external_id: "v3", open: false, name: "CVE-2025-0003", cve: "CVE-2025-0003", cvss: 4.0 },
      ],
      history: [
        { at: "2026-11-21T10:00:00Z", actor: "system", kind: "issue_closed", issue: "opencti:v2", from: null, to: null, reason: null, evidence: "not affected: version 1.25.3 is outside the affected ranges" },
        { at: "2026-11-21T10:00:00Z", actor: "system", kind: "issue_closed", issue: "opencti:v3", from: null, to: null, reason: null, evidence: "closed at the source" },
        { at: "2026-11-21T10:05:00Z", actor: "system", kind: "retargeted", issue: null, from: null, to: "upgrade:f5/nginx@1.25.5", reason: null, evidence: null },
      ],
    },
  }),
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

describe("case version helpers (#928)", () => {
  it("reads the version of an upgrade key, never @latest", () => {
    expect(keyVersion("upgrade:f5/nginx@1.25.5")).toBe("1.25.5");
    expect(keyVersion("upgrade:f5/nginx@latest")).toBeNull();
    expect(keyVersion("coa:x")).toBeNull();
    expect(keyVersion(null)).toBeNull();
  });

  it("reads the declared version of a not-affected evidence, nothing else", () => {
    expect(notAffectedVersion("not affected: version 1.25.3 is outside the affected ranges")).toBe("1.25.3");
    expect(notAffectedVersion("closed at the source")).toBeNull();
  });

  it("keeps the last close of each member: a later close at the source clears not affected", () => {
    const closed = (issue: string, evidence: string) => ({ kind: "issue_closed", issue, evidence });
    const map = notAffectedVersions([
      closed("a", "not affected: version 2 is outside the affected ranges"),
      closed("b", "not affected: version 3 is outside the affected ranges"),
      closed("b", "closed at the source"),
    ]);
    expect([...map.entries()]).toEqual([["a", "2"]]);
  });
});

describe("CtemCaseDetail target version (#928)", () => {
  it("draws a member closed on its declared version as Not affected with the version", () => {
    state.fixedVersion = "1.25.5";
    page();
    const rows = screen.getAllByTestId("ctem-case-issue-row");
    const row = rows.find((r) => within(r).queryByText("CVE-2025-0002"))!;
    expect(within(row).getByTestId("ctem-case-not-affected").textContent).toBe("Not affectedVersion 1.25.3 is outside the affected ranges");
    const other = rows.find((r) => within(r).queryByText("CVE-2025-0003"))!;
    expect(within(other).queryByTestId("ctem-case-not-affected")).toBeNull();
    expect(within(other).getByText("Closed")).toBeInTheDocument();
  });

  it("names the new target version on a retargeted history row", () => {
    state.fixedVersion = "1.25.5";
    page();
    const history = screen.getByTestId("ctem-case-history");
    expect(within(history).getByText("Changed the target version")).toBeInTheDocument();
    expect(within(history).getByText("1.25.5")).toBeInTheDocument();
    expect(within(history).getByText("Not affected: version 1.25.3 is outside the affected ranges")).toBeInTheDocument();
  });

  it("says the fixed version is unknown on an upgrade case without one, and only then", () => {
    state.fixedVersion = null;
    const { unmount } = page();
    expect(screen.getByTestId("ctem-case-fixed-version-unknown")).toBeInTheDocument();
    unmount();
    state.fixedVersion = "1.25.5";
    page();
    expect(screen.queryByTestId("ctem-case-fixed-version-unknown")).toBeNull();
  });
});
