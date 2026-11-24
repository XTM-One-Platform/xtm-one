// The decision record of a CTEM gate (#1518): an answered decision reads as
// answered with who, when, the option label, the comment, the wait, the
// channel, the decision link and what ran next, whether or not the case
// recorded the answer; a policy pass says why in words; a waiting gate shows
// who it was asked of and since when; the cards drop the false "is in their
// Waiting for you list" sentence once a record says the decision was taken.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseEvent, CtemCaseGateRecord } from "@/hooks/api/useCtemCasesApi";
import { gateChipOf, gateRecordOf, gateRecordReason, waitParts } from "./GateDecisionRecord";

const state = vi.hoisted(() => ({ items: [] as unknown[] }));

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: false }) }));
vi.mock("@/hooks/api/useWorkItemApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useWorkItemApi")>()),
  useWorkItems: (_filters: unknown, enabled: boolean) => ({ data: enabled ? state.items : undefined, isLoading: false, refetch: vi.fn() }),
}));
vi.mock("@/components/work-items/WorkItemDecisionCard", () => ({
  WorkItemDecisionCard: () => <div data-testid="unified-decision-card" />,
}));

const { GateDecisionRecord } = await import("./GateDecisionRecord");
const { CaseValidation } = await import("./CaseValidation");
const { CaseAnalystReview } = await import("./CaseAnalystReview");
const { CaseRemediationDecision } = await import("./CaseRemediationDecision");

const USER = "0b8f2a64-1c3d-4e5f-8a9b-0c1d2e3f4a5b";

function rec(over: Partial<CtemCaseGateRecord>): CtemCaseGateRecord {
  return {
    key: "ctem.validation.non_readonly",
    phase: "validation",
    status: "answered",
    asked_at: "2026-11-23T11:56:00Z",
    asked_of: { type: "user", id: USER, name: "Samuel Hassine" },
    asked_by_agent: { id: "a-1", name: "CTEM Validator" },
    deadline: null,
    answered_at: "2026-11-23T12:41:00Z",
    answered_by: { id: USER, name: "Samuel Hassine" },
    surface: "teams_card",
    option: { key: "A", label: "Launch the atomic test" },
    comment: "Go, the asset is in the lab group.",
    reason: null,
    passed_at: null,
    wait_seconds: 2700,
    decision_id: "d-1",
    work_item_id: "w-1",
    recorded_on_case: true,
    next: { at: "2026-11-23T12:42:00Z", kind: "validation", headline: "Atomic test launched in OpenAEV", actor_type: "agent", actor_name: "CTEM Validator" },
    ...over,
  };
}

function ev(over: Partial<CtemCaseEvent>): CtemCaseEvent {
  return { at: "2026-11-23T11:56:00Z", actor: "system", kind: "gate", issue: null, from: "ctem.validation.non_readonly", to: "waited", reason: null, evidence: null, ...over };
}

beforeEach(() => {
  state.items = [];
});

/** The record links to the decision and the asking agent: a router is needed. */
function render(ui: ReactElement) {
  return renderWithProviders(<MemoryRouter>{ui}</MemoryRouter>);
}

describe("gate record helpers", () => {
  it("picks the record of a gate key, null on an older API", () => {
    const gates = [rec({}), rec({ key: "ctem.analyst_review" })];
    expect(gateRecordOf(gates, "ctem.analyst_review")?.key).toBe("ctem.analyst_review");
    expect(gateRecordOf(undefined, "ctem.analyst_review")).toBeNull();
    expect(gateRecordOf(gates, "ctem.mobilize")).toBeNull();
  });

  it("splits a wait into its two largest units", () => {
    expect(waitParts(42)).toEqual([[42, "second"]]);
    expect(waitParts(2700)).toEqual([[45, "minute"]]);
    expect(waitParts(3 * 3600 + 5 * 60 + 9)).toEqual([[3, "hour"], [5, "minute"]]);
    expect(waitParts(2 * 86400 + 3600 + 60)).toEqual([[2, "day"], [1, "hour"]]);
  });

  it("names a policy pass in words, never a person", () => {
    expect(gateRecordReason({ kind: "runbook", runbook: "RB-FW-12" })).toEqual({ id: "Pre-approved runbook {runbook}", values: { runbook: "RB-FW-12" } });
    expect(gateRecordReason({ kind: "fleet_level" })?.id).toBe("The fleet runs this gate without a person");
    expect(gateRecordReason({ kind: "person" })).toBeNull();
    expect(gateRecordReason(null)).toBeNull();
  });

  it("reads an answer by the meaning of its option, answered when it has none", () => {
    expect(gateChipOf({ status: "answered", option: { key: "approve", label: "Approve the threat package" } })).toBe("approved");
    expect(gateChipOf({ status: "answered", option: { key: "reject", label: "Reject the package" } })).toBe("rejected");
    expect(gateChipOf({ status: "answered", option: { key: "B", label: "Defer to next week" } })).toBe("deferred");
    expect(gateChipOf({ status: "answered", option: { key: "A", label: "Launch the atomic test" } })).toBe("answered");
    expect(gateChipOf({ status: "answered", option: null })).toBe("answered");
    expect(gateChipOf({ status: "expired", option: { key: "approve", label: null } })).toBe("expired");
  });
});

