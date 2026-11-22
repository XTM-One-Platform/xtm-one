// Who works a case (#1266): read from every automation on the case events,
// never "the CTEM loop fleet is not set up" as the only way.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import { loopFleetOf, readerOpenedLines } from "./case-loop-reason";
import { caseAutomations, caseSources, caseWork, listensToCases } from "./case-workers";
import { CaseLoopProvider, CaseSectionReason } from "./CaseSectionReason";
import { CaseTreatmentPlan } from "./CaseTreatmentPlan";

const SEEDED = { id: "f-loop", slug: "ctem-loop", is_builtin: true, is_archived: false, forked_from: null };

function run(over: Partial<CtemCaseRun>): CtemCaseRun {
  return { id: "r1", agent_id: "a1", agent_name: "CTEM Triage", status: "running", created_at: "2026-11-22T10:00:00Z", completed_at: null, outcome: "active", ...over };
}

function assignment(over: Partial<{ id: string; agent_id: string; name: string; enabled: boolean; trigger_type: string; trigger_config: { event?: unknown } | null }>) {
  return { id: "as1", agent_id: "a1", name: "Work cases", enabled: true, trigger_type: "platform_event", trigger_config: { event: "ctem_case.opened" }, ...over };
}

const AGENTS = [
  { id: "a1", name: "CTEM Triage", fleet: { fleet_id: "f-loop", fleet_name: "CTEM loop" } },
  { id: "a2", name: "My agent", fleet: null },
];

describe("loopFleetOf", () => {
  it("finds the seeded fleet, else a fork of it, never an archived one", () => {
    expect(loopFleetOf([SEEDED])?.id).toBe("f-loop");
    const fork = { id: "f-fork", slug: "my-loop", is_builtin: false, is_archived: false, forked_from: { slug: "ctem-loop", is_builtin: true } };
    expect(loopFleetOf([fork])?.id).toBe("f-fork");
    expect(loopFleetOf([{ ...SEEDED, is_archived: true }])).toBeNull();
    expect(loopFleetOf(undefined)).toBeNull();
  });
});

describe("listensToCases and caseAutomations", () => {
  it("keeps the platform-event assignments on a case event, a list of events included", () => {
    expect(listensToCases(assignment({}))).toBe(true);
    expect(listensToCases(assignment({ trigger_config: { event: ["timer.due", "ctem_case.reopened"] } }))).toBe(true);
    expect(listensToCases(assignment({ trigger_config: { event: "timer.due" } }))).toBe(false);
    expect(listensToCases(assignment({ trigger_type: "schedule" }))).toBe(false);
    expect(listensToCases(assignment({ trigger_config: null }))).toBe(false);
  });

  it("names the agent and its fleet, whatever fleet owns it, enabled first", () => {
    const list = caseAutomations(
      [assignment({ id: "x", agent_id: "a2", enabled: false }), assignment({ id: "y" }), assignment({ id: "z", trigger_config: { event: "timer.due" } })],
      AGENTS,
    );
    expect(list.map((a) => a.id)).toEqual(["y", "x"]);
    expect(list[0]).toMatchObject({ agentName: "CTEM Triage", fleetName: "CTEM loop" });
    expect(list[1]).toMatchObject({ agentName: "My agent", fleetName: null, enabled: false });
  });
});

describe("caseWork", () => {
  const on = caseAutomations([assignment({})], AGENTS);
  const off = caseAutomations([assignment({ enabled: false })], AGENTS);

  it("says none when nothing listens to the case events", () => {
    expect(caseWork({ licensed: true, automations: [], runs: [] })).toEqual({ kind: "none" });
  });

  it("names the automations switched off", () => {
    expect(caseWork({ licensed: true, automations: off, runs: [] })).toMatchObject({ kind: "off", automations: off });
  });

  it("names the oldest live run working the case", () => {
    const runs = [run({ id: "r2", created_at: "2026-11-22T11:00:00Z" }), run({})];
    expect(caseWork({ licensed: true, automations: on, runs })).toMatchObject({ kind: "running", run: { id: "r1" } });
  });

  it("says the newest run failed when none runs now", () => {
    const runs = [run({ id: "old", outcome: "succeeded", created_at: "2026-11-21T10:00:00Z" }), run({ id: "new", outcome: "failed" })];
    expect(caseWork({ licensed: true, automations: on, runs })).toMatchObject({ kind: "failed", run: { id: "new" } });
  });

  it("is on when an automation is on and no run works the case now", () => {
    expect(caseWork({ licensed: true, automations: on, runs: [run({ outcome: "succeeded" })] }).kind).toBe("on");
  });

  it("claims nothing without the licence or before the automations are read", () => {
    expect(caseWork({ licensed: false, automations: on, runs: [] }).kind).toBe("unknown");
    expect(caseWork({ licensed: true, automations: undefined, runs: [] }).kind).toBe("unknown");
  });
});

describe("caseSources and readerOpenedLines", () => {
  it("reads each product from the registrations, OpenBAS read as OpenAEV", () => {
    expect(caseSources(undefined)).toEqual({ opencrq: null, opencti: null, openaev: null });
    expect(caseSources([{ platform_identifier: "opencti" }, { platform_identifier: "openbas" }])).toEqual({ opencrq: false, opencti: true, openaev: true });
  });

  it("names each source reader once, OpenBAS read as OpenAEV", () => {
    expect(readerOpenedLines(["opencti", "opencti"])).toEqual(["opencti"]);
    expect(readerOpenedLines(["openbas", "opencti"])).toEqual(["opencti", "openaev"]);
    expect(readerOpenedLines([])).toEqual([]);
  });
});

describe("CaseSectionReason", () => {
  const ALL = { opencrq: true, opencti: true, openaev: true };

  it("says what the section holds and that its product is connected, never who works the case", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseLoopProvider value={{ sources: ALL }}>
          <CaseSectionReason own="What it holds" source="openaev" testId="reason" />
        </CaseLoopProvider>
      </MemoryRouter>,
    );
    expect(screen.getByText("What it holds")).toBeInTheDocument();
    expect(screen.getByText(/Written from OpenAEV, connected/)).toBeInTheDocument();
    expect(screen.queryByText(/No automation works this case/)).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("keeps the section's sentence alone without the page's state", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseSectionReason own="What it holds" source="opencrq" testId="reason" />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("reason").textContent).toBe("What it holds");
  });

  it("offers a Connect button where the product is missing", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseLoopProvider value={{ sources: { ...ALL, opencrq: false } }}>
          <CaseTreatmentPlan plan={null} />
        </CaseLoopProvider>
      </MemoryRouter>,
    );
    expect(screen.getByText(/OpenCRQ is not connected/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect OpenCRQ" })).toBeInTheDocument();
  });
});
