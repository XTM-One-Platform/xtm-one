// SPDX-License-Identifier: Apache-2.0
// The admin Delete of the Cases list (#1723): drawn for an administrator only; the
// click asks the server for a dry run first and the dialog names what would go.

import { describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseDeleteResult } from "@/hooks/api/useCtemCasesApi";
import { CasesDeleteAction, useCaseDelete } from "./CasesDeleteAction";
import { deletePreviewTotals } from "./cases-delete";

const state = vi.hoisted(() => ({
  isAdmin: true,
  calls: [] as { case_ids: string[]; dry_run: boolean }[],
}));

const PREVIEW: CtemCaseDeleteResult = {
  dry_run: true,
  count: 2,
  unknown: [],
  cases: [
    { id: "c1", title: "Upgrade Apache", state: "new", opened_by: "legacy", members: 3, events: 4, work_items: ["w1", "w2"], timers: ["t1"], signals: [] },
    { id: "c2", title: "Patch OpenSSL", state: "analyzing", opened_by: "signal", members: 1, events: 2, work_items: [], timers: [], signals: ["s1"] },
  ],
};

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: state.isAdmin }) }));
vi.mock("@/hooks/api/useCtemCasesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemCasesApi")>()),
  useDeleteCtemCases: () => ({
    isPending: false,
    mutate: (body: { case_ids: string[]; dry_run: boolean }, opts: { onSuccess: (r: CtemCaseDeleteResult) => void }) => {
      state.calls.push(body);
      opts.onSuccess({ ...PREVIEW, dry_run: body.dry_run });
    },
  }),
}));

describe("Delete on the Cases list", () => {
  it("adds up the dry run's work items, timers and signals", () => {
    expect(deletePreviewTotals(PREVIEW)).toEqual({ workItems: 2, timers: 1, signals: 1 });
  });

  it("draws nothing for a non-administrator", () => {
    state.isAdmin = false;
    renderWithProviders(<CasesDeleteAction caseIds={["c1"]} onDone={() => undefined} />);
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    state.isAdmin = true;
  });

  it("asks for a dry run first and shows what the delete removes", async () => {
    state.calls = [];
    renderWithProviders(<CasesDeleteAction caseIds={["c1", "c2"]} onDone={() => undefined} />);
    await act(async () => screen.getByTestId("ctem-cases-bulk-delete").click());
    expect(state.calls).toEqual([{ case_ids: ["c1", "c2"], dry_run: true }]);
    const preview = await screen.findByTestId("ctem-cases-delete-preview");
    expect(preview).toHaveTextContent("2 cases and their history");
    expect(preview).toHaveTextContent("2 open work items cancelled");
    expect(preview).toHaveTextContent("1 timer cancelled");
    expect(preview).toHaveTextContent("1 signal sent back to the scope gate");
  });
});

// The case page's More actions row (#1950) runs the same flow through useCaseDelete.
describe("Delete on the case page", () => {
  it("is not offered to a non-administrator", () => {
    state.isAdmin = false;
    try {
      function Probe() {
        const remove = useCaseDelete(["c1"], () => undefined);
        return <span data-testid="probe">{remove.allowed ? "allowed" : "refused"}</span>;
      }
      renderWithProviders(<Probe />);
      expect(screen.getByTestId("probe")).toHaveTextContent("refused");
    } finally {
      state.isAdmin = true;
    }
  });

  it("asks an administrator's dry run for the case before the delete", async () => {
    state.calls = [];
    const done = vi.fn();
    function Page() {
      const remove = useCaseDelete(["c1"], done);
      return (
        <>
          <button type="button" onClick={remove.ask}>
            Delete
          </button>
          {remove.dialog}
        </>
      );
    }
    renderWithProviders(<Page />);
    await act(async () => screen.getByRole("button", { name: "Delete" }).click());
    expect(state.calls).toEqual([{ case_ids: ["c1"], dry_run: true }]);
    expect(await screen.findByTestId("ctem-cases-delete-preview")).toHaveTextContent("2 cases and their history");
  });
});
