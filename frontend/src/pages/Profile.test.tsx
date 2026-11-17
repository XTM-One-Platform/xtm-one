// The Profile page's General Assistant override reads the built-in suites
// from the served registry. Before it answers, the Built-in Tools
// group shows a skeleton, and an inline error line when the read failed,
// instead of an empty group that pushes the rest down when it lands.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { GAToolsSection } from "./Profile";

const state = vi.hoisted(() => ({
  registry: { isReady: false, isError: false },
}));

vi.mock("@/hooks/useBuiltinTools", () => ({
  useBuiltinTools: () => ({
    carries: (ids: readonly string[] | null | undefined, id: string) => (ids ?? []).includes(id),
    canonicalIds: (ids: readonly string[] | null | undefined) => [...(ids ?? [])],
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
vi.mock("@/hooks/api/useAuthApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAuthApi")>()),
  useMe: () => ({
    data: {
      id: "u1",
      ga_builtin_tool_ids: null,
      ga_integration_bindings: null,
      ga_mcp_server_ids: null,
      ga_custom_tool_ids: null,
      ga_knowledge_base_ids: null,
      ga_browser_profile_ids: null,
    },
  }),
  useUpdateProfile: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/api/useSettingsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useSettingsApi")>()),
  useGeneralAssistantConfig: () => ({ data: { builtin_tool_ids: ["web_fetch"] } }),
}));
vi.mock("@/hooks/api/useIntegrationApi", () => ({ useIntegrations: () => ({ data: [] }) }));
vi.mock("@/hooks/api/useMCPServerApi", () => ({ useMCPServers: () => ({ data: [] }) }));
vi.mock("@/hooks/api/useCustomToolApi", () => ({ useCustomTools: () => ({ data: [] }) }));
vi.mock("@/hooks/api/useKnowledgeBaseApi", () => ({ useBindableKnowledgeBases: () => ({ data: [] }) }));
vi.mock("@/hooks/api/useBrowserProfilesApi", () => ({ useBrowserProfiles: () => ({ data: [] }) }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useCustomToolsEnabled: () => false,
}));

describe("Profile, General Assistant tools, before the registry is known", () => {
  beforeEach(() => {
    state.registry = { isReady: false, isError: false };
  });

  it("shows a skeleton under Built-in Tools while the registry loads", () => {
    renderWithProviders(<GAToolsSection />);
    const loading = screen.getByTestId("profile-builtin-tools-loading");
    expect(loading.getAttribute("aria-busy")).toBe("true");
    // List rows like the loaded group, not picker cards.
    expect(loading.getAttribute("data-variant")).toBe("rows");
    expect(screen.getAllByTestId("profile-builtin-tools-loading-row")).toHaveLength(3);
    expect(screen.queryByText("No matching tools.")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows an inline error line when the registry read failed", () => {
    state.registry = { isReady: false, isError: true };
    renderWithProviders(<GAToolsSection />);
    expect(screen.getByTestId("profile-builtin-tools-error").textContent).toContain("Built-in tools could not be loaded");
    expect(screen.queryByTestId("profile-builtin-tools-loading")).toBeNull();
  });

  it("lists the offered suites once the registry is loaded", () => {
    state.registry = { isReady: true, isError: false };
    renderWithProviders(<GAToolsSection />);
    expect(screen.queryByTestId("profile-builtin-tools-loading")).toBeNull();
    expect(screen.getByText("Web Fetch")).not.toBeNull();
  });
});
