// Deleting CTEM cases: a refused delete (a 409 while a recompute holds the lock, a 500) is
// shown once, by the caller (the dialog line or the "Delete failed" toast). The app
// MutationCache is mounted so its global error toast would show here if it fired (#1950).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, screen } from "@testing-library/react";

import { useDeleteCtemCases } from "@/hooks/api/useCtemCasesApi";
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

describe("useDeleteCtemCases", () => {
  it("deletes and reads the cases again", async () => {
    const client = testQueryClient({ appMutationCache: true });
    client.setQueryData(["ctem-cases"], { seeded: true });
    post.mockResolvedValue({ dry_run: false, count: 1, cases: [], unknown: [] });
    const { result } = renderHook(() => useDeleteCtemCases(), { wrapper: withProviders(client) });

    await act(() => result.current.mutateAsync({ case_ids: ["c-1"], dry_run: false }));

    expect(post).toHaveBeenCalledWith("/ctem-cases/bulk-delete", { case_ids: ["c-1"], dry_run: false });
    expect(client.getQueryState(["ctem-cases"])?.isInvalidated).toBe(true);
  });

  it.each([
    ["409", "a case recompute is running for this organization, try again in a few seconds"],
    ["500", "500 Internal Server Error: database unavailable"],
  ])("a %s raises no global error toast, the caller shows it once", async (_status, detail) => {
    const client = testQueryClient({ appMutationCache: true });
    post.mockRejectedValue(new Error(detail));
    const { result } = renderHook(() => useDeleteCtemCases(), { wrapper: withProviders(client) });

    await expect(
      act(() => result.current.mutateAsync({ case_ids: ["c-1", "c-2"], dry_run: false })),
    ).rejects.toThrow(detail);

    const [mutation] = client.getMutationCache().getAll();
    expect(mutation.meta).toEqual({ suppressGlobalErrorToast: true });
    expect(screen.queryByText(detail)).toBeNull();
  });
});
