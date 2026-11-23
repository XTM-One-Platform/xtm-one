// Who works a case (#1266): read from every automation on the case events,
// never "the CTEM loop fleet is not set up" as the only way.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import { loopFleetOf, noSignalOrigin } from "./case-loop-reason";
import { caseAutomations, caseSources, caseWork, filterTakesCase, listensToCases } from "./case-workers";
import { CaseLoopProvider, CaseSectionReason } from "./CaseSectionReason";
import { CaseTreatmentPlan } from "./CaseTreatmentPlan";

const SEEDED = { id: "f-loop", slug: "ctem-loop", is_builtin: true, is_archived: false, forked_from: null };

function run(over: Partial<CtemCaseRun>): CtemCaseRun {
  return { id: "r1", agent_id: "a1", agent_name: "CTEM Triage", status: "running", created_at: "2026-11-22T10:00:00Z", completed_at: null, outcome: "active", ...over };
}

const VULN_CASE = { id: "c1", trigger_path: "new_vulnerability" };
const THREAT_CASE = { id: "c2", trigger_path: "relevant_threat" };
const THREAT_PATHS = ["relevant_threat", "credential_leak", "third_party_event"];
const SEEDED_EVENTS = ["ctem_case.opened", "ctem_case.reopened", "ctem_case.sla_breached", "ctem_case.validation_finished", "ctem_case.retest_requested"];

function assignment(over: Partial<{ id: string; agent_id: string; name: string; enabled: boolean; trigger_type: string; trigger_config: { event?: unknown; filter?: unknown } | null }>) {
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
  it("keeps the platform-event assignments that start on a case event, a list of events included", () => {
    expect(listensToCases(assignment({}))).toBe(true);
    expect(listensToCases(assignment({ trigger_config: { event: ["timer.due", "ctem_case.reopened"] } }))).toBe(true);
    // Resolved or SLA events only continue a case: they never start the work on it.
    expect(listensToCases(assignment({ trigger_config: { event: "ctem_case.resolved" } }))).toBe(false);
    expect(listensToCases(assignment({ trigger_config: { event: ["ctem_case.sla_breached", "ctem_case.resolved"] } }))).toBe(false);
    expect(listensToCases(assignment({ trigger_config: { event: "timer.due" } }))).toBe(false);
    expect(listensToCases(assignment({ trigger_type: "schedule" }))).toBe(false);
    expect(listensToCases(assignment({ trigger_config: null }))).toBe(false);
  });

  it("names the agent and its fleet, whatever fleet owns it, enabled first", () => {
    const list = caseAutomations(
      [assignment({ id: "x", agent_id: "a2", enabled: false }), assignment({ id: "y" }), assignment({ id: "z", trigger_config: { event: "timer.due" } })],
      AGENTS,
      VULN_CASE,
    );
    expect(list.map((a) => a.id)).toEqual(["y", "x"]);
    expect(list[0]).toMatchObject({ agentName: "CTEM Triage", fleetName: "CTEM loop" });
    expect(list[1]).toMatchObject({ agentName: "My agent", fleetName: null, enabled: false });
  });
});

describe("filterTakesCase: the two seeded case automations", () => {
  const loop = assignment({ id: "loop", name: "Loop Orchestrator", trigger_config: { event: SEEDED_EVENTS, filter: { trigger_path: { in: THREAT_PATHS } } } });
  const vuln = assignment({ id: "vuln", name: "Vuln Orchestrator", trigger_config: { event: SEEDED_EVENTS, filter: { trigger_path: { not_in: THREAT_PATHS } } } });

  it("keeps exactly one of the mutually exclusive seeded filters per case", () => {
    expect(caseAutomations([loop, vuln], AGENTS, VULN_CASE).map((a) => a.id)).toEqual(["vuln"]);
    expect(caseAutomations([loop, vuln], AGENTS, THREAT_CASE).map((a) => a.id)).toEqual(["loop"]);
  });

  it("never claims a worker whose filter skips the case, even when it alone is on", () => {
    const automations = caseAutomations([loop, { ...vuln, enabled: false }], AGENTS, VULN_CASE);
    expect(caseWork({ licensed: true, automations, runs: [] })).toMatchObject({ kind: "off" });
    expect(caseWork({ licensed: true, automations: caseAutomations([loop], AGENTS, VULN_CASE), runs: [] })).toEqual({ kind: "none" });
  });

  it("reads the filter grammar: equality, lists, combinators, read-side keys", () => {
    expect(filterTakesCase({ trigger_path: "new_vulnerability" }, VULN_CASE)).toBe(true);
    expect(filterTakesCase({ trigger_path: ["relevant_threat", "credential_leak"] }, VULN_CASE)).toBe(false);
    expect(filterTakesCase({ not: { trigger_path: "new_vulnerability" } }, VULN_CASE)).toBe(false);
    expect(filterTakesCase({ any: [{ trigger_path: "relevant_threat" }, { object_type: "ctem_case" }] }, VULN_CASE)).toBe(true);
    expect(filterTakesCase({ scope: "all" }, VULN_CASE)).toBe(true);
    expect(filterTakesCase({ case_id: { eq: "other" } }, VULN_CASE)).toBe(false);
  });

  it("keeps an automation whose filter it cannot judge", () => {
    // vertical is in the event but not on the page; matches is not evaluated here.
    expect(filterTakesCase({ vertical: "cyber" }, VULN_CASE)).toBe(true);
    expect(filterTakesCase({ trigger_path: { matches: "rel.*" } }, VULN_CASE)).toBe(true);
    // An older API that does not name the case's path.
    expect(filterTakesCase({ trigger_path: { in: THREAT_PATHS } }, { id: "c3" })).toBe(true);
    expect(filterTakesCase(undefined, VULN_CASE)).toBe(true);
  });
});

describe("caseWork", () => {
  const on = caseAutomations([assignment({})], AGENTS, VULN_CASE);
  const off = caseAutomations([assignment({ enabled: false })], AGENTS, VULN_CASE);

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

describe("caseSources and noSignalOrigin", () => {
  it("reads each product from the registrations, OpenBAS read as OpenAEV", () => {
    expect(caseSources(undefined)).toEqual({ opencrq: null, opencti: null, openaev: null });
    expect(caseSources([{ platform_identifier: "opencti" }, { platform_identifier: "openbas" }])).toEqual({ opencrq: false, opencti: true, openaev: true });
  });

  it("reads a split from its manual creation, anything else as a legacy source read", () => {
    expect(noSignalOrigin([{ kind: "created", to: "manual" }])).toBe("split");
    expect(noSignalOrigin([{ kind: "created", to: "upgrade:fortios" }])).toBe("legacy");
    expect(noSignalOrigin(undefined)).toBe("legacy");
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
