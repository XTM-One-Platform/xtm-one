// The OpenCRQ treatment plan on a case (CTEM-18-UI, #1068): the plan's name
// linked to OpenCRQ, a restricted line for a viewer who does not read OpenCRQ,
// and the card left out of the page while no plan is attached (#2786), unless
// OpenCRQ is not connected.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import { CaseLoopProvider } from "./CaseSectionReason";
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

  it("is left out of the page when there is no plan and OpenCRQ is connected or unknown (#2786)", () => {
    const { unmount } = renderWithProviders(<CaseTreatmentPlan plan={null} />);
    expect(screen.queryByTestId("ctem-case-treatment-plan")).toBeNull();
    unmount();
    renderWithProviders(
      <CaseLoopProvider value={{ sources: { opencrq: true, opencti: true, openaev: true } }}>
        <CaseTreatmentPlan plan={null} />
      </CaseLoopProvider>,
    );
    expect(screen.queryByTestId("ctem-case-treatment-plan")).toBeNull();
  });

  it("says when a plan appears, with Connect, when OpenCRQ is not connected (#2786)", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseLoopProvider value={{ sources: { opencrq: false, opencti: true, openaev: true } }}>
          <CaseTreatmentPlan plan={null} />
        </CaseLoopProvider>
      </MemoryRouter>,
    );
    expect(screen.getByText(/writes in OpenCRQ/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect OpenCRQ" })).toBeInTheDocument();
  });
});
