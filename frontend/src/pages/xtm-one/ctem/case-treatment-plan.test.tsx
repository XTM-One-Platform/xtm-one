// The OpenCRQ treatment plan on a case (CTEM-18-UI, #1068): the plan's name
// linked to OpenCRQ, a restricted line for a viewer who does not read OpenCRQ,
// and an empty state that is never blank.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { CaseTreatmentPlan } from "./CaseTreatmentPlan";

const URL = "https://crq.example.test/t1/treatment-plans/4f1c2a8e-6b7d-4e1a-9c3b-2d5e8f7a1b90";

describe("CaseTreatmentPlan", () => {
  it("links the plan's name to OpenCRQ", () => {
    renderWithProviders(
      <CaseTreatmentPlan plan={{ restricted: false, id: "4f1c", name: "Upgrade nginx before January", url: URL, at: "2026-11-22T03:00:00Z" }} />,
    );
    const link = screen.getByTestId("ctem-case-treatment-plan-link");
    expect(link.getAttribute("href")).toBe(URL);
    expect(link.getAttribute("target")).toBe("_blank");
    expect(screen.getByText("Upgrade nginx before January")).toBeInTheDocument();
    expect(screen.getByText(/Attached on/)).toBeInTheDocument();
  });

  it("never links an unsafe url", () => {
    renderWithProviders(<CaseTreatmentPlan plan={{ restricted: false, id: "x", name: "P", url: "javascript:alert(1)", at: null }} />);
    expect(screen.queryByTestId("ctem-case-treatment-plan-link")).toBeNull();
    expect(screen.getByText("P")).toBeInTheDocument();
  });

  it("shows a restricted plan without its name", () => {
    renderWithProviders(<CaseTreatmentPlan plan={{ restricted: true, at: "2026-11-22T03:00:00Z" }} />);
    expect(screen.getByText(/restricted to OpenCRQ readers/)).toBeInTheDocument();
    expect(screen.queryByTestId("ctem-case-treatment-plan-link")).toBeNull();
  });

  it("says when a plan appears when there is none", () => {
    renderWithProviders(<CaseTreatmentPlan plan={null} />);
    expect(screen.getByText(/once the case is validated|once the case is prioritized|writes in OpenCRQ/)).toBeInTheDocument();
  });
});
