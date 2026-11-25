// CTEM-13-UI (#1142): an OpenAEV attack path that reached a critical asset is a
// "OpenAEV result received" row of the case history, worded "Critical asset
// reached", never the raw event key.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import CtemCaseDetail from "./CtemCaseDetail";

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
      title: "Patch ingress-nginx",
      remediation_kind: "version",
      product_label: "ingress-nginx",
      fixed_version: null,
      state: "remediating",
      state_since: null,
      owner_user_id: null,
      due_at: null,
      accepted_reason: null,
      accepted_until: null,
      priority: "high",
      priority_basis: "",
      issue_count: 1,
      open_issue_count: 1,
      restricted_count: 0,
      asset_count: 1,
      primary_asset_count: 1,
      kev_count: 0,
      max_cvss: 9.8,
      max_epss: null,
      first_seen_at: null,
      details_unavailable: [],
      keys: [],
      issues: [{ source: "openaev", external_id: "f1", open: true, name: "CVE-2025-1974", cve: "CVE-2025-1974", assets: ["ingress"] }],
      history: [
        {
          at: "2026-11-22T10:00:00Z",
          actor: "system",
          kind: "source_event",
          issue: null,
          from: null,
          to: "critical_asset_reached",
          reason: null,
          evidence: "openaev:sim-1",
        },
      ],
    },
  }),
}));

describe("CtemCaseDetail critical asset reached (CTEM-13-UI)", () => {
  it("words the OpenAEV event of a source_event row", () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/ctem/cases/c1"]}>
        <Routes>
          <Route path="/ctem/cases/:id" element={<CtemCaseDetail />} />
        </Routes>
      </MemoryRouter>,
    );
    // The History Table opens a row's dialog, where the event is worded (#1679).
    fireEvent.click(screen.getByTestId("ctem-audit-row"));
    const event = screen.getByTestId("ctem-case-source-event");
    expect(event).toHaveTextContent("Critical asset reached");
    expect(event).not.toHaveTextContent("critical_asset_reached");
  });
});
