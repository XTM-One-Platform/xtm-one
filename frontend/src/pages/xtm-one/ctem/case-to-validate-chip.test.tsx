// The "To validate" marker of a version-blind case (#2004): shown only when the
// running version OpenCTI cannot place, with the reason as its title; never in
// place of the priority, which the case keeps.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { CaseToValidateChip } from "./cases-shared";

describe("CaseToValidateChip", () => {
  it("says the case is to validate when the running version is not verified", () => {
    renderWithProviders(<CaseToValidateChip unverified />);
    const chip = screen.getByTestId("ctem-case-to-validate");
    expect(chip).toHaveTextContent("To validate");
    expect(chip).toHaveAttribute("title", "Running version not verified: validate it before acting on the case.");
  });

  it("draws nothing when every running version is placed", () => {
    renderWithProviders(<CaseToValidateChip unverified={false} />);
    expect(screen.queryByTestId("ctem-case-to-validate")).toBeNull();
    renderWithProviders(<CaseToValidateChip />);
    expect(screen.queryByTestId("ctem-case-to-validate")).toBeNull();
  });

  it("reads in French", () => {
    renderWithProviders(<CaseToValidateChip unverified />, { locale: "fr" });
    expect(screen.getByTestId("ctem-case-to-validate")).toHaveTextContent("À valider");
  });
});
