// The page of one asset group (#1556): its member assets are read from the server filtered on the group, paged with
// the table's page size, each row opening its asset; the members its source names that the inventory lacks are said.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { AssetGroupRecord, CanonicalAssetSummary } from "@/hooks/api/useOrganizationAssetsApi";
import CtemAssetGroupDetail from "./CtemAssetGroupDetail";

const calls = vi.hoisted(() => [] as { q?: string; offset: number; limit: number; filters?: Record<string, readonly string[]> }[]);
const state = vi.hoisted(() => ({ unresolved: 0, missing: false }));

const GROUP = {
  id: "g-1",
  row_version: 1,
  name: "DMZ servers",
  description: "",
  environment: "production",
  internet_facing: "yes",
  owner: "",
  tags: [],
  tech_stack: [],
  source: "openaev",
  external_id: "oaev-1",
  member_count: 2,
  members_synced_at: "2026-11-23T10:00:00Z",
} as AssetGroupRecord;

const asset = (id: string, name: string) =>
  ({ id, name, category: "server", subcategory: "", criticality: "high", internet_facing: "yes", sources: ["openaev"] }) as unknown as CanonicalAssetSummary;

vi.mock("@/hooks/api/useOrganizationAssetsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationAssetsApi")>()),
  useAssetGroup: () =>
    state.missing
      ? { isLoading: false, isError: true, data: undefined }
      : { isLoading: false, isError: false, data: { ...GROUP, unresolved_members: state.unresolved } },
  useCanonicalAssets: (query: (typeof calls)[number]) => {
    calls.push(query);
    return {
      isLoading: false,
      isError: false,
      refetch: () => undefined,
      data: { items: [asset("a-1", "srv-1"), asset("a-2", "srv-2")], total: 2, offset: query.offset, limit: query.limit },
    };
  },
}));

vi.mock("@/hooks/useAuth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAuth")>()),
  useAuth: () => ({ isAdmin: false }),
}));

vi.mock("./ctem/InventoryEntryDialog", () => ({ InventoryEntryDialog: () => null }));

function renderPage() {
  calls.length = 0;
  return renderWithProviders(
    <MemoryRouter initialEntries={["/ctem/inventory/asset-groups/g-1"]}>
      <Routes>
        <Route path="/ctem/inventory/asset-groups/:id" element={<CtemAssetGroupDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("CtemAssetGroupDetail", () => {
  it("reads its members from the server filtered on the group, 25 at a time, each row opening its asset", () => {
    state.unresolved = 0;
    state.missing = false;
    renderPage();
    expect(calls[calls.length - 1]).toMatchObject({ offset: 0, limit: 25, filters: { group: ["g-1"] } });
    const rows = screen.getAllByTestId("ctem-asset-group-member-row");
    expect(rows).toHaveLength(2);
    expect(screen.getByRole("link", { name: /srv-1/ }).getAttribute("href")).toBe("/ctem/inventory/assets/a-1");
    expect(screen.queryByTestId("ctem-asset-group-unresolved")).toBeNull();
  });

  it("says how many members its source names that the inventory lacks", () => {
    state.unresolved = 3;
    state.missing = false;
    renderPage();
    expect(screen.getByTestId("ctem-asset-group-unresolved").textContent).toContain("3");
  });

  it("answers a group that no longer exists with the not found state", () => {
    state.missing = true;
    renderPage();
    expect(screen.getByText("Asset group not found")).toBeInTheDocument();
  });
});
