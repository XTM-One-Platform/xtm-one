// Attack path and blast radius on a case (CTEM-23-UI, #1289): the hops nearest
// first with proven or inferred, the primary assets reached, a restricted
// reading for a viewer who does not read OpenCRQ, and empty states that are
// never blank.

import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseAttackPath } from "@/hooks/api/useCtemCasesApi";
import { CaseAttackPath } from "./CaseAttackPath";

const READING: CtemCaseAttackPath = {
  origin: { asset: "Edge bastion", exposure: "internet_facing" },
  hops: [
    { from: "Edge bastion", to: "Jump host", nature: "proven", simulation: "sim-2026-11-20" },
    { from: "Jump host", to: "Core banking", nature: "inferred", simulation: null },
  ],
  reached: [
    { asset: "Jump host", depth: 1, criticality: "medium" },
    { asset: "Core banking", depth: 2, criticality: "very_high" },
  ],
  primary: ["Credit decision"],
  at: "2026-11-22T21:00:00Z",
};

describe("CaseAttackPath", () => {
  it("lists the hops nearest first, proven or inferred, and the primary assets reached", () => {
    renderWithProviders(<CaseAttackPath reading={READING} />);
    expect(screen.getByText("Attack path and blast radius")).toBeInTheDocument();
    expect(screen.getByText("Internet-facing")).toBeInTheDocument();
    const hops = screen.getAllByTestId("ctem-case-attack-path-hop");
    expect(hops).toHaveLength(2);
    expect(within(hops[0]).getByText("Jump host")).toBeInTheDocument();
    expect(within(hops[0]).getByText("Proven")).toBeInTheDocument();
    expect(within(hops[0]).getByText("Simulation sim-2026-11-20")).toBeInTheDocument();
    expect(within(hops[1]).getByText("Core banking")).toBeInTheDocument();
    expect(within(hops[1]).getByText("Inferred")).toBeInTheDocument();
    expect(within(hops[1]).queryByText(/Simulation/)).not.toBeInTheDocument();
    expect(screen.getAllByTestId("ctem-case-attack-path-primary").map((li) => li.textContent)).toEqual(["Credit decision"]);
  });

  it("shows restricted assets without their names", () => {
    renderWithProviders(
      <CaseAttackPath
        reading={{
          ...READING,
          origin: { asset: "Restricted", exposure: "internal" },
          hops: [{ from: "Restricted", to: "Restricted", nature: "proven", simulation: null }],
          primary: ["Restricted"],
        }}
      />,
    );
    expect(screen.getAllByText("Restricted asset").length).toBeGreaterThanOrEqual(4);
    expect(screen.queryByText("Edge bastion")).not.toBeInTheDocument();
    expect(screen.getByText("Proven")).toBeInTheDocument();
  });

  it("says why when no attack path leaves the asset and no business asset is reached", () => {
    renderWithProviders(<CaseAttackPath reading={{ ...READING, hops: [], reached: [], primary: [] }} />);
    expect(screen.getByTestId("ctem-case-attack-path-no-hop")).toBeInTheDocument();
    expect(screen.getByTestId("ctem-case-attack-path-no-primary")).toBeInTheDocument();
  });

  it("is never blank before triage reads it", () => {
    renderWithProviders(<CaseAttackPath reading={null} />);
    expect(screen.getByTestId("ctem-case-attack-path-empty")).toBeInTheDocument();
  });
});
