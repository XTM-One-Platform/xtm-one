// The built-in tool page reads the served registry. On first paint the
// registry has not answered yet: the page shows its back link and a skeleton,
// never "Tool not found", which only an absent id on a loaded registry earns.
// A failed read shows an inline error line (B3).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { BuiltinToolDetail } from "./BuiltinToolDetail";

const state = vi.hoisted(() => ({ registry: { isReady: false, isError: false } }));

vi.mock("@/hooks/useBuiltinTools", () => ({
  useBuiltinTools: () => ({
    describe: (id: string) => ({
      id, label: id, description: "", detail: "", tag: "", icon: () => null,
      available: true, agentOnly: false, assistantAdminOnly: false, known: false, functions: [],
    }),
    isLoading: !state.registry.isReady && !state.registry.isError,
    isError: state.registry.isError,
    isReady: state.registry.isReady,
  }),
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: true }) }));
vi.mock("@/hooks/api/useAgentApi", () => ({ useAgents: () => ({ data: [] }) }));
vi.mock("@/hooks/api/useSettingsApi", () => ({
  useGeneralAssistantConfig: () => ({ data: undefined }),
  useUpdateGeneralAssistantConfig: () => ({ mutate: vi.fn(), isPending: false }),
  useAvailableImageModels: () => ({ data: [] }),
}));
vi.mock("@/hooks/api/useAssignmentApi", () => ({
  useBoundAgentRunStats: () => ({ totalRuns: 0, lastRun: null }),
}));
vi.mock("@/pages/tools/useToolGateSummary", () => ({ useToolGateSummary: () => ({ byGroup: new Map() }) }));

function page() {
  return (
    <MemoryRouter>
      <BuiltinToolDetail toolId="builtin-web_fetch" />
    </MemoryRouter>
  );
}

describe("BuiltinToolDetail before the registry is known", () => {
  beforeEach(() => {
    state.registry = { isReady: false, isError: false };
  });

  it("shows the back link and a skeleton, not 'Tool not found', while the registry loads", () => {
    renderWithProviders(page());
    expect(screen.getByTestId("builtin-tool-detail-loading")).not.toBeNull();
    expect(screen.queryByText("Tool not found")).toBeNull();
    expect(screen.getByText("Back to Tools")).not.toBeNull();
  });

  it("shows an inline error line when the registry read failed", () => {
    state.registry = { isReady: false, isError: true };
    renderWithProviders(page());
    expect(screen.getByTestId("builtin-tool-detail-error").textContent).toContain("Built-in tools could not be loaded");
    expect(screen.queryByText("Tool not found")).toBeNull();
  });

  it("says 'Tool not found' only once the registry is loaded and the id is absent", () => {
    state.registry = { isReady: true, isError: false };
    renderWithProviders(page());
    expect(screen.getByText("Tool not found")).not.toBeNull();
  });
});
