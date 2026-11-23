// The case page says who works the case (#1266): three equal choices when
// nothing does, the automations switched off by name, the run working it.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import { CaseWorkCard } from "./CaseWorkCard";
import type { CaseAutomation } from "./case-workers";

const OFF: CaseAutomation = { id: "as1", name: "Work cases", enabled: false, agentId: "a2", agentName: "My agent", fleetId: "f1", fleetName: "SOC fleet" };

function renderCard(ui: React.ReactElement) {
  return renderWithProviders(<MemoryRouter>{ui}</MemoryRouter>);
}

describe("CaseWorkCard", () => {
  it("offers three choices on equal footing when nothing works the case, all buttons", () => {
    renderCard(<CaseWorkCard work={{ kind: "none" }} loopFleetId={null} onStart={() => {}} />);
    expect(screen.getByText("No automation works this case yet")).toBeInTheDocument();
    expect(screen.getByTestId("ctem-case-work-choice-loop")).toBeInTheDocument();
    expect(screen.getByTestId("ctem-case-work-choice-own")).toBeInTheDocument();
    expect(screen.getByTestId("ctem-case-work-choice-self")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add a case automation" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("names each automation switched off with its agent and fleet, and Switch on", () => {
    renderCard(<CaseWorkCard work={{ kind: "off", automations: [OFF] }} loopFleetId={null} />);
    expect(screen.getByText("Work cases")).toBeInTheDocument();
    expect(screen.getByText("My agent (SOC fleet)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Switch on" })).toBeInTheDocument();
  });

  it("names the agent of a failed run and opens the run", () => {
    const run = { id: "r1", agent_id: "a2", agent_name: "My agent", status: "failed", created_at: "2026-11-22T10:00:00Z", completed_at: "2026-11-22T10:05:00Z", outcome: "failed" };
    renderCard(<CaseWorkCard work={{ kind: "failed", run, automations: [{ ...OFF, enabled: true }] }} loopFleetId={null} />);
    expect(screen.getByText("Worked by My agent (SOC fleet)")).toBeInTheDocument();
    expect(screen.getByTestId("ctem-case-work-state").textContent).toMatch(/failed/);
    expect(screen.getByRole("button", { name: "Open the run" })).toBeInTheDocument();
  });

  it("claims nothing while unknown", () => {
    const { container } = renderCard(<CaseWorkCard work={{ kind: "unknown" }} loopFleetId={null} />);
    expect(container.textContent).toBe("");
  });
});
