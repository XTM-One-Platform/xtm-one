// The Software table of an asset page is paged on the server: it opens at 25 rows, and a page size picked in
// the table reaches the API as the request's limit, with offsets that stay contiguous at every size.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { AssetSoftware } from "@/hooks/api/useOrganizationAssetsApi";
import CtemAssetDetail from "./CtemAssetDetail";

const TOTAL = 342;
const calls = vi.hoisted(() => [] as { offset: number; limit: number }[]);
const asset = vi.hoisted(() => ({ category: "endpoint" as string, subcategory: "workstation" as string }));

function softwareRow(index: number): AssetSoftware {
  return {
    vendor: "acme",
    product: `Product ${String(index).padStart(3, "0")}`,
    version: "1.0",
    cpe: null,
    first_seen: null,
    last_seen: null,
    sources: ["openaev"],
  } as AssetSoftware;
}

vi.mock("@/hooks/api/useOrganizationAssetsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationAssetsApi")>()),
  useCanonicalAsset: (_id: string | undefined, software: { offset: number; limit: number }) => {
    calls.push(software);
    const end = Math.min(TOTAL, software.offset + software.limit);
    return {
      isLoading: false,
      isError: false,
      refetch: () => undefined,
      data: {
        id: "asset-1",
        name: "laptop-01",
        category: asset.category,
        subcategory: asset.subcategory,
        criticality: null,
        internet_facing: null,
        owner: null,
        os_family: null,
        os_version: null,
        sources: ["openaev"],
        software_count: TOTAL,
        merged_on: [],
        fields: {},
        explanation: "",
        possible_matches: [],
        records: [],
        software: {
          items: Array.from({ length: Math.max(0, end - software.offset) }, (_, i) => softwareRow(software.offset + i)),
          total: TOTAL,
          offset: software.offset,
          limit: software.limit,
        },
      },
    };
  },
  useImportAssetSbom: () => ({ isPending: false, mutate: () => undefined }),
  useRecordAssetDecision: () => ({ isPending: false, mutate: () => undefined }),
}));

vi.mock("@/hooks/api/useCtemCasesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemCasesApi")>()),
  useCtemCasesForAsset: () => ({ isLoading: false, isError: false, data: { cases: [] }, refetch: () => undefined }),
}));

vi.mock("@/hooks/api/useOrganizationContextApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationContextApi")>()),
  useOrganizationContext: () => ({ data: { profile: { primary_assets: [], assets_exposure: [] } } }),
}));

vi.mock("@/hooks/useAuth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAuth")>()),
  useAuth: () => ({ isAdmin: false }),
}));

vi.mock("./ctem/InventoryEntryDialog", () => ({ InventoryEntryDialog: () => null }));

function renderPage() {
  calls.length = 0;
  return renderWithProviders(
    <MemoryRouter initialEntries={["/ctem/inventory/assets/asset-1"]}>
      <Routes>
        <Route path="/ctem/inventory/assets/:id" element={<CtemAssetDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

const softwareRows = () => screen.getAllByTestId("ctem-asset-software-row");
const firstProduct = () => /Product \d{3}/.exec(softwareRows()[0].textContent ?? "")?.[0];
const lastCall = () => calls[calls.length - 1];

function pickPageSize(size: number) {
  const section = screen.getByTestId("ctem-asset-section-asset-software");
  fireEvent.click(within(section).getByRole("combobox", { name: "Rows per page" }));
  fireEvent.click(within(screen.getByRole("listbox")).getByRole("option", { name: `${size} per page` }));
}

describe("CtemAssetDetail software paging", () => {
  it("opens the Software table at 25 rows and asks the API for 25", () => {
    asset.category = "endpoint";
    asset.subcategory = "workstation";
    renderPage();
    expect(lastCall()).toEqual({ offset: 0, limit: 25 });
    expect(softwareRows()).toHaveLength(25);
  });

  it.each([10, 50, 100])("sends a picked size of %i as the limit and keeps the next page contiguous", (size) => {
    asset.category = "endpoint";
    asset.subcategory = "workstation";
    renderPage();
    pickPageSize(size);
    expect(lastCall()).toEqual({ offset: 0, limit: size });
    expect(softwareRows()).toHaveLength(size);
    expect(firstProduct()).toBe("Product 000");
    fireEvent.click(within(screen.getByTestId("ctem-asset-section-asset-software")).getByRole("button", { name: "Next page" }));
    expect(lastCall()).toEqual({ offset: size, limit: size });
    expect(firstProduct()).toBe(`Product ${String(size).padStart(3, "0")}`);
  });

  it("pages a repository's Dependencies the same way", () => {
    asset.category = "source_code";
    asset.subcategory = "code_repository";
    renderPage();
    expect(screen.getByText("Dependencies")).toBeInTheDocument();
    expect(lastCall()).toEqual({ offset: 0, limit: 25 });
    pickPageSize(50);
    expect(lastCall()).toEqual({ offset: 0, limit: 50 });
    expect(softwareRows()).toHaveLength(50);
  });
});
