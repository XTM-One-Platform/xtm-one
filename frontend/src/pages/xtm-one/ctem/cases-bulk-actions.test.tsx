// The Start bulk action of the Cases list (S5c of #11545, retest of #11666):
// Start opens no dialog, so when every selected case is refused the refusal is
// an error toast naming each case with its rule, and the selection is kept.

import { describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { CasesBulkActions } from "./CasesBulkActions";

const state = vi.hoisted(() => ({
  results: [] as { updated: string[]; failed: { id: string; reason: string }[] }[],
}));

vi.mock("@/hooks/api/useAuthApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAuthApi")>()),
  useMe: () => ({ data: { id: "u1" } }),
  useUserDirectory: () => ({ data: [] }),
}));

vi.mock("@/hooks/api/useCtemCasesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemCasesApi")>()),
  useBulkUpdateCtemCases: () => ({
    isPending: false,
    mutate: () => undefined,
    mutateAsync: () => Promise.resolve(state.results.shift() ?? { updated: [], failed: [] }),
  }),
  useMergeCtemCases: () => ({ isPending: false, mutate: () => undefined }),
}));

const CASES = [
  { id: "c1", title: "Upgrade Apache HTTP Server to 2.4.62", owner_user_id: null },
  { id: "c2", title: "Patch OpenSSL", owner_user_id: "u2" },
] as unknown as CtemCaseSummary[];

describe("Start on the Cases list", () => {
  it("shows a refused toast with each case and its rule when every case is refused", async () => {
    state.results = [
      { updated: [], failed: [{ id: "c1", reason: "a resolved case cannot start" }] },
      { updated: [], failed: [{ id: "c2", reason: "an accepted risk needs its expiry first" }] },
    ];
    const onDone = vi.fn();
    renderWithProviders(<CasesBulkActions cases={CASES} onDone={onDone} />);
    await act(async () => screen.getByRole("button", { name: "Start" }).click());
    expect(await screen.findByText("2 cases refused")).toBeInTheDocument();
    const toast = screen.getByRole("region", { name: /Notifications/ });
    expect(toast).toHaveTextContent("Upgrade Apache HTTP Server to 2.4.62: a resolved case cannot start");
    expect(toast).toHaveTextContent("Patch OpenSSL: an accepted risk needs its expiry first");
    expect(onDone).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("clears the selection with an updated toast when every case starts", async () => {
    state.results = [
      { updated: ["c1"], failed: [] },
      { updated: ["c2"], failed: [] },
    ];
    const onDone = vi.fn();
    renderWithProviders(<CasesBulkActions cases={CASES} onDone={onDone} />);
    await act(async () => screen.getByRole("button", { name: "Start" }).click());
    expect(await screen.findByText("2 cases updated")).toBeInTheDocument();
    expect(onDone).toHaveBeenCalledTimes(1);
  });
});
