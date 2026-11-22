// Why an empty case section is empty (#1206, slice D): read from live state,
// never "the fleet drafts one" while no automation would do it.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import { caseLoopWorker, loopFleetOf, opencrqConnected, readerOpenedLines, sectionReason } from "./case-loop-reason";
import { CaseLoopProvider, CaseSectionReason } from "./CaseSectionReason";
import { CaseTreatmentPlan } from "./CaseTreatmentPlan";

const SEEDED = { id: "f-loop", slug: "ctem-loop", is_builtin: true, is_archived: false, forked_from: null };
const OFF = { total: 6, enabled: 0, paused: 0 };
const ON = { total: 6, enabled: 6, paused: 0 };

function run(over: Partial<CtemCaseRun>): CtemCaseRun {
  return { id: "r1", agent_id: "a1", agent_name: "CTEM Triage", status: "running", created_at: "2026-11-22T10:00:00Z", completed_at: null, outcome: "active", ...over };
}

describe("loopFleetOf", () => {
  it("finds the seeded fleet, else a fork of it, never an archived one", () => {
    expect(loopFleetOf([SEEDED])?.id).toBe("f-loop");
    const fork = { id: "f-fork", slug: "my-loop", is_builtin: false, is_archived: false, forked_from: { slug: "ctem-loop", is_builtin: true } };
    expect(loopFleetOf([fork])?.id).toBe("f-fork");
    expect(loopFleetOf([{ ...SEEDED, is_archived: true }])).toBeNull();
    expect(loopFleetOf(undefined)).toBeNull();
  });
});

describe("caseLoopWorker", () => {
  const base = { licensed: true, fleets: [SEEDED], fleetsError: false, automations: OFF, runs: [] as CtemCaseRun[] };

  it("says not set up when no automation of the loop fleet is on", () => {
    expect(caseLoopWorker(base)).toEqual({ kind: "not_set_up", fleetId: "f-loop" });
  });

  it("says not set up when the reader sees no loop fleet", () => {
    expect(caseLoopWorker({ ...base, fleets: [] })).toEqual({ kind: "not_set_up", fleetId: null });
  });

  it("names the oldest active run working the case", () => {
    const runs = [run({ id: "r2", agent_name: "Later", created_at: "2026-11-22T11:00:00Z" }), run({})];
    expect(caseLoopWorker({ ...base, automations: ON, runs })).toEqual({ kind: "running", agent: "CTEM Triage", since: "2026-11-22T10:00:00Z" });
  });

  it("claims nothing without the licence, on a read error, or before the health is read", () => {
    expect(caseLoopWorker({ ...base, licensed: false }).kind).toBe("unknown");
    expect(caseLoopWorker({ ...base, fleetsError: true }).kind).toBe("unknown");
    expect(caseLoopWorker({ ...base, fleets: undefined }).kind).toBe("unknown");
    expect(caseLoopWorker({ ...base, automations: undefined }).kind).toBe("unknown");
  });

  it("is on when the fleet runs and no run works the case now", () => {
    expect(caseLoopWorker({ ...base, automations: ON, runs: [run({ outcome: "succeeded" })] }).kind).toBe("on");
  });
});

describe("sectionReason", () => {
  it("puts OpenCRQ first for the sections written from it, only when known absent", () => {
    expect(sectionReason({ kind: "on" }, true, false)).toBe("opencrq");
    expect(sectionReason({ kind: "on" }, true, null)).toBe("own");
    expect(sectionReason({ kind: "not_set_up", fleetId: null }, false, false)).toBe("not_set_up");
    expect(sectionReason({ kind: "unknown" }, false, true)).toBe("own");
  });
});

describe("opencrqConnected and readerOpenedLines", () => {
  it("reads OpenCRQ from the registrations", () => {
    expect(opencrqConnected(undefined)).toBeNull();
    expect(opencrqConnected([{ platform_identifier: "opencti" }])).toBe(false);
    expect(opencrqConnected([{ platform_identifier: "opencrq" }])).toBe(true);
  });

  it("names each source reader once, OpenBAS read as OpenAEV", () => {
    expect(readerOpenedLines(["opencti", "opencti"])).toEqual(["opencti"]);
    expect(readerOpenedLines(["openbas", "opencti"])).toEqual(["opencti", "openaev"]);
    expect(readerOpenedLines([])).toEqual([]);
  });
});

describe("CaseSectionReason", () => {
  it("says no automation works the case and links the fleet", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseLoopProvider value={{ worker: { kind: "not_set_up", fleetId: "f-loop" }, opencrq: true }}>
          <CaseSectionReason own="Own sentence" testId="reason" />
        </CaseLoopProvider>
      </MemoryRouter>,
    );
    expect(screen.getByText(/No automation works this case/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the fleet" }).getAttribute("href")).toBe("/fleets/f-loop");
    expect(screen.queryByText("Own sentence")).toBeNull();
  });

  it("names the agent working the case", () => {
    renderWithProviders(
      <CaseLoopProvider value={{ worker: { kind: "running", agent: "CTEM Triage", since: "2026-11-22T10:00:00Z" }, opencrq: true }}>
        <CaseSectionReason own="Own sentence" testId="reason" />
      </CaseLoopProvider>,
    );
    expect(screen.getByTestId("reason").textContent).toMatch(/Waiting for CTEM Triage, working this case since/);
  });

  it("keeps the section's own sentence without the page's state", () => {
    renderWithProviders(<CaseSectionReason own="Own sentence" testId="reason" />);
    expect(screen.getByText("Own sentence")).toBeInTheDocument();
  });

  it("says OpenCRQ is not connected on the treatment plan", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseLoopProvider value={{ worker: { kind: "on" }, opencrq: false }}>
          <CaseTreatmentPlan plan={null} />
        </CaseLoopProvider>
      </MemoryRouter>,
    );
    expect(screen.getByText(/OpenCRQ is not connected/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connect OpenCRQ" }).getAttribute("href")).toBe("/integrations");
  });
});
