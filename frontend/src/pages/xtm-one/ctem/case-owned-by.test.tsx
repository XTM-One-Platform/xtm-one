// The case page names the fleet that runs the case (#1390): "Owned by <fleet>"
// linked to the fleet page, or "No active fleet owns this case".

import { describe, expect, it } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import { CaseOwnedBy } from "./CaseOwnedBy";
import { ownerFleetId } from "./case-loop-reason";

const LOOP = { id: "f-loop", slug: "ctem-loop", is_builtin: true, is_archived: false, forked_from: null };
const VULN = { id: "f-vuln", slug: "vulnerability-management", is_builtin: true, is_archived: false, forked_from: null };

function FleetPage() {
  const state = useLocation().state as { backTo?: string; backLabel?: string } | null;
  return <p data-testid="fleet-back">{`${state?.backLabel ?? ""} ${state?.backTo ?? ""}`}</p>;
}

describe("CaseOwnedBy", () => {
  it("names the owning fleet and links to its page", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseOwnedBy owner={{ slug: "ctem-loop", name: "CTEM loop", active: true }} fleets={[VULN, LOOP]} caseId="c-1" />
      </MemoryRouter>,
    );
    const link = screen.getByRole("link", { name: "Owned by CTEM loop" });
    expect(link).toHaveAttribute("href", "/fleets/f-loop");
    expect(screen.queryByText("No active fleet owns this case")).toBeNull();
  });

  it("keeps the linked label and its icon in the product primary, visited included", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseOwnedBy owner={{ slug: "ctem-loop", name: "CTEM loop", active: true }} fleets={[LOOP]} caseId="c-1" />
      </MemoryRouter>,
    );
    const link = screen.getByRole("link", { name: "Owned by CTEM loop" });
    expect(link.className).toContain("text-filigran-brand-primary");
    expect(link.className).toContain("visited:text-filigran-brand-primary");
    // The library's visited colour is the tonic accent (turquoise): never in the header.
    expect(link.className).not.toContain("visited:text-filigran-tonic-accent");
    const icon = screen.getByTestId("ctem-case-owner-fleet").querySelector("svg");
    expect(icon?.getAttribute("class") ?? "").toContain("text-filigran-brand-primary");
  });

  it("gives the fleet page a way back to the case", () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/ctem/cases/c-1"]}>
        <Routes>
          <Route
            path="/ctem/cases/:id"
            element={<CaseOwnedBy owner={{ slug: "ctem-loop", name: "CTEM loop", active: true }} fleets={[LOOP]} caseId="c-1" />}
          />
          <Route path="/fleets/:id" element={<FleetPage />} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("link", { name: "Owned by CTEM loop" }));
    expect(screen.getByTestId("fleet-back")).toHaveTextContent("Back to Case /ctem/cases/c-1");
  });

  it("names the owner without a link when the reader does not see the fleet", () => {
    renderWithProviders(<CaseOwnedBy owner={{ slug: "ctem-loop", name: "CTEM loop", active: true }} fleets={[VULN]} caseId="c-1" />);
    expect(screen.getByText("Owned by CTEM loop")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("says no active fleet owns the case when the owner is inactive", () => {
    renderWithProviders(<CaseOwnedBy owner={{ slug: "ctem-loop", name: "CTEM loop", active: false }} fleets={[LOOP]} caseId="c-1" />);
    expect(screen.getByText("No active fleet owns this case")).toBeInTheDocument();
    expect(screen.queryByText(/Owned by/)).toBeNull();
  });

  it("says no active fleet owns the case when the API names none", () => {
    renderWithProviders(<CaseOwnedBy owner={null} fleets={[LOOP]} caseId="c-1" />);
    expect(screen.getByText("No active fleet owns this case")).toBeInTheDocument();
  });

  it("draws nothing on an API older than the owner field", () => {
    renderWithProviders(<CaseOwnedBy owner={undefined} fleets={[LOOP]} caseId="c-1" />);
    expect(screen.queryByTestId("ctem-case-owner-fleet")).toBeNull();
  });
});

describe("ownerFleetId", () => {
  it("prefers the seeded fleet, then a fork of it, and skips archived fleets", () => {
    const fork = { id: "f-fork", slug: "my-loop", is_builtin: false, is_archived: false, forked_from: { slug: "ctem-loop", is_builtin: true } };
    expect(ownerFleetId("ctem-loop", [fork, LOOP])).toBe("f-loop");
    expect(ownerFleetId("ctem-loop", [fork])).toBe("f-fork");
    expect(ownerFleetId("ctem-loop", [{ ...LOOP, is_archived: true }])).toBeNull();
  });
});
