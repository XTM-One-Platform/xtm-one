// Moving a home member to another fleet is one server call: the
// transaction lives in `move_member()`, so the hook never deletes, re-adds or
// restores anything itself.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useMoveFleetMember, type FleetMember } from "@/hooks/api/useFleetApi";
import { api } from "@/lib/api";
import { testQueryClient, withProviders } from "@/testing/providers";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const post = vi.mocked(api.post);
const put = vi.mocked(api.put);
const remove = vi.mocked(api.delete);

const SOURCE = "f-source";
const TARGET = "f-target";

function member(extra: Partial<FleetMember> = {}): FleetMember {
  return {
    id: "m1",
    fleet_id: SOURCE,
    role: "member",
    agent_id: "agent-1",
    name: "Analyst",
    target_fleet_id: null,
    parent_member_id: "m0",
    is_orchestrator: true,
    is_exposed: true,
    exposed_explicitly: true,
    role_label: "Release manager",
    collaboration_instruction: "",
    context_inclusion: "none",
    created_at: "2026-11-01T00:00:00Z",
    ...extra,
  };
}

beforeEach(() => {
  post.mockReset();
  put.mockReset();
  remove.mockReset();
});

describe("useMoveFleetMember", () => {
  it("calls the move endpoint once and answers the row in the other fleet", async () => {
    const client = testQueryClient();
    client.setQueryData(["fleets"], { seeded: true });
    post.mockResolvedValue(member({ id: "m2", fleet_id: TARGET, is_orchestrator: false }));
    const { result } = renderHook(() => useMoveFleetMember(SOURCE), { wrapper: withProviders(client) });

    const moved = await act(() => result.current.mutateAsync({ member: member(), targetFleetId: TARGET }));

    expect(moved.fleet_id).toBe(TARGET);
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith(`/fleets/${SOURCE}/members/m1/move`, { target_fleet_id: TARGET });
    expect(remove).not.toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
    expect(client.getQueryState(["fleets"])?.isInvalidated).toBe(true);
  });

  it("a refused move is the server's error, with no client-side way back", async () => {
    const client = testQueryClient();
    post.mockRejectedValue(new Error("This fleet is archived: restore it to change its members."));
    const { result } = renderHook(() => useMoveFleetMember(SOURCE), { wrapper: withProviders(client) });

    await expect(
      act(() => result.current.mutateAsync({ member: member(), targetFleetId: TARGET })),
    ).rejects.toThrow("This fleet is archived: restore it to change its members.");
    // Nothing re-adds the member or sets the orchestrator back: the server never moved it.
    expect(post).toHaveBeenCalledTimes(1);
    expect(remove).not.toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
  });

  it("a refused move raises no global error toast: the panel's toast is the only one", async () => {
    const client = testQueryClient();
    post.mockRejectedValue(new Error("This fleet is archived: restore it to change its members."));
    const { result } = renderHook(() => useMoveFleetMember(SOURCE), { wrapper: withProviders(client) });

    await expect(
      act(() => result.current.mutateAsync({ member: member(), targetFleetId: TARGET })),
    ).rejects.toThrow();
    const [mutation] = client.getMutationCache().getAll();
    expect(mutation.meta).toEqual({ suppressGlobalErrorToast: true });
  });
});
