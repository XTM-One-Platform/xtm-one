// The case page says who works the case (#1266): three equal choices when
// nothing does, the automations switched off by name, the run working it.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import { CaseWorkCard } from "./CaseWorkCard";
import type { CtemCaseGateRecord, CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import { caseLoopWait, type CaseAutomation } from "./case-workers";

const OFF: CaseAutomation = { id: "as1", name: "Work cases", enabled: false, agentId: "a2", agentName: "My agent", fleetId: "f1", fleetName: "SOC fleet" };

const ON: CaseAutomation = { ...OFF, id: "as2", enabled: true, agentName: "Triage", fleetName: "CTEM loop" };
const LAST: CtemCaseRun = {
  id: "r9",
  agent_id: "a2",
  agent_name: "Triage",
  status: "completed",
  created_at: "2026-11-24T10:00:00Z",
  completed_at: "2026-11-24T10:04:00Z",
  outcome: "ok",
};

function gate(over: Partial<CtemCaseGateRecord>): CtemCaseGateRecord {
  return {
    key: "ctem.remediation.decision",
    phase: null,
    status: "waiting",
    asked_at: "2026-11-24T10:05:00Z",
    asked_of: null,
    asked_by_agent: null,
    deadline: null,
    answered_at: null,
    answered_by: null,
    surface: null,
    option: null,
    comment: null,
    reason: null,
    passed_at: null,
    wait_seconds: null,
    decision_id: null,
    work_item_id: null,
    recorded_on_case: false,
    next: null,
    ...over,
  };
}

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

  it("shows the open gate's question and its decision when the loop waits on a person", () => {
    const wait = caseLoopWait({ state: "analyzing", gates: [gate({ key: "ctem.analyst_review", work_item_id: "wi-1" })] });
    renderCard(<CaseWorkCard work={{ kind: "on", automations: [ON], last: LAST }} wait={wait} loopFleetId={null} />);
    expect(screen.getByTestId("ctem-case-work-state")).toHaveTextContent("Waits for a person");
    expect(screen.getByTestId("ctem-case-work-gate")).toHaveTextContent("Review: an analyst checks the threat package");
    expect(screen.getByRole("button", { name: "Open the decision" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Switch on" })).toBeNull();
  });

  it("names a decision keyed outside the gate catalog by its title (#2782)", () => {
    const other = gate({ key: "ctem:evidence", title: "Provide the exploit evidence", work_item_id: "wi-ev" });
    const wait = caseLoopWait({ state: "waiting_decision", gates: [], otherDecisions: [other] });
    expect(wait).toMatchObject({ kind: "gate", gate: "ctem:evidence", workItemId: "wi-ev", title: "Provide the exploit evidence" });
    renderCard(<CaseWorkCard work={{ kind: "on", automations: [ON], last: LAST }} wait={wait} loopFleetId={null} />);
    expect(screen.getByTestId("ctem-case-work-gate")).toHaveTextContent("Waiting for: Provide the exploit evidence");
  });

  it("puts the gate and the last run on one row grid", () => {
    const wait = caseLoopWait({ state: "analyzing", gates: [gate({ key: "ctem.analyst_review", work_item_id: "wi-1" })] });
    renderCard(<CaseWorkCard work={{ kind: "on", automations: [ON], last: LAST }} wait={wait} loopFleetId={null} />);
    expect(screen.getByTestId("ctem-case-work-gate")).toHaveAttribute("data-loop-row");
    expect(screen.getByTestId("ctem-case-work-last-run")).toHaveAttribute("data-loop-row");
  });

  it("shows the last run outcome and the stage the loop waits on when no gate is open", () => {
    const wait = caseLoopWait({ state: "validating", gates: [gate({ status: "answered" })] });
    renderCard(<CaseWorkCard work={{ kind: "on", automations: [ON], last: LAST }} wait={wait} loopFleetId={null} />);
    expect(screen.getByTestId("ctem-case-work-state")).toHaveTextContent("Waits for the validation to finish");
    expect(screen.queryByTestId("ctem-case-work-gate")).toBeNull();
    const last = screen.getByTestId("ctem-case-work-last-run");
    expect(last).toHaveTextContent("Last run by Triage");
    expect(last).toHaveTextContent(/succeeded/i);
    expect(screen.getByRole("button", { name: "Open the run" })).toBeInTheDocument();
  });

  it("names the other enabled automations behind and N more", () => {
    const other = { ...ON, id: "as3", name: "Second worker", agentName: "Other agent" };
    renderCard(<CaseWorkCard work={{ kind: "on", automations: [ON, other], last: null }} loopFleetId={null} />);
    expect(screen.getByTestId("ctem-case-work-more")).toHaveTextContent("and 1 more");
    expect(screen.getByTestId("ctem-case-work-more")).toHaveAttribute("tabindex", "0");
  });

  it("claims nothing while unknown", () => {
    const { container } = renderCard(<CaseWorkCard work={{ kind: "unknown" }} loopFleetId={null} />);
    expect(container.textContent).toBe("");
  });
});
