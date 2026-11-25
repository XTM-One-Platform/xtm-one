// Attack path and blast radius on a case (CTEM-23-UI, #1289): the hops nearest
// first with proven or inferred, the primary assets reached, a restricted
// reading for a viewer who does not read OpenCRQ, and empty states that are
// never blank.

import { describe, expect, it } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseAttackPath } from "@/hooks/api/useCtemCasesApi";
import { CaseAttackPath } from "./CaseAttackPath";

const READING: CtemCaseAttackPath = {
  origin: { asset: "Edge bastion", exposure: "internet_facing" },
  hops: [
    {
      from: "Edge bastion",
      to: "Jump host",
      nature: "proven",
      simulation_name: "Edge pivot drill",
      simulation_url: "https://aev.example/admin/simulations/sim-1",
      seen_at: "2026-11-20",
    },
    { from: "Jump host", to: "Core banking", nature: "inferred", simulation_name: null, simulation_url: null, seen_at: null },
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
    renderWithProviders(<MemoryRouter><CaseAttackPath reading={READING} /></MemoryRouter>);
    expect(screen.getByText("Attack path and blast radius")).toBeInTheDocument();
    expect(screen.getByText("Internet-facing")).toBeInTheDocument();
    const hops = screen.getAllByTestId("ctem-case-attack-path-hop");
    expect(hops).toHaveLength(2);
    expect(within(hops[0]).getByText("Jump host")).toBeInTheDocument();
    expect(within(hops[0]).getByText("Proven")).toBeInTheDocument();
    const proof = within(hops[0]).getByTestId("ctem-case-attack-path-proof");
    expect(proof).toHaveTextContent(/^Proven by Edge pivot drill, /);
    expect(proof).toHaveAttribute("href", "https://aev.example/admin/simulations/sim-1");
    expect(proof).not.toHaveTextContent("sim-1");
    expect(within(hops[1]).getByText("Core banking")).toBeInTheDocument();
    expect(within(hops[1]).getByText("Inferred")).toBeInTheDocument();
    expect(within(hops[1]).queryByTestId("ctem-case-attack-path-proof")).not.toBeInTheDocument();
    expect(screen.getByText("Attack starts from")).toBeInTheDocument();
    expect(screen.getAllByTestId("ctem-case-attack-path-primary").map((li) => li.textContent)).toEqual(["Credit decision"]);
    // Each primary asset reached opens its page, by name (#1980 S2).
    expect(screen.getByRole("link", { name: "Credit decision" }).getAttribute("href")).toBe("/ctem/inventory/primary-assets/Credit%20decision");
  });

  it("shows restricted assets without their names", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseAttackPath
          reading={{
            ...READING,
            origin: { asset: "Restricted", exposure: "internal" },
            hops: [{ from: "Restricted", to: "Restricted", nature: "proven", simulation_name: null, simulation_url: null, seen_at: "2026-11-20" }],
            primary: ["Restricted"],
          }}
        />
      </MemoryRouter>,
    );
    // A restricted primary asset opens nothing: its name is not the viewer's to read.
    expect(within(screen.getByTestId("ctem-case-attack-path-primary")).queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getAllByText("Restricted asset").length).toBeGreaterThanOrEqual(4);
    expect(screen.queryByText("Edge bastion")).not.toBeInTheDocument();
    expect(screen.getByText("Proven")).toBeInTheDocument();
    expect(screen.getByTestId("ctem-case-attack-path-proof")).toHaveTextContent(/^Proven by an OpenAEV simulation, /);
    expect(screen.getByTestId("ctem-case-attack-path-proof").tagName).toBe("SPAN");
  });

  it("gives proven and inferred hops different tones", () => {
    renderWithProviders(<MemoryRouter><CaseAttackPath reading={READING} /></MemoryRouter>);
    const [proven, inferred] = screen.getAllByTestId("ctem-case-attack-path-nature");
    expect(proven.outerHTML).not.toEqual(inferred.outerHTML.replace("Inferred", "Proven"));
  });

  it("says why when no attack path leaves the asset and no business asset is reached", () => {
    renderWithProviders(<MemoryRouter><CaseAttackPath reading={{ ...READING, hops: [], reached: [], primary: [] }} /></MemoryRouter>);
    expect(screen.getByTestId("ctem-case-attack-path-no-hop")).toBeInTheDocument();
    expect(screen.getByTestId("ctem-case-attack-path-no-primary")).toBeInTheDocument();
  });

  it("is never blank before triage reads it", () => {
    renderWithProviders(<MemoryRouter><CaseAttackPath reading={null} /></MemoryRouter>);
    expect(screen.getByTestId("ctem-case-attack-path-empty")).toBeInTheDocument();
  });

  it("folds a long path to the five nearest hops behind Show all (#1679, point 5)", () => {
    const hops = Array.from({ length: 12 }, (_, i) => ({
      from: `Host ${i}`,
      to: `Host ${i + 1}`,
      nature: "inferred" as const,
      simulation_name: null,
      simulation_url: null,
      seen_at: null,
    }));
    renderWithProviders(<MemoryRouter><CaseAttackPath reading={{ ...READING, hops }} /></MemoryRouter>);
    expect(screen.getAllByTestId("ctem-case-attack-path-hop")).toHaveLength(5);
    expect(within(screen.getAllByTestId("ctem-case-attack-path-hop")[0]).getByText("Host 1")).toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: "Show all (12)" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", "ctem-case-attack-path-hops");
    fireEvent.click(toggle);
    expect(screen.getAllByTestId("ctem-case-attack-path-hop")).toHaveLength(12);
    expect(screen.getByRole("button", { name: "Show fewer" })).toHaveAttribute("aria-expanded", "true");
  });

  it("draws no fold on a short path", () => {
    renderWithProviders(<MemoryRouter><CaseAttackPath reading={READING} /></MemoryRouter>);
    expect(screen.queryByTestId("ctem-case-attack-path-more")).not.toBeInTheDocument();
  });
});
