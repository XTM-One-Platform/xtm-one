// "Opened by" on a case no signal opened (#1529): a case is opened only by a
// signal or split by a person or an agent; a source read before signals is
// labelled legacy, never a reader sentence.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";

vi.mock("@/hooks/api/useCtemSignalsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemSignalsApi")>()),
  useCtemCaseSignals: () => ({ data: { signals: [] }, isLoading: false, isError: false }),
}));

const { CaseOpenedBy } = await import("./CaseOpenedBy");

const created = (to: string | null, reason: string | null = null) => ({ at: null, actor: "system", kind: "created", issue: null, from: null, to, reason, evidence: null });

describe("CaseOpenedBy", () => {
  it("labels a case a source read built before signals as legacy", () => {
    renderWithProviders(<MemoryRouter><CaseOpenedBy caseId="c1" history={[created("upgrade:fortios")]} /></MemoryRouter>);
    expect(screen.getByText("A source read before signals (legacy)")).toBeInTheDocument();
    expect(screen.queryByText(/matched to your inventory/)).toBeNull();
  });

  it("names the split of a case a person or an agent made", () => {
    renderWithProviders(<MemoryRouter><CaseOpenedBy caseId="c1" history={[created("manual")]} /></MemoryRouter>);
    expect(screen.getByText("Split into its own case by a person or an agent")).toBeInTheDocument();
  });

  it("says a signal opened the case when the reader cannot see that signal, never legacy", () => {
    renderWithProviders(<MemoryRouter><CaseOpenedBy caseId="c1" history={[created("upgrade:fortios", "signal 057f8fb5")]} /></MemoryRouter>);
    expect(screen.getByText("A signal you cannot see")).toBeInTheDocument();
    expect(screen.queryByText(/legacy/)).toBeNull();
  });
});
