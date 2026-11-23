// The case page names the fleet that runs the case (#1390): "Owned by <fleet>"
// linked to the fleet page, or "No active fleet owns this case".

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import { CaseOwnedBy } from "./CaseOwnedBy";
import { ownerFleetId } from "./case-loop-reason";

const LOOP = { id: "f-loop", slug: "ctem-loop", is_builtin: true, is_archived: false, forked_from: null };
const VULN = { id: "f-vuln", slug: "vulnerability-management", is_builtin: true, is_archived: false, forked_from: null };

describe("CaseOwnedBy", () => {
  it("names the owning fleet and links to its page", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseOwnedBy owner={{ slug: "ctem-loop", name: "CTEM loop", active: true }} fleets={[VULN, LOOP]} />
      </MemoryRouter>,
    );
    const link = screen.getByRole("link", { name: "Owned by CTEM loop" });
    expect(link).toHaveAttribute("href", "/fleets/f-loop");
    expect(screen.queryByText("No active fleet owns this case")).toBeNull();
  });

  it("names the owner without a link when the reader does not see the fleet", () => {
    renderWithProviders(<CaseOwnedBy owner={{ slug: "ctem-loop", name: "CTEM loop", active: true }} fleets={[VULN]} />);
    expect(screen.getByText("Owned by CTEM loop")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("says no active fleet owns the case when the owner is inactive", () => {
    renderWithProviders(<CaseOwnedBy owner={{ slug: "ctem-loop", name: "CTEM loop", active: false }} fleets={[LOOP]} />);
    expect(screen.getByText("No active fleet owns this case")).toBeInTheDocument();
    expect(screen.queryByText(/Owned by/)).toBeNull();
  });

  it("says no active fleet owns the case when the API names none", () => {
    renderWithProviders(<CaseOwnedBy owner={null} fleets={[LOOP]} />);
    expect(screen.getByText("No active fleet owns this case")).toBeInTheDocument();
  });

  it("draws nothing on an API older than the owner field", () => {
    renderWithProviders(<CaseOwnedBy owner={undefined} fleets={[LOOP]} />);
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
