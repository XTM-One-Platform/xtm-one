import { describe, expect, it } from "vitest";

import type { Organization } from "@/hooks/api/useOrganizationsApi";

import { scopeEstimateIds, scopeEstimateSummary } from "./members-view";

function org(id: string, role: Organization["role"], monitoring = "off", archived = false): Organization {
  return {
    id,
    name: id,
    role,
    relationship: role === "primary" ? null : "provider",
    criticality: "medium",
    opencti_organization_id: null,
    monitoring,
    created_at: "2026-11-07T00:00:00Z",
    archived_at: archived ? "2026-11-07T00:00:00Z" : null,
  };
}

const ORGS = [org("own", "primary", "on"), org("a", "tracked", "on"), org("b", "tracked", "off"), org("gone", "tracked", "on", true)];

describe("scopeEstimateIds (#9162 R3, run estimate in the scope editor)", () => {
  it("reads the monitored tracked organizations for 'All monitored organizations'", () => {
    expect(scopeEstimateIds(true, ["b"], ORGS)).toEqual(["a"]);
  });

  it("reads the picked organizations still tracked, never the primary or an archived one", () => {
    expect(scopeEstimateIds(false, ["b", "own", "gone", "b"], ORGS)).toEqual(["b"]);
  });

  it("reads nothing when only the primary is in scope", () => {
    expect(scopeEstimateIds(false, [], ORGS)).toEqual([]);
  });
});

describe("scopeEstimateSummary", () => {
  it("sums the tracked figures and counts the organizations with no figure", () => {
    expect(
      scopeEstimateSummary({
        organizations: [
          { role: "primary", expected_runs_per_month: 0, note: "the primary's own triggers are not capped", assignments: [] },
          { role: "tracked", expected_runs_per_month: 12, note: null, assignments: [{ error: null }] },
          { role: "tracked", expected_runs_per_month: 30, note: null, assignments: [{ error: null }] },
          { role: "tracked", expected_runs_per_month: 0, note: "no OpenCTI connection", assignments: [] },
        ],
      }),
    ).toEqual({ runs: 42, read: 2, unread: 1, unserved: false });
  });

  it("counts an organization whose assignment count failed as not estimated, not as zero", () => {
    expect(
      scopeEstimateSummary({
        organizations: [
          { role: "tracked", expected_runs_per_month: 12, note: null, assignments: [{ error: null }] },
          {
            role: "tracked",
            expected_runs_per_month: 5,
            note: null,
            assignments: [{ error: null }, { error: "no OpenCTI identity linked" }],
          },
        ],
      }),
    ).toEqual({ runs: 12, read: 1, unread: 1, unserved: false });
  });

  it("says no member trigger serves the picks when no tracked organization has an assignment", () => {
    expect(
      scopeEstimateSummary({
        organizations: [
          { role: "tracked", expected_runs_per_month: 0, note: null, assignments: [] },
          { role: "tracked", expected_runs_per_month: 0, note: null, assignments: [] },
        ],
      }),
    ).toEqual({ runs: 0, read: 0, unread: 0, unserved: true });
  });

  it("keeps the figure when only some picks are served", () => {
    expect(
      scopeEstimateSummary({
        organizations: [
          { role: "tracked", expected_runs_per_month: 0, note: null, assignments: [] },
          { role: "tracked", expected_runs_per_month: 7, note: null, assignments: [{ error: null }] },
        ],
      }),
    ).toEqual({ runs: 7, read: 1, unread: 0, unserved: false });
  });

  it("reads no figure when every served organization failed its count, so the runs are unknown, not zero", () => {
    expect(
      scopeEstimateSummary({
        organizations: [
          { role: "tracked", expected_runs_per_month: 0, note: null, assignments: [{ error: "no OpenCTI identity linked" }] },
          { role: "tracked", expected_runs_per_month: 0, note: null, assignments: [] },
        ],
      }),
    ).toEqual({ runs: 0, read: 0, unread: 1, unserved: false });
  });
});
