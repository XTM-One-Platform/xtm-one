// Applying a good-practice suggestion changes the member's handovers: the
// member's own agent query is refreshed with the lists, because the flow
// builder's agent node reads `["agent", id]` through `useAgent` (#12033).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useApplyFleetPracticeSuggestion } from "@/hooks/api/useFleetApi";
import { api } from "@/lib/api";
import { testQueryClient, withProviders } from "@/testing/providers";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const post = vi.mocked(api.post);
const FLEET = "f-1";

beforeEach(() => {
  post.mockReset();
});

describe("useApplyFleetPracticeSuggestion", () => {
  it("refreshes the member's agent query, so the flow node shows its new handover", async () => {
    const client = testQueryClient();
    client.setQueryData(["agent", "agent-9"], { id: "agent-9", handover_agent_ids: [] });
    client.setQueryData(["agent", "agent-other"], { id: "agent-other", handover_agent_ids: [] });
    client.setQueryData(["agents"], { seeded: true });
    post.mockResolvedValue({ findings: [] });
    const { result } = renderHook(() => useApplyFleetPracticeSuggestion(FLEET), { wrapper: withProviders(client) });

    await act(() => result.current.mutateAsync({ id: "bc1", agentId: "agent-9" }));

    expect(post).toHaveBeenCalledWith(`/fleets/${FLEET}/documentation/findings/bc1/apply`, {});
    expect(client.getQueryState(["agent", "agent-9"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["agents"])?.isInvalidated).toBe(true);
    // Another agent's query is left alone.
    expect(client.getQueryState(["agent", "agent-other"])?.isInvalidated).toBe(false);
  });

  it("refreshes the member's agent query after a refusal too: the server may have written it", async () => {
    const client = testQueryClient();
    client.setQueryData(["agent", "agent-9"], { id: "agent-9", handover_agent_ids: [] });
    post.mockRejectedValue(new Error("This suggestion no longer applies."));
    const { result } = renderHook(() => useApplyFleetPracticeSuggestion(FLEET), { wrapper: withProviders(client) });

    await expect(act(() => result.current.mutateAsync({ id: "bc1", agentId: "agent-9" }))).rejects.toThrow(
      "This suggestion no longer applies.",
    );
    expect(client.getQueryState(["agent", "agent-9"])?.isInvalidated).toBe(true);
  });

  it("without a member agent it still refreshes the lists", async () => {
    const client = testQueryClient();
    client.setQueryData(["agents"], { seeded: true });
    post.mockResolvedValue({ findings: [] });
    const { result } = renderHook(() => useApplyFleetPracticeSuggestion(FLEET), { wrapper: withProviders(client) });

    await act(() => result.current.mutateAsync({ id: "kb1" }));

    expect(client.getQueryState(["agents"])?.isInvalidated).toBe(true);
  });
});
