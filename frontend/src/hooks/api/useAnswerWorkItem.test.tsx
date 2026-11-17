// Answering a work item's decision: a refused answer is toasted once, by the
// decision card, with a translated hint. The app MutationCache is mounted so
// its global error toast (the raw backend detail) would show here if it fired.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, screen } from "@testing-library/react";

import { useAnswerWorkItem } from "@/hooks/api/useWorkItemApi";
import { api } from "@/lib/api";
import { testQueryClient, withProviders } from "@/testing/providers";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const post = vi.mocked(api.post);

beforeEach(() => {
  post.mockReset();
});

describe("useAnswerWorkItem", () => {
  it("posts the answer and reads the work items again", async () => {
    const client = testQueryClient({ appMutationCache: true });
    client.setQueryData(["work-items"], { seeded: true });
    post.mockResolvedValue({ id: "wi-1" });
    const { result } = renderHook(() => useAnswerWorkItem(), { wrapper: withProviders(client) });

    await act(() => result.current.mutateAsync({ id: "wi-1", option: "B" }));

    expect(post).toHaveBeenCalledWith("/work-items/wi-1/answer", { option: "B" });
    expect(client.getQueryState(["work-items"])?.isInvalidated).toBe(true);
  });

  it.each([
    ["409", "this work item is not waiting for a person's answer"],
    ["500", "500 Internal Server Error: database unavailable"],
  ])("a %s raises no global error toast with the raw backend detail", async (_status, detail) => {
    const client = testQueryClient({ appMutationCache: true });
    post.mockRejectedValue(new Error(detail));
    const { result } = renderHook(() => useAnswerWorkItem(), { wrapper: withProviders(client) });

    await expect(act(() => result.current.mutateAsync({ id: "wi-1", option: "A" }))).rejects.toThrow(detail);

    const [mutation] = client.getMutationCache().getAll();
    expect(mutation.meta).toEqual({ suppressGlobalErrorToast: true });
    expect(screen.queryByText(detail)).toBeNull();
  });
});
