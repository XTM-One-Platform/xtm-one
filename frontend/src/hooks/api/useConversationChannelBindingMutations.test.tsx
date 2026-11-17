// What continuing a conversation in Slack or Teams, or stopping, refreshes.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { QueryClient } from "@tanstack/react-query";

import { useConversationChannelBindingMutations } from "@/hooks/api/useChatApi";
import { api } from "@/lib/api";
import { testQueryClient, withProviders } from "@/testing/providers";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
}));

const post = vi.mocked(api.post);
const remove = vi.mocked(api.delete);

const NOW_CONTINUED = ["conversation", "c-1"];
const CONTINUED_UNTIL_NOW = ["conversation", "c-0"];
const KEYS = [
  NOW_CONTINUED,
  CONTINUED_UNTIL_NOW,
  ["conversation-channel-bindings", "c-1"],
  ["conversation-channel-bindings", "c-0"],
];
const LIST = ["conversations", { scope: "own" }];

function seeded(): QueryClient {
  const client = testQueryClient();
  for (const key of [...KEYS, LIST]) client.setQueryData(key, { seeded: true });
  return client;
}

const invalidated = (client: QueryClient, key: unknown[]) => client.getQueryState(key)?.isInvalidated ?? false;

beforeEach(() => {
  post.mockReset();
  remove.mockReset();
});

describe("useConversationChannelBindingMutations", () => {
  it("binding a Teams personal chat refreshes the conversation it was taken from too", async () => {
    const client = seeded();
    post.mockResolvedValue({ channel: "teams", created_at: "", updated_at: "" });
    const { result } = renderHook(() => useConversationChannelBindingMutations("c-1"), {
      wrapper: withProviders(client),
    });

    await act(() => result.current.continueIn.mutateAsync("teams"));

    expect(post).toHaveBeenCalledWith("/chat/conversations/c-1/channel-bindings", { channel: "teams" });
    for (const key of KEYS) expect(invalidated(client, key), JSON.stringify(key)).toBe(true);
    // The list shows where a conversation started, never where it is continued.
    expect(invalidated(client, LIST)).toBe(false);
  });

  it("stopping refreshes the same", async () => {
    const client = seeded();
    remove.mockResolvedValue(undefined);
    const { result } = renderHook(() => useConversationChannelBindingMutations("c-1"), {
      wrapper: withProviders(client),
    });

    await act(() => result.current.stopIn.mutateAsync("slack"));

    expect(remove).toHaveBeenCalledWith("/chat/conversations/c-1/channel-bindings/slack");
    for (const key of KEYS) expect(invalidated(client, key), JSON.stringify(key)).toBe(true);
    expect(invalidated(client, LIST)).toBe(false);
  });

  it("a refused bind refreshes nothing", async () => {
    const client = seeded();
    post.mockRejectedValue(new Error("The Slack bot does not answer direct messages."));
    const { result } = renderHook(() => useConversationChannelBindingMutations("c-1"), {
      wrapper: withProviders(client),
    });

    await act(() => expect(result.current.continueIn.mutateAsync("slack")).rejects.toThrow("direct messages"));

    for (const key of [...KEYS, LIST]) expect(invalidated(client, key), JSON.stringify(key)).toBe(false);
  });
});
