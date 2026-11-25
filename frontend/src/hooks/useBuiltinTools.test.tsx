// The built-in registry is served by `GET /settings/builtin-tools/groups`.
// Before it answers the catalog says so (`isLoading`), and a failed
// read is reported (`isError`) instead of reaching the page's error boundary,
// even under the app's global `throwOnError: true`.

import { describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { withProviders } from "@/testing/providers";
import { useBuiltinTools } from "./useBuiltinTools";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
const auth = vi.hoisted(() => ({ isAdmin: true }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "u1" }, isAdmin: auth.isAdmin }) }));
vi.mock("@/hooks/usePlatformConfig", () => ({
  useCodeInterpreterEnabled: () => true,
  usePlatformName: () => "XTM One",
}));
vi.mock("@/hooks/useDesktop", () => ({ useRunnersEnabled: () => true }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => true }));

const get = vi.mocked(api.get);

/** The app's own policy (main.tsx): every query throws to the boundary. */
function appClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity, throwOnError: true } },
  });
}

const GROUP = {
  id: "web_fetch",
  name: "Web Fetch",
  tagline: "Read a web page",
  detail: null,
  category: "Web",
  icon: "globe",
  gate: null,
  default_on: true,
  admin_only: false,
  agent_keyed: false,
  named_after_platform: false,
  functions: ["web_fetch"],
};

describe("useBuiltinTools registry states", () => {
  it("is loading, not ready, until the registry answers", async () => {
    let answer: (value: unknown) => void = () => {};
    get.mockReturnValueOnce(new Promise((resolve) => { answer = resolve; }) as never);
    const { result } = renderHook(() => useBuiltinTools(), { wrapper: withProviders(appClient()) });
    expect(result.current.isLoading).toBe(true);
    expect(result.current.isReady).toBe(false);
    expect(result.current.offered("agent")).toEqual([]);
    answer([GROUP]);
    await waitFor(() => expect(result.current.isReady).toBe(true));
    expect(result.current.isLoading).toBe(false);
    expect(result.current.describe("web_fetch").label).toBe("Web Fetch");
    expect(result.current.describe("web_fetch").known).toBe(true);
  });

  it("reports a failed read without throwing to the error boundary", async () => {
    get.mockRejectedValue(new Error("500"));
    const { result } = renderHook(() => useBuiltinTools(), { wrapper: withProviders(appClient()) });
    await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 5000 });
    expect(result.current.isLoading).toBe(false);
    expect(result.current.isReady).toBe(false);
  });
});

describe("useBuiltinTools administrator-bound suite", () => {
  const orgContext = { ...GROUP, id: "organization_context", name: "Organization Profile Editing", agent_keyed: true, bind_admin_only: true };

  it("is locked for a non-administrator and open for an administrator", async () => {
    auth.isAdmin = false;
    try {
      get.mockResolvedValueOnce([GROUP, orgContext] as never);
      const { result } = renderHook(() => useBuiltinTools(), { wrapper: withProviders(appClient()) });
      await waitFor(() => expect(result.current.isReady).toBe(true));
      expect(result.current.describe("organization_context").bindLocked).toBe(true);
      expect(result.current.describe("web_fetch").bindLocked).toBe(false);
    } finally {
      auth.isAdmin = true;
    }
    get.mockResolvedValueOnce([GROUP, orgContext] as never);
    const { result } = renderHook(() => useBuiltinTools(), { wrapper: withProviders(appClient()) });
    await waitFor(() => expect(result.current.isReady).toBe(true));
    expect(result.current.describe("organization_context").bindLocked).toBe(false);
  });
});

describe("useBuiltinTools Platform Health internal group (#1943)", () => {
  it("offers only Platform Health among the health groups: the internal group is on no Tools page, picker or chat", async () => {
    const specialists = {
      ...GROUP,
      id: "health_specialists",
      name: "Platform Health specialists",
      gate: "health",
      default_on: false,
      agent_keyed: true,
      internal: true,
    };
    const orchestrator = { ...specialists, id: "health_orchestrator", name: "Platform Health", internal: false };
    get.mockResolvedValueOnce([{ ...GROUP, internal: false }, specialists, orchestrator] as never);
    const { result } = renderHook(() => useBuiltinTools(), { wrapper: withProviders(appClient()) });
    await waitFor(() => expect(result.current.isReady).toBe(true));
    const agent = { builtin_tool_ids: ["health_specialists"], user_id: "u1", is_company_managed: false };
    expect(result.current.offered("agent").map((tool) => tool.id)).toEqual(["web_fetch", "health_orchestrator"]);
    expect(result.current.offeredInChat(true, agent).map((tool) => tool.id)).toEqual(["web_fetch", "health_orchestrator"]);
    expect(result.current.offeredInChat(false).map((tool) => tool.id)).toEqual(["web_fetch"]);
    // An agent's grid lists it only where the agent carries it (a fleet agent).
    expect(result.current.listedInAgentConfig([], []).map((tool) => tool.id)).toEqual(["web_fetch", "health_orchestrator"]);
    expect(result.current.listedInAgentConfig(["health_specialists"], []).map((tool) => tool.id)).toContain("health_specialists");
    expect(result.current.describe("health_specialists").internal).toBe(true);
  });
});
