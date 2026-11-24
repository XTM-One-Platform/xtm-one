// "Cases in this phase" on the stage pages (#1621): counts by status linking to the
// list on that phase, the decisions waiting, the five that need attention first,
// and the empty sentences.
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import { CasesInPhaseCard } from "./CasesInPhaseCard";

const overview = vi.hoisted(() => ({ value: { data: undefined as unknown, isError: false } }));
vi.mock("@/hooks/api/useCtemCasesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemCasesApi")>()),
  useCtemCasesOverview: () => overview.value,
}));

const zero = { new: 0, analyzing: 0, waiting_decision: 0, remediating: 0, validating: 0, mitigated_watching: 0, resolved: 0, accepted_risk: 0 };
const emptyPhase = { open: 0, waiting: 0, counts: zero, top: [] };

function read(over: Record<string, unknown> = {}) {
  return {
    vertical: "cyber",
    counts: { ...zero, remediating: 1, waiting_decision: 1, accepted_risk: 2 },
    open: 2,
    phases: {
      scoping: emptyPhase,
      discovery: emptyPhase,
      prioritization: emptyPhase,
      validation: emptyPhase,
      remediation: {
        open: 2,
        waiting: 1,
        counts: { ...zero, remediating: 1, waiting_decision: 1 },
        top: [
          { id: "c1", title: "Approve the Tomcat fix", state: "waiting_decision", priority: "high", created_at: null, updated_at: null, state_since: null, waiting_on: null, crown_jewel: "Payments" },
          { id: "c2", title: "Upgrade OpenSSL", state: "remediating", priority: "medium", created_at: null, updated_at: null, state_since: null, waiting_on: null, crown_jewel: null },
        ],
      },
    },
    no_phase_open: 0,
    waiting: { count: 1, oldest_hours: 3, oldest: null, cases: [{ case_id: "c1", title: "Approve the Tomcat fix", phase: "remediation", gate: "approve_fix", since: null }] },
    funnel: { in_case: 2, resolved_window_days: 30, resolved_recent: 0 },
    ...over,
  };
}

function draw(stage: Parameters<typeof CasesInPhaseCard>[0]["stage"]) {
  return renderWithProviders(
    <MemoryRouter>
      <CasesInPhaseCard stage={stage} backTo={`/ctem/${stage}`} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  overview.value = { data: undefined, isError: false };
});

describe("CasesInPhaseCard", () => {
  it("links each status count to the list on the phase and that status", () => {
    overview.value = { data: read(), isError: false };
    draw("remediation");
    expect(screen.getByTestId("ctem-phase-count-remediating").getAttribute("href")).toBe("/ctem/cases?c_f_phase=remediation&state=remediating");
    expect(screen.getByTestId("ctem-phase-count-waiting_decision").textContent).toBe("Waiting for decision: 1 case");
    expect(screen.getByTestId("ctem-phase-cases-open").getAttribute("href")).toBe("/ctem/cases?c_f_phase=remediation");
  });

  it("lists the decisions waiting and the cases that need attention first, with the crown jewel", () => {
    overview.value = { data: read(), isError: false };
    draw("remediation");
    expect(screen.getByTestId("ctem-phase-wait-c1").querySelector("a")?.getAttribute("href")).toBe("/ctem/cases/c1");
    const first = screen.getByTestId("ctem-phase-case-c1");
    expect(first.textContent).toContain("touches Payments");
    expect(screen.getAllByTestId(/^ctem-phase-case-/)).toHaveLength(2);
  });

  it("says why a phase has no case when other phases have some", () => {
    overview.value = { data: read(), isError: false };
    draw("scoping");
    expect(screen.getByTestId("ctem-phase-cases-empty").textContent).toBe("No open case in Scoping: every case is in another phase or resolved.");
  });

  it("says no case exists yet when there is none at all", () => {
    overview.value = { data: read({ counts: zero, open: 0, waiting: { count: 0, oldest: null, oldest_hours: null, cases: [] } }), isError: false };
    draw("discovery");
    expect(screen.getByTestId("ctem-phase-cases-empty").textContent).toContain("No case yet");
  });

  it("counts the decisions waiting and the accepted risks on Governance & Risk", () => {
    overview.value = { data: read(), isError: false };
    draw("governance");
    expect(screen.getByTestId("ctem-phase-count-waiting_decision").getAttribute("href")).toBe("/ctem/cases?state=waiting_decision");
    expect(screen.getByTestId("ctem-phase-count-accepted_risk").textContent).toBe("Accepted risk: 2 cases");
  });

  it("titles the Governance & Risk card for what it counts, the stage pages by their phase (UX B2)", () => {
    overview.value = { data: read(), isError: false };
    const { unmount } = draw("governance");
    expect(screen.getByRole("heading", { name: "Decisions and accepted risks" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Cases in this phase" })).toBeNull();
    unmount();
    draw("remediation");
    expect(screen.getByRole("heading", { name: "Cases in this phase" })).toBeTruthy();
  });
});
