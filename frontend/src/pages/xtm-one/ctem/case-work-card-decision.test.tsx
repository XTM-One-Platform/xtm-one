// The Worked-by card points to a decision answered on the case page instead of
// a link away (one way to answer a decision): "Answer on this page" scrolls to
// and focuses the card; "Open the decision" only when the card is not readable.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemCaseGateRecord } from "@/hooks/api/useCtemCasesApi";
import { renderWithProviders } from "@/testing/providers";

const state = vi.hoisted(() => ({ byId: {} as Record<string, unknown> }));

vi.mock("@/hooks/api/useWorkItemApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useWorkItemApi")>()),
  useWorkItem: (id: string | null | undefined) => ({ data: id ? state.byId[id] : undefined, isLoading: false, refetch: vi.fn() }),
}));

const { CaseWorkCard } = await import("./CaseWorkCard");
const { GateDecisionAnchor } = await import("./CaseGateDecision");
const { caseLoopWait } = await import("./case-workers");

const ON = { id: "as2", name: "Work cases", enabled: true, agentId: "a2", agentName: "Triage", fleetId: "f1", fleetName: "CTEM loop" };
const OPEN = (id: string) => ({ id, status: "blocked", needs_human: true, ask: { question: "Go?", options: [] } });

function gate(over: Partial<CtemCaseGateRecord>): CtemCaseGateRecord {
  return {
    key: "ctem.close_without_retest",
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
    work_item_id: "wi-1",
    recorded_on_case: false,
    next: null,
    ...over,
  };
}

beforeEach(() => {
  state.byId = {};
});

describe("Worked-by: the waiting decision", () => {
  it("points to the decision card on the page and focuses it, never a link away", () => {
    state.byId["wi-1"] = OPEN("wi-1");
    const wait = caseLoopWait({ state: "waiting_decision", gates: [gate({})] });
    renderWithProviders(
      <MemoryRouter>
        <CaseWorkCard work={{ kind: "on", automations: [ON], last: null }} wait={wait} loopFleetId={null} />
        <GateDecisionAnchor gate="ctem.close_without_retest">
          <div>card</div>
        </GateDecisionAnchor>
      </MemoryRouter>,
    );
    expect(screen.queryByText("Open the decision")).toBeNull();
    const anchor = screen.getByTestId("ctem-gate-decision-anchor");
    anchor.scrollIntoView = vi.fn();
    fireEvent.click(screen.getByRole("button", { name: "Answer on this page" }));
    expect(anchor.scrollIntoView).toHaveBeenCalled();
    expect(document.activeElement).toBe(anchor);
  });

  // The arrow says where the card is: a section's card below the row, the page's waiting card above it.
  it("points down when the decision card comes after the row", () => {
    state.byId["wi-1"] = OPEN("wi-1");
    const wait = caseLoopWait({ state: "waiting_decision", gates: [gate({})] });
    renderWithProviders(
      <MemoryRouter>
        <CaseWorkCard work={{ kind: "on", automations: [ON], last: null }} wait={wait} loopFleetId={null} />
        <GateDecisionAnchor gate="ctem.close_without_retest">
          <div>card</div>
        </GateDecisionAnchor>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("ctem-case-work-gate-answer")).toHaveAttribute("data-direction", "down");
  });

  it("points up when the decision card comes before the row", () => {
    state.byId["wi-1"] = OPEN("wi-1");
    const wait = caseLoopWait({ state: "waiting_decision", gates: [gate({})] });
    renderWithProviders(
      <MemoryRouter>
        <GateDecisionAnchor gate="ctem.close_without_retest">
          <div>card</div>
        </GateDecisionAnchor>
        <CaseWorkCard work={{ kind: "on", automations: [ON], last: null }} wait={wait} loopFleetId={null} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("ctem-case-work-gate-answer")).toHaveAttribute("data-direction", "up");
  });

  it("keeps Open the decision when the viewer cannot read the item", () => {
    const wait = caseLoopWait({ state: "waiting_decision", gates: [gate({ work_item_id: "wi-hidden" })] });
    renderWithProviders(
      <MemoryRouter>
        <CaseWorkCard work={{ kind: "on", automations: [ON], last: null }} wait={wait} loopFleetId={null} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("button", { name: "Open the decision" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Answer on this page" })).toBeNull();
  });
});

// The anchor renders its card and nothing else.
describe("GateDecisionAnchor", () => {
  it("wraps the card with the gate's anchor id", () => {
    render(
      <GateDecisionAnchor gate="ctem.accept_risk">
        <span>x</span>
      </GateDecisionAnchor>,
    );
    expect(screen.getByTestId("ctem-gate-decision-anchor").id).toBe("ctem-gate-decision-ctem-accept-risk");
  });
});
