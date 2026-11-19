// The flow list takes the entity list scope like the agent list: an admin's
// picker over every flow (Settings > Agents and automations > Platform Health) reads `scope=all`.

import { describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { testQueryClient, withProviders } from "@/testing/providers";
import { useFlows } from "./useFlowApi";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn().mockResolvedValue([]), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "admin" }, isAdmin: true }) }));

describe("useFlows", () => {
  it("reads every flow for an admin who asks for all of them", async () => {
    renderHook(() => useFlows({ scope: "all" }), { wrapper: withProviders(testQueryClient()) });
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/flows?scope=all"));
  });

  it("reads the library set by default", async () => {
    renderHook(() => useFlows(), { wrapper: withProviders(testQueryClient()) });
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/flows"));
  });
});
