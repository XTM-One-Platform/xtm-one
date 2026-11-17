// /dashboards/packs on the shared data table (#9378, slice 4, UX round 1 on #11042):
// a failed read is the table's own error line with Retry, never the page error
// boundary (B1), and the Mine / Company / Shared / All scope lives in `?vis=`, so a
// reload or a link reopens the same segment (B2).

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { ErrorBoundary } from "@/components/ui/error-boundary";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { WidgetPack } from "@/types";
import WidgetPacks from "./WidgetPacks";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn(), upload: vi.fn(), download: vi.fn() },
}));

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "me" }, isAdmin: false }),
}));

const get = vi.mocked(api.get);

function pack(overrides: Partial<WidgetPack>): WidgetPack {
  return {
    id: "p",
    name: "Pack",
    description: "",
    origin: "upload",
    builtin: false,
    sources: [],
    widget_count: 1,
    notes: [],
    user_id: "me",
    is_company_managed: false,
    can_manage: true,
    created_at: "2026-11-10T10:00:00Z",
    updated_at: "2026-11-10T10:00:00Z",
    ...overrides,
  } as unknown as WidgetPack;
}

/** The app's query policy (main.tsx): a read error goes to the boundary unless the query opts out. */
function appQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, throwOnError: true } } });
}

function Search() {
  return <output data-testid="search">{useLocation().search}</output>;
}

function renderPage(url = "/dashboards/packs") {
  vi.spyOn(console, "error").mockImplementation(() => {});
  return renderWithProviders(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route
          path="/dashboards/packs"
          element={
            <ErrorBoundary fallback={<p>page replaced</p>}>
              <WidgetPacks />
              <Search />
            </ErrorBoundary>
          }
        />
      </Routes>
    </MemoryRouter>,
    appQueryClient(),
  );
}

describe("/dashboards/packs", () => {
  it("says a failed read in the table with Retry and keeps the page", async () => {
    get.mockRejectedValue(Object.assign(new Error("Internal Server Error"), { status: 500 }));
    renderPage();

    expect(await screen.findByText("The widget packs could not be loaded.")).toBeTruthy();
    expect(screen.queryByText("page replaced")).toBeNull();
    expect(screen.getByRole("button", { name: /Import a pack/ })).toBeTruthy();
  });

  it("reads the scope from ?vis= and writes it back there", async () => {
    get.mockResolvedValue([
      pack({ id: "a", name: "Mine one", user_id: "me" }),
      pack({ id: "b", name: "Company one", user_id: "other", is_company_managed: true }),
    ]);
    renderPage("/dashboards/packs?vis=company");

    expect(await screen.findByText("Company one")).toBeTruthy();
    expect(screen.queryByText("Mine one")).toBeNull();

    fireEvent.click(screen.getByRole("radio", { name: /Mine/ }));
    expect(await screen.findByText("Mine one")).toBeTruthy();
    expect(screen.getByTestId("search").textContent).toContain("vis=mine");
  });
});
