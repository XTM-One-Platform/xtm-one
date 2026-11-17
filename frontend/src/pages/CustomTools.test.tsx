// The Tools page's Built-in tab reads the served registry. Before it
// answers, both the grid and the list view show a skeleton; when the read
// failed, an inline error line; never a blank area under the view switcher.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import CustomTools from "./CustomTools";

const state = vi.hoisted(() => ({
  registry: { isReady: false, isError: false },
  view: "grid" as "grid" | "list",
}));

vi.mock("@/hooks/useBuiltinTools", () => ({
  useBuiltinTools: () => ({
    offered: () =>
      state.registry.isReady
        ? [{
            id: "web_fetch", label: "Web Fetch", description: "Fetch a page", detail: "", tag: "Web",
            icon: () => null, available: true, agentOnly: false, assistantAdminOnly: false, known: true, functions: [],
          }]
        : [],
    isLoading: !state.registry.isReady && !state.registry.isError,
    isError: state.registry.isError,
    isReady: state.registry.isReady,
  }),
}));
// The page's tabs register palette commands; the palette is not under test here.
vi.mock("@/lib/commands/CommandsProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/commands/CommandsProvider")>()),
  useCommandSections: () => {},
}));
vi.mock("@/hooks/useViewMode", () => ({ useViewMode: () => [state.view, vi.fn()] }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "u1" }, isAdmin: true }) }));
vi.mock("@/hooks/api/useCustomToolApi", () => ({ useCustomTools: () => ({ data: [], isLoading: false }) }));
vi.mock("@/hooks/usePlatformConfig", () => ({
  useAgenticAIEnabled: () => true,
  useCustomToolsEnabled: () => false,
}));
vi.mock("@/pages/tools/useToolGateSummary", () => ({
  useToolGateSummary: () => ({ byGroup: new Map(), byName: new Map(), active: false }),
}));

function page() {
  return (
    <MemoryRouter initialEntries={["/tools"]}>
      <CustomTools />
    </MemoryRouter>
  );
}

describe("Tools page, Built-in tab, before the registry is known", () => {
  beforeEach(() => {
    state.registry = { isReady: false, isError: false };
    state.view = "grid";
  });

  it.each(["grid", "list"] as const)("shows a skeleton in the %s view while the registry loads", (view) => {
    state.view = view;
    renderWithProviders(page());
    expect(screen.getByTestId("tools-builtin-loading").getAttribute("aria-busy")).toBe("true");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each(["grid", "list"] as const)("shows an inline error line in the %s view when the read failed", (view) => {
    state.view = view;
    state.registry = { isReady: false, isError: true };
    renderWithProviders(page());
    expect(screen.getByTestId("tools-builtin-error").textContent).toContain("Built-in tools could not be loaded");
  });

  it("shows the tools once the registry is loaded", () => {
    state.registry = { isReady: true, isError: false };
    renderWithProviders(page());
    expect(screen.queryByTestId("tools-builtin-loading")).toBeNull();
    expect(screen.getAllByText("Web Fetch").length).toBeGreaterThan(0);
  });

  // The text chip ran past the card's right edge at 1024 px; the grid
  // card carries the mark as an icon whose accessible name keeps the wording.
  it("marks a built-in card company-managed with a named icon, not a text chip", () => {
    state.registry = { isReady: true, isError: false };
    renderWithProviders(page());
    const card = screen.getByTestId("tool-card");
    expect(within(card).getByLabelText("Company-managed")).toBeTruthy();
    expect(within(card).queryByText("Company-managed")).toBeNull();
  });
});