describe("GateDecisionRecord", () => {
  it("shows an answered decision as a structured record", () => {
    render(<GateDecisionRecord record={rec({})} />);
    expect(screen.getByTestId("ctem-gate-record-status").textContent).toBe("Answered");
    expect(screen.getByTestId("ctem-gate-record-by").textContent).toContain("Samuel Hassine");
    expect(screen.getByTestId("ctem-gate-record-option").querySelector("dd")?.textContent).toBe("Launch the atomic test");
    expect(screen.getByTestId("ctem-gate-record-comment").textContent).toContain("Go, the asset is in the lab group.");
    expect(screen.getByTestId("ctem-gate-record-wait").textContent).toContain("45");
    expect(screen.getByTestId("ctem-gate-record-surface").textContent).toContain("Teams card");
    expect((screen.getByTestId("ctem-gate-record-decision").querySelector("a") as HTMLAnchorElement).getAttribute("href")).toBe("/tasks/work-items/w-1");
    expect(screen.getByTestId("ctem-gate-record-next").textContent).toContain("Recorded a validation");
    expect(screen.getByTestId("ctem-gate-record-next").textContent).toContain("Atomic test launched in OpenAEV");
    expect(screen.queryByText("Waited for a person")).toBeNull();
    expect(screen.queryByTestId("ctem-gate-record-pending")).toBeNull();
  });

  it("shows a rejection as Rejected, never as a green Answered", () => {
    render(<GateDecisionRecord record={rec({ key: "ctem.analyst_review", option: { key: "reject", label: "Reject the package" } })} />);
    expect(screen.getByTestId("ctem-gate-record-status").textContent).toBe("Rejected");
    expect(screen.getByTestId("ctem-gate-record").getAttribute("data-chip")).toBe("rejected");
    expect(screen.getByTestId("ctem-gate-record-option").querySelector("dd")?.textContent).toBe("Reject the package");
  });

  it("falls back to the option key only when there is no label", () => {
    render(<GateDecisionRecord record={rec({ option: { key: "C", label: null } })} />);
    expect(screen.getByTestId("ctem-gate-record-option").querySelector("dd")?.textContent).toBe("Option C");
  });

  it("names the next step in words, never its raw kind", () => {
    const states = rec({ next: { at: "2026-11-23T12:42:00Z", kind: "state", from: "waiting_decision", to: "remediating", headline: null, actor_type: "agent", actor_name: null } });
    const { unmount } = render(<GateDecisionRecord record={states} />);
    expect(screen.getByTestId("ctem-gate-record-next").textContent).toContain("Changed the state to");
    expect(screen.getByTestId("ctem-gate-record-next").textContent).toContain("Remediating");
    const parts = Array.from(screen.getByTestId("ctem-gate-record-next").querySelectorAll("dd span span")).map((s) => s.textContent);
    expect(parts).not.toContain("state");
    unmount();
    const gate = rec({ next: { at: "2026-11-23T12:42:00Z", kind: "gate", from: "ctem.analyst_review", to: "answered", headline: null, actor_type: "system", actor_name: null } });
    render(<GateDecisionRecord record={gate} />);
    expect(screen.getByTestId("ctem-gate-record-next").textContent).toContain("Review: an analyst checks the threat package");
    expect(screen.getByTestId("ctem-gate-record-next").textContent).toContain("Answered by a person");
  });

  it("draws both links through the design-system link", () => {
    render(<GateDecisionRecord record={rec({})} />);
    const decision = screen.getByRole("link", { name: "Open the decision" });
    const agent = screen.getByRole("link", { name: "CTEM Validator" });
    expect(decision.getAttribute("href")).toBe("/tasks/work-items/w-1");
    expect(agent.getAttribute("href")).toBe("/agents/a-1");
  });

  it("says the case has not recorded an answer yet, and still reads as answered", () => {
    render(<GateDecisionRecord record={rec({ recorded_on_case: false, next: null })} />);
    expect(screen.getByTestId("ctem-gate-record-status").textContent).toBe("Answered");
    expect(screen.getByTestId("ctem-gate-record-pending")).toBeTruthy();
  });

  it("never prints the id of a person it cannot name", () => {
    render(<GateDecisionRecord record={rec({ answered_by: { id: USER, name: null } })} />);
    expect(screen.getByTestId("ctem-gate-record-by").querySelector("dd")?.textContent).toBe("a person");
    expect(screen.queryByText(new RegExp(USER))).toBeNull();
  });

  it("shows a rejected-then-expired decision as expired with no answer fields", () => {
    render(<GateDecisionRecord record={rec({ status: "expired", answered_at: null, answered_by: null, option: null, comment: null, surface: null, wait_seconds: null, next: null })} />);
    expect(screen.getByTestId("ctem-gate-record-status").textContent).toBe("Expired");
    expect(screen.queryByTestId("ctem-gate-record-by")).toBeNull();
    expect(screen.queryByTestId("ctem-gate-record-option")).toBeNull();
  });

  it("dates an expired gate by its deadline and keeps the asked time", () => {
    render(
      <GateDecisionRecord record={rec({ status: "expired", answered_at: null, answered_by: null, option: null, comment: null, surface: null, wait_seconds: null, next: null, asked_at: "2026-11-22T08:17:00Z", deadline: "2026-11-23T08:17:00Z" })} />,
    );
    // The header carries the expiry (the deadline), never the ask time again.
    expect(screen.getAllByText(/Nov 23, 2026/)).toHaveLength(1);
    expect(screen.getByTestId("ctem-gate-record-asked-at").textContent).toContain("Nov 22, 2026");
    expect(screen.getAllByText(/Nov 22, 2026/)).toHaveLength(1);
  });

  it("shows no header time on a cancelled gate", () => {
    render(
      <GateDecisionRecord record={rec({ status: "cancelled", answered_at: null, answered_by: null, option: null, comment: null, surface: null, wait_seconds: null, next: null, asked_at: "2026-11-22T08:17:00Z", deadline: "2026-11-23T08:17:00Z" })} />,
    );
    // Only the asked time, in the grid: the cancellation time is not known.
    expect(screen.getAllByText(/Nov 22, 2026/)).toHaveLength(1);
    expect(screen.queryByText(/Nov 23, 2026/)).toBeNull();
  });

  it("shows a policy pass with its reason and no person", () => {
    render(
      <GateDecisionRecord record={rec({ status: "passed_automatically", answered_at: null, answered_by: null, option: null, comment: null, surface: null, wait_seconds: null, asked_of: null, reason: { kind: "runbook", runbook: "RB-FW-12" }, passed_at: "2026-11-23T11:00:00Z" })} />,
    );
    expect(screen.getByTestId("ctem-gate-record-status").textContent).toBe("Passed automatically");
    expect(screen.getByTestId("ctem-gate-record-reason").querySelector("dd")?.textContent).toBe("Pre-approved runbook RB-FW-12");
    expect(screen.queryByTestId("ctem-gate-record-by")).toBeNull();
    expect(screen.queryByTestId("ctem-gate-record-asked-at")).toBeNull();
  });

  it("shows a waiting gate with who it was asked of and since when", () => {
    render(
      <GateDecisionRecord record={rec({ status: "waiting", answered_at: null, answered_by: null, option: null, comment: null, surface: null, next: null, deadline: "2026-11-24T12:00:00Z" })} />,
    );
    expect(screen.getByTestId("ctem-gate-record-status").textContent).toBe("Waiting");
    // One reading of the wait: the grid alone, no relative time and no asked time in the header.
    expect(screen.queryByTestId("ctem-gate-record-since")).toBeNull();
    expect(screen.getAllByText(/Nov 23, 2026/)).toHaveLength(1);
    expect(screen.getByTestId("ctem-gate-record-asked-of").textContent).toContain("Samuel Hassine");
    expect(screen.getByTestId("ctem-gate-record-deadline")).toBeTruthy();
    expect(screen.getByTestId("ctem-gate-record-wait").textContent).toContain("Waiting for");
  });
});

