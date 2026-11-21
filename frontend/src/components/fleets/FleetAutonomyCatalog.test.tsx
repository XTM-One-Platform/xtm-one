// The About page's approval gates as a level matrix (#9769 S4): one row per
// declared gate, one column per level, the fleet's level marked, each cell
// saying whether the action waits or runs on its own.
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, screen, within } from "@testing-library/react";

import type { FleetAutonomyGate } from "@/hooks/api/useFleetApi";
import { renderWithProviders } from "@/testing/providers";

import { FleetAutonomyCatalog } from "./FleetAutonomyCatalog";

const catalog: FleetAutonomyGate[] = [
  {
    key: "ticket.create",
    action: "Create a ticket",
    module: "platform",
    impact: "low",
    gated: false,
    by_level: { full_gates: true, medium: false, low: false, autonomous: false },
  },
  {
    key: "openaev.scenario.launch",
    action: "Launch a scenario",
    module: "openaev",
    impact: "high",
    gated: true,
    by_level: { full_gates: true, medium: true, low: false, autonomous: false },
  },
];

afterEach(cleanup);

describe("FleetAutonomyCatalog", () => {
  it("draws one row per gate and one column per level, the fleet's level marked", () => {
    renderWithProviders(<FleetAutonomyCatalog catalog={catalog} level="medium" />);
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader")).toHaveLength(5);
    expect(within(table).getAllByRole("rowheader")).toHaveLength(2);
    expect(screen.getByTestId("about-autonomy-col-medium")).toHaveAttribute("aria-current", "true");
    expect(screen.getByTestId("about-autonomy-col-low")).not.toHaveAttribute("aria-current");
  });

  it("says per level whether each action waits for a person or runs on its own", () => {
    renderWithProviders(<FleetAutonomyCatalog catalog={catalog} level="medium" />);
    const [ticket, launch] = screen.getAllByRole("row").slice(1);
    const cells = (row: HTMLElement) => within(row).getAllByRole("cell").map((c) => c.textContent);
    // The strip's words, "Waits" and "Runs alone" (UX round 1 B3 on #10034).
    expect(cells(ticket)).toEqual(["Waits", "Runs alone", "Runs alone", "Runs alone"]);
    expect(cells(launch)).toEqual(["Waits", "Waits", "Runs alone", "Runs alone"]);
    expect(launch).toHaveTextContent("High impact");
  });

  it("draws the source line under the matrix when given (C3)", () => {
    renderWithProviders(<FleetAutonomyCatalog catalog={catalog} level="medium" source="Source: stored" />);
    expect(screen.getByTestId("about-autonomy-source")).toHaveTextContent("Source: stored");
  });

  it("names a gate proposed from an integration by its plain action and integration", () => {
    const proposed: FleetAutonomyGate = {
      key: "slack.high-writes",
      action: "Create or change content in Slack",
      module: "platform",
      provider: "slack",
      impact: "high",
      gated: true,
      by_level: { full_gates: true, medium: true, low: false, autonomous: false },
    };
    renderWithProviders(<FleetAutonomyCatalog catalog={[proposed]} level="medium" />);
    expect(screen.getByRole("rowheader")).toHaveTextContent("Create or change content in Slack");
    expect(screen.getByRole("rowheader")).not.toHaveTextContent("_");
  });

  it("marks a CTEM loop gate with its effect and its out-of-the-box default (#916, CTEM-34)", () => {
    const ctem: FleetAutonomyGate[] = [
      {
        key: "ctem.mobilize",
        action: "Mobilize: create the tickets, notify the owners, hand over the prepared change",
        module: "platform",
        impact: "destructive",
        gated: true,
        by_level: { full_gates: true, medium: true, low: true, autonomous: false },
      },
      {
        key: "ctem.remediation.decision",
        action: "Decide: choose the remediation option",
        module: "platform",
        impact: "high",
        gated: false,
        by_level: { full_gates: true, medium: true, low: false, autonomous: false },
      },
    ];
    renderWithProviders(<FleetAutonomyCatalog catalog={ctem} level="low" />);
    expect(screen.getByTestId("about-autonomy-default-ctem.mobilize")).toHaveTextContent("Out of the box: waits");
    expect(screen.getByTestId("about-autonomy-default-ctem.remediation.decision")).toHaveTextContent("Out of the box: runs alone");
    expect(screen.getByTestId("about-autonomy-effect-ctem.remediation.decision")).toHaveTextContent("decided by policy");
    expect(screen.getByTestId("about-autonomy-effect-ctem.mobilize")).toHaveTextContent("tickets");
    // A gate outside the CTEM catalog carries neither line.
    cleanup();
    renderWithProviders(<FleetAutonomyCatalog catalog={catalog} level="medium" />);
    expect(screen.queryByTestId("about-autonomy-default-ticket.create")).toBeNull();
  });

  it("gives the supplier Mobilize its own effect, not the tickets one (UX round 1 B2 on #934)", () => {
    const supplier: FleetAutonomyGate = {
      key: "ctem.mobilize",
      action: "Mobilize: carry out an action on a supplier (contact, control, clause, monitoring)",
      module: "platform",
      impact: "destructive",
      gated: true,
      by_level: { full_gates: true, medium: true, low: true, autonomous: false },
    };
    renderWithProviders(<FleetAutonomyCatalog catalog={[supplier]} level="low" />);
    const effect = screen.getByTestId("about-autonomy-effect-ctem.mobilize");
    expect(effect).toHaveTextContent("action on the supplier");
    expect(effect).not.toHaveTextContent("tickets");
  });

  it("has no setting column for a reader who does not manage the fleet", () => {
    renderWithProviders(<FleetAutonomyCatalog catalog={catalog} level="medium" fleetId="f1" canManage={false} />);
    expect(within(screen.getByRole("table")).getAllByRole("columnheader")).toHaveLength(5);
    expect(screen.queryByTestId("about-autonomy-pin-ticket.create")).toBeNull();
  });

  it("adds a setting column with one control per gate for a manager", () => {
    renderWithProviders(<FleetAutonomyCatalog catalog={catalog} level="medium" fleetId="f1" canManage isAdmin />);
    expect(within(screen.getByRole("table")).getAllByRole("columnheader")).toHaveLength(6);
    expect(screen.getByTestId("about-autonomy-pin-ticket.create")).toHaveTextContent("Follows the level");
  });
});
