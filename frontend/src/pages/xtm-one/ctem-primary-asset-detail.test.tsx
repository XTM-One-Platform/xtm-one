// The page of one primary asset (#1980 S2): its supporting assets are read from the server for that primary asset,
// paged with the table's page size, direct links and asset group members told apart by name, each matched row
// opening its asset; the open cases reaching it are listed; an unknown name answers the not found state.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { PrimaryAssetPage, PrimaryAssetSupporter } from "@/hooks/api/useOrganizationAssetsApi";
import CtemPrimaryAssetDetail from "./CtemPrimaryAssetDetail";

const calls = vi.hoisted(() => [] as { name: string | undefined; page: { q: string; offset: number; limit: number } }[]);
const caseCalls = vi.hoisted(() => [] as (string | undefined)[]);
const state = vi.hoisted(() => ({ missing: false, empty: false }));

const supporter = (id: string, name: string, kind: PrimaryAssetSupporter["through_kind"], through = ""): PrimaryAssetSupporter => ({
  record_id: `r-${id}`,
  asset_id: id === "x" ? null : id,
  name,
  category: "host",
  criticality: "high",
  through_kind: kind,
  through,
  source: "opencrq",
  roles: ["stores"],
  inherited_needs: { confidentiality: "high" },
  via: "",
});

const PAGE = (empty: boolean): PrimaryAssetPage => ({
  primary_asset: { name: "Payments platform", criticality: "very_high", owner: "Finance", business_service: "Payments", source: "opencrq" },
  supporters: empty
    ? { items: [], total: 0, offset: 0, limit: 25, counts: { total: 0, through: {}, source: {} } }
    : {
        items: [supporter("a-1", "payments-db", "asset"), supporter("a-2", "laptop-001", "asset_group", "Employee laptops"), supporter("x", "laptop-unmatched", "asset_group", "Employee laptops")],
        total: 265,
        offset: 0,
        limit: 25,
        counts: { total: 265, through: { asset: 2, asset_group: 263 }, source: { opencrq: 265 } },
      },
});

vi.mock("@/hooks/api/useOrganizationAssetsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationAssetsApi")>()),
  usePrimaryAsset: (name: string | undefined, page: { q: string; offset: number; limit: number }) => {
    calls.push({ name, page });
    return state.missing
      ? { isLoading: false, isError: true, data: undefined, refetch: () => undefined }
      : { isLoading: false, isError: false, data: PAGE(state.empty), refetch: () => undefined };
  },
}));

vi.mock("@/hooks/api/useCtemCasesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemCasesApi")>()),
  useCtemCasesForPrimaryAsset: (name: string | undefined) => {
    caseCalls.push(name);
    return {
      isLoading: false,
      isError: false,
      refetch: () => undefined,
      data: {
        cases: [
          { id: "c-1", title: "Upgrade nginx", state: "remediating", priority: "high", due_at: null },
          { id: "c-2", title: "Old resolved", state: "resolved", priority: "low", due_at: null },
        ],
        counts: {},
      },
    };
  },
}));

function renderPage() {
  calls.length = 0;
  caseCalls.length = 0;
  return renderWithProviders(
    <MemoryRouter initialEntries={["/ctem/inventory/primary-assets/Payments%20platform"]}>
      <Routes>
        <Route path="/ctem/inventory/primary-assets/:name" element={<CtemPrimaryAssetDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("CtemPrimaryAssetDetail", () => {
  it("reads its supporting assets from the server by name, 25 at a time, and says how each reaches it", () => {
    state.missing = false;
    state.empty = false;
    renderPage();
    expect(calls[calls.length - 1]).toEqual({ name: "Payments platform", page: { q: "", offset: 0, limit: 25 } });
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Payments platform");
    expect(screen.getAllByTestId("ctem-primary-asset-supporter-row")).toHaveLength(3);
    expect(screen.getByText("Linked directly")).toBeInTheDocument();
    expect(screen.getAllByText("Through the asset group Employee laptops")).toHaveLength(2);
    expect(screen.getByRole("link", { name: /payments-db/ }).getAttribute("href")).toBe("/ctem/inventory/assets/a-1");
    expect(screen.getByTestId("ctem-primary-asset-overview").textContent).toContain("2 linked directly");
    expect(screen.getByTestId("ctem-primary-asset-overview").textContent).toContain("263 through asset groups");
  });

  it("lists the open cases reaching it, never a resolved one", () => {
    state.missing = false;
    state.empty = false;
    renderPage();
    expect(caseCalls[caseCalls.length - 1]).toBe("Payments platform");
    const rows = screen.getAllByTestId("ctem-primary-asset-case-row");
    expect(rows).toHaveLength(1);
    expect(screen.getByRole("link", { name: /Upgrade nginx/ }).getAttribute("href")).toBe("/ctem/cases/c-1");
  });

  it("says when nothing supports it yet, never a bare zero", () => {
    state.missing = false;
    state.empty = true;
    renderPage();
    expect(screen.getByTestId("ctem-primary-asset-overview").textContent).toContain("None linked yet");
    expect(screen.getAllByText("No asset of the inventory supports this primary asset yet.").length).toBeGreaterThanOrEqual(1);
  });

  it("answers an unknown name with the not found state", () => {
    state.missing = true;
    renderPage();
    expect(screen.getByText("Primary asset not found")).toBeInTheDocument();
  });
});
