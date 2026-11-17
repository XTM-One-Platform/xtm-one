// A purge of one's own memory also deletes the consolidation history of those
// lanes: the profile reads both again, so no purged text or dead Undo stays.

import { describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { testQueryClient, withProviders } from "@/testing/providers";
import { usePurgeMyAgentMemory, usePurgeMyMemory, usePurgeMyUserProfile } from "./useSettingsApi";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn().mockResolvedValue({}) },
}));

function seeded() {
  const client = testQueryClient();
  client.setQueryData(["my-memory"], {});
  client.setQueryData(["my-memory-consolidations"], { items: [] });
  const stale = (key: string) => client.getQueryState([key])?.isInvalidated;
  return { client, stale };
}

describe("memory purges on the profile", () => {
  const purges: [string, () => () => Promise<unknown>][] = [
    ["every memory", () => usePurgeMyMemory().mutateAsync],
    ["the user profile", () => usePurgeMyUserProfile().mutateAsync],
    ["one agent's memory", () => { const purge = usePurgeMyAgentMemory(); return () => purge.mutateAsync("a-1"); }],
  ];

  it.each(purges)("reads the consolidation history again after purging %s", async (_name, hook) => {
    const { client, stale } = seeded();
    const { result } = renderHook(hook, { wrapper: withProviders(client) });

    await act(() => result.current());
    expect([stale("my-memory"), stale("my-memory-consolidations")]).toEqual([true, true]);
  });
});
