// "Opened by" on a case the source readers built (#1206, slice D): it names
// the reader that opened it, never "No signal opened this case" alone.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";

vi.mock("@/hooks/api/useCtemSignalsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemSignalsApi")>()),
  useCtemCaseSignals: () => ({ data: { signals: [] }, isLoading: false, isError: false }),
}));

const { CaseOpenedBy } = await import("./CaseOpenedBy");

describe("CaseOpenedBy", () => {
  it("names the OpenCTI reader on a case with no signal", () => {
    renderWithProviders(<CaseOpenedBy caseId="c1" sources={["opencti", "opencti"]} />);
    expect(screen.getByText("Opened from OpenCTI vulnerabilities matched to your inventory")).toBeInTheDocument();
    expect(screen.queryByText(/No signal opened this case/)).toBeNull();
  });

  it("keeps the generic line when no issue is visible", () => {
    renderWithProviders(<CaseOpenedBy caseId="c1" sources={[]} />);
    expect(screen.getByText(/No signal opened this case/)).toBeInTheDocument();
  });
});
