// The case page shows what CTEM-11 reads from OpenCTI (#946) on a case (#928):
// the fixed version of each issue with the Software that remediates it (or the
// course of action that names it), "Not affected" when OpenCTI relates no
// vulnerability to the declared version, "version not known to OpenCTI" when it
// holds no Software of that version, a moved upgrade target as a history row,
// and an upgrade case with no known fixed version says so. Every value comes
// from the issue fields of the viewer's own read, never from a history sentence.

import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import CtemCaseDetail from "./CtemCaseDetail";
import { keyVersion } from "./ctem/case-version";

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
      issue_count: 4,
      open_issue_count: 2,
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
        { source: "opencti", external_id: "v1", open: true, name: "CVE-2025-0001", cve: "CVE-2025-0001", cvss: 7.5, installed_version: "1.25.3", fixed_version: "1.25.5", fixed_by: "nginx 1.25.5", declared_version_known: true },
        { source: "opencti", external_id: "v4", open: true, name: "CVE-2025-0004", cve: "CVE-2025-0004", cvss: 6.0, installed_version: "1.25.3", fixed_version: "1.25.4", course_of_action_name: "Upgrade to 1.25.4", declared_version_known: true },
        { source: "opencti", external_id: "v2", open: false, name: "CVE-2025-0002", cve: "CVE-2025-0002", cvss: 5.0, installed_version: "1.25.3", not_affected: true, declared_version_known: true },
        { source: "opencti", external_id: "v3", open: false, name: "CVE-2025-0003", cve: "CVE-2025-0003", cvss: 4.0 },
      ],
      history: [
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

function rowOf(cve: string) {
  return screen.getAllByTestId("ctem-case-issue-row").find((r) => within(r).queryByText(cve))!;
}

describe("case version helpers (#928)", () => {
  it("reads the version of an upgrade key, never @latest", () => {
    expect(keyVersion("upgrade:f5/nginx@1.25.5")).toBe("1.25.5");
    expect(keyVersion("upgrade:f5/nginx@latest")).toBeNull();
    expect(keyVersion("coa:x")).toBeNull();
    expect(keyVersion(null)).toBeNull();
  });
});

describe("CtemCaseDetail target version (#928)", () => {
  it("names the fixed version with the OpenCTI Software that remediates it", () => {
    state.fixedVersion = "1.25.5";
    page();
    const cell = within(rowOf("CVE-2025-0001")).getByTestId("ctem-case-issue-version");
    expect(cell.textContent).toBe("1.25.5Matched on Software nginx 1.25.5");
  });

  it("names the course of action when no remediating Software gave the fix", () => {
    state.fixedVersion = "1.25.5";
    page();
    const cell = within(rowOf("CVE-2025-0004")).getByTestId("ctem-case-issue-version");
    expect(cell.textContent).toBe("1.25.4Named by Upgrade to 1.25.4");
  });

  it("draws a member OpenCTI does not relate to the declared version as Not affected", () => {
    state.fixedVersion = "1.25.5";
    page();
    expect(within(rowOf("CVE-2025-0002")).getByTestId("ctem-case-not-affected").textContent).toBe(
      "Not affectedOpenCTI relates no vulnerability to version 1.25.3",
    );
    const other = rowOf("CVE-2025-0003");
    expect(within(other).queryByTestId("ctem-case-not-affected")).toBeNull();
    expect(within(other).getByText("Closed")).toBeInTheDocument();
  });

  it("names the new target version on a retargeted history row", () => {
    state.fixedVersion = "1.25.5";
    page();
    const history = screen.getByTestId("ctem-case-history");
    expect(within(history).getByText("Changed the target version")).toBeInTheDocument();
    expect(within(history).getByText("1.25.5")).toBeInTheDocument();
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