describe("the gate cards read the record first", () => {
  it("Validation: an answered record wins over a waited event, no false sentence", () => {
    render(<CaseValidation caseId="c1" validations={[]} history={[ev({})]} gates={[rec({ recorded_on_case: false })]} />);
    expect(screen.getByTestId("ctem-case-validation-record-status").textContent).toBe("Answered");
    expect(screen.queryByText("Waited for a person")).toBeNull();
    expect(screen.queryByTestId("ctem-case-validation-elsewhere")).toBeNull();
  });

  it("Analyst review: an answered record shows the option label", () => {
    const record = rec({ key: "ctem.analyst_review", option: { key: "approve", label: "Approve the package" } });
    render(<CaseAnalystReview caseId="c1" pkg={null} history={[ev({ from: "ctem.analyst_review" })]} gates={[record]} />);
    expect(screen.getByTestId("ctem-case-analyst-review-record-option").textContent).toContain("Approve the package");
    expect(screen.queryByTestId("ctem-case-analyst-review-elsewhere")).toBeNull();
  });

  it("Remediation decision: an answered record replaces the outcome line", () => {
    const record = rec({ key: "ctem.remediation.decision", option: { key: "B", label: "Upgrade nginx" } });
    render(<CaseRemediationDecision caseId="c1" history={[ev({ from: "ctem.remediation.decision" })]} gates={[record]} />);
    expect(screen.getByTestId("ctem-case-remediation-record-option").querySelector("dd")?.textContent).toBe("Upgrade nginx");
    expect(screen.queryByTestId("ctem-case-remediation-outcome")).toBeNull();
    expect(screen.queryByTestId("ctem-case-remediation-elsewhere")).toBeNull();
  });

  it("an older API without records keeps the event line", () => {
    render(<CaseValidation caseId="c1" validations={[]} history={[ev({ to: "answered", reason: USER, evidence: "A" })]} />);
    expect(screen.getByTestId("ctem-case-validation-gate")).toBeTruthy();
  });
});
