// The Sources page of CTEM > Inventory is for administrators only (UX round 1 on #1476, B2): the sources and syncs
// API answers 403 to a reader, so a reader sees no Sources button and a reader opening the URL lands on the inventory.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import CtemInventory from "./CtemInventory";

const auth = vi.hoisted(() => ({ isAdmin: true, isLoading: false }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => auth }));

vi.mock("@/lib/commands/CommandsProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/commands/CommandsProvider")>()),
  useCommandSections: () => {},
}));

vi.mock("@/hooks/api/useOrganizationAssetsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationAssetsApi")>()),
  useCanonicalAssets: () => ({ data: { items: [], total: 0 }, isLoading: false, isError: false, refetch: () => undefined }),
  useAssetFacets: () => ({ data: undefined }),
  useRecordAssetDecision: () => ({ isPending: false, mutate: () => undefined, mutateAsync: async () => undefined }),
  useTechStackInventory: () => ({ data: { items: [], total: 0 }, isLoading: false, isError: false, refetch: () => undefined }),
}));

vi.mock("@/hooks/api/useOrganizationContextApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationContextApi")>()),
  useOrganizationContext: () => ({ data: { profile: {} } }),
  useSaveOrganizationContext: () => ({ isPending: false, mutate: () => undefined }),
}));

vi.mock("./ctem/InventorySourcesCard", () => ({
  InventorySourcesCard: () => <div data-testid="sources-card" />,
  AssetMatchingCard: () => <div data-testid="matching-card" />,
}));

function page(path: string) {
  return renderWithProviders(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/ctem/inventory/:tab?" element={<CtemInventory />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("CtemInventory sources for administrators only", () => {
  it("shows the Sources button and the Sources page to an administrator", () => {
    auth.isAdmin = true;
    page("/ctem/inventory");
    expect(screen.getByTestId("ctem-inventory-sources")).toBeTruthy();
  });

  it("opens the Sources page for an administrator", () => {
    auth.isAdmin = true;
    page("/ctem/inventory/sources");
    expect(screen.getByTestId("ctem-inventory-sources-page")).toBeTruthy();
    expect(screen.getByTestId("sources-card")).toBeTruthy();
  });

  it("shows no Sources button to a reader", () => {
    auth.isAdmin = false;
    page("/ctem/inventory");
    expect(screen.queryByTestId("ctem-inventory-sources")).toBeNull();
    auth.isAdmin = true;
  });

  it("sends a reader who opens the Sources URL to the inventory", () => {
    auth.isAdmin = false;
    page("/ctem/inventory/sources");
    expect(screen.queryByTestId("ctem-inventory-sources-page")).toBeNull();
    expect(screen.queryByTestId("sources-card")).toBeNull();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain("Inventory");
    auth.isAdmin = true;
  });
});
