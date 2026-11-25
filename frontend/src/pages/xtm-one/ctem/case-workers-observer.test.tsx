// An observer of the case events is never the case worker (#2054): a
// supervisor that hears every case must not make a case look worked while
// every automation that would work it is switched off.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import { platformEventConfig, platformEventDraft } from "@/lib/platform-events";
import type { TriggerConfig } from "@/types";
import { caseAutomations, caseWork, observesOnly } from "./case-workers";
import { CaseWorkCard } from "./CaseWorkCard";

const CASE = { id: "c1", trigger_path: "new_vulnerability" };
const CASE_EVENTS = ["ctem_case.opened", "ctem_case.reopened"];
const AGENTS = [
  { id: "loop", name: "Vuln Orchestrator", fleet: { fleet_id: "f-loop", fleet_name: "CTEM loop" } },
  { id: "sup", name: "Supervisor", fleet: null },
];

function assignment(over: Partial<{ id: string; agent_id: string; name: string; enabled: boolean; trigger_config: Record<string, unknown> | null }>) {
  return { id: "work", agent_id: "loop", name: "Work cases", enabled: true, trigger_type: "platform_event", trigger_config: { event: CASE_EVENTS }, ...over };
}

const observer = assignment({
  id: "watch",
  agent_id: "sup",
  name: "Supervise the case lifecycle",
  trigger_config: { event: CASE_EVENTS, filter: { any: [{ case_id: { exists: true } }] }, role: "observer" },
});

function renderCard(ui: React.ReactElement) {
  return renderWithProviders(<MemoryRouter>{ui}</MemoryRouter>);
}

describe("observesOnly", () => {
  it("reads trigger_config.role", () => {
    expect(observesOnly(observer)).toBe(true);
    expect(observesOnly(assignment({}))).toBe(false);
    expect(observesOnly(assignment({ trigger_config: { event: CASE_EVENTS, role: "worker" } }))).toBe(false);
    expect(observesOnly(assignment({ trigger_config: null }))).toBe(false);
  });
});

describe("caseWork with an observer", () => {
  it("is off when only the observer is on and the loop automation is off", () => {
    const automations = caseAutomations([observer, assignment({ enabled: false })], AGENTS, CASE);
    expect(automations.map((a) => a.id)).toEqual(["work"]);
    expect(caseWork({ licensed: true, automations, runs: [] })).toMatchObject({ kind: "off", automations: [{ id: "work" }] });
  });

  it("is none when only the observer hears the case", () => {
    expect(caseWork({ licensed: true, automations: caseAutomations([observer], AGENTS, CASE), runs: [] })).toEqual({ kind: "none" });
  });

  it("names the loop automation, never the observer, when one loop automation is on", () => {
    const automations = caseAutomations([observer, assignment({})], AGENTS, CASE);
    const work = caseWork({ licensed: true, automations, runs: [] });
    expect(work).toMatchObject({ kind: "on", automations: [{ id: "work", agentName: "Vuln Orchestrator" }], off: [] });
  });

  it("keeps the switched-off automations visible next to the enabled ones", () => {
    const automations = caseAutomations(
      [observer, assignment({}), assignment({ id: "work-2", name: "Work cases too", enabled: false })],
      AGENTS,
      CASE,
    );
    expect(caseWork({ licensed: true, automations, runs: [] })).toMatchObject({ kind: "on", automations: [{ id: "work" }], off: [{ id: "work-2" }] });
  });
});

describe("CaseWorkCard with an observer", () => {
  it("shows the off state, never Worked by, when only the observer is on", () => {
    const automations = caseAutomations([observer, assignment({ enabled: false })], AGENTS, CASE);
    renderCard(<CaseWorkCard work={caseWork({ licensed: true, automations, runs: [] })} loopFleetId={null} />);
    expect(screen.getByTestId("ctem-case-work")).toHaveAttribute("data-state", "off");
    expect(screen.queryByText(/Worked by/)).toBeNull();
    expect(screen.queryByText(/Supervisor/)).toBeNull();
    expect(screen.getByRole("button", { name: "Switch on" })).toBeInTheDocument();
  });

  it("lists the automations switched off in the on state, by name with Switch on", () => {
    const automations = caseAutomations(
      [observer, assignment({}), assignment({ id: "work-2", name: "Work cases too", enabled: false })],
      AGENTS,
      CASE,
    );
    renderCard(<CaseWorkCard work={caseWork({ licensed: true, automations, runs: [] })} loopFleetId={null} />);
    expect(screen.getByTestId("ctem-case-work")).toHaveAttribute("data-state", "on");
    expect(screen.getByText(/Worked by Vuln Orchestrator/)).toBeInTheDocument();
    expect(screen.getByTestId("ctem-case-work-switched-off")).toHaveTextContent("1 automation that can work this case is switched off");
    const list = screen.getByTestId("ctem-case-work-switched-off-list");
    expect(list).toHaveTextContent("Work cases too");
    expect(screen.getAllByRole("button", { name: "Switch on" })).toHaveLength(1);
    expect(screen.queryByText(/Supervisor/)).toBeNull();
  });
});

describe("the observer role in the trigger editor", () => {
  it("round-trips trigger_config.role", () => {
    const draft = platformEventDraft({ event: CASE_EVENTS, role: "observer" } as unknown as TriggerConfig);
    expect(draft.observer).toBe(true);
    expect(platformEventConfig(draft)).toMatchObject({ role: "observer" });
    const plain = platformEventDraft({ event: "ctem_case.opened" } as TriggerConfig);
    expect(plain.observer).toBeUndefined();
    expect(platformEventConfig(plain)).not.toHaveProperty("role");
  });
});
