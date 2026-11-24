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
}));

vi.mock("@/hooks/api/useOrganizationContextApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationContextApi")>()),
  useOrganizationContext: () => ({ data: { profile: {} } }),
  usePatchOrganizationContext: () => ({ isPending: false, mutate: () => undefined }),
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

  it("opens the Sources page for an administrator, Asset matching under the syncs table with no header button", () => {
    auth.isAdmin = true;
    page("/ctem/inventory/sources");
    const sourcesPage = screen.getByTestId("ctem-inventory-sources-page");
    const sources = screen.getByTestId("sources-card");
    const matching = screen.getByTestId("matching-card");
    // The syncs table first, Asset matching below it (Sam, 23 Nov 2026).
    expect(sources.compareDocumentPosition(matching) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(sourcesPage.contains(matching)).toBe(true);
    expect(screen.queryByTestId("ctem-inventory-matching")).toBeNull();
    expect(screen.queryByRole("button", { name: /Asset matching/ })).toBeNull();
  });

  it("lands an old Asset matching link on the Sources page with both cards", () => {
    auth.isAdmin = true;
    page("/ctem/inventory/sources?matching=1");
    expect(screen.getByTestId("sources-card")).toBeTruthy();
    expect(screen.getByTestId("matching-card")).toBeTruthy();
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
