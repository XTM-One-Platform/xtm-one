// The evaluation's Dataset tab reads each linked dataset's family: a family change
// on either side reads the list again, so a conflict or a missing family shows at once.

import { describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { api } from "@/lib/api";
import { testQueryClient, withProviders } from "@/testing/providers";
import { useUpdateDataset } from "./useDatasetApi";
import { useUpdateEvaluation } from "./useEvaluationApi";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn().mockResolvedValue({}), patch: vi.fn(), delete: vi.fn() },
}));

function seeded() {
  const client = testQueryClient();
  client.setQueryData(["evaluation-datasets", "e-1"], []);
  client.setQueryData(["evaluation-datasets", "e-2"], []);
  const stale = (evaluationId: string) => client.getQueryState(["evaluation-datasets", evaluationId])?.isInvalidated;
  return { client, stale };
}

describe("linked datasets after a family change", () => {
  it("reads the evaluation's linked datasets again once the evaluation is saved", async () => {
    const { client, stale } = seeded();
    const { result } = renderHook(() => useUpdateEvaluation(), { wrapper: withProviders(client) });

    await act(() => result.current.mutateAsync({ id: "e-1", family: "PROSE" }));
    expect(api.put).toHaveBeenCalledWith("/evaluations/e-1", { family: "PROSE" });
    expect([stale("e-1"), stale("e-2")]).toEqual([true, false]);
  });

  it("reads every evaluation's linked datasets again once a dataset is saved", async () => {
    const { client, stale } = seeded();
    const { result } = renderHook(() => useUpdateDataset(), { wrapper: withProviders(client) });

    await act(() => result.current.mutateAsync({ id: "d-1", family: "PROSE" }));
    expect([stale("e-1"), stale("e-2")]).toEqual([true, true]);
  });
});
