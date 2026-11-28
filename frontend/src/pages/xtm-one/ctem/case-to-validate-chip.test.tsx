// The "To validate" marker of a version-blind case (#2004): shown only when the
// running version OpenCTI cannot place, with the reason in the DS tooltip on
// hover and keyboard focus, never the native title (UX round 1 B1 on #2007);
// never in place of the priority, which the case keeps; never on a closed case,
// which nobody works any more (#2731).

import { describe, expect, it } from "vitest";
import { act, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { CTEM_OPEN_CASE_STATES } from "@/hooks/api/useCtemCasesApi";
import { CaseToValidateChip } from "./cases-shared";

describe("CaseToValidateChip", () => {
  it("says the case is to validate, the reason in the DS tooltip on focus", async () => {
    renderWithProviders(<CaseToValidateChip unverified state="new" />);
    const chip = screen.getByTestId("ctem-case-to-validate");
    expect(chip).toHaveTextContent("To validate");
    expect(chip).not.toHaveAttribute("title");
    expect(chip.querySelector("[title]")).toBeNull();
    expect(chip).toHaveAttribute("tabindex", "0");
    act(() => chip.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Running version not verified: validate it before acting on the case.");
  });

  it("draws nothing when every running version is placed", () => {
    renderWithProviders(<CaseToValidateChip unverified={false} state="analyzing" />);
    expect(screen.queryByTestId("ctem-case-to-validate")).toBeNull();
    renderWithProviders(<CaseToValidateChip state="analyzing" />);
    expect(screen.queryByTestId("ctem-case-to-validate")).toBeNull();
  });

  it.each(CTEM_OPEN_CASE_STATES)("shows on an open %s case", (state) => {
    renderWithProviders(<CaseToValidateChip unverified state={state} />);
    expect(screen.getByTestId("ctem-case-to-validate")).toHaveTextContent("To validate");
  });

  it.each(["mitigated_watching", "resolved", "accepted_risk"] as const)("draws nothing on a closed %s case", (state) => {
    renderWithProviders(<CaseToValidateChip unverified state={state} />);
    expect(screen.queryByTestId("ctem-case-to-validate")).toBeNull();
  });

  it("reads in French", () => {
    renderWithProviders(<CaseToValidateChip unverified state="new" />, { locale: "fr" });
    expect(screen.getByTestId("ctem-case-to-validate")).toHaveTextContent("À valider");
  });
});
