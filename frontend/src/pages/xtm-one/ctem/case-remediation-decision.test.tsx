// The remediation decision of a case (CTEM-17-UI): "Decided by policy" with
// the option and why, a waiting decision answered on the unified work-item
// decision card (never a CTEM card), a viewer who cannot see the decision
// item gets no figure and no answer control, and a case not decided yet says so.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseEvent } from "@/hooks/api/useCtemCasesApi";
import { latestGate, remediationOptionLines } from "./case-gates";

const state = vi.hoisted(() => ({ items: [] as unknown[], calls: [] as unknown[] }));

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: false }) }));
vi.mock("@/hooks/api/useWorkItemApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useWorkItemApi")>()),
  useWorkItems: (filters: unknown, enabled: boolean) => {
    state.calls.push({ filters, enabled });
    return { data: enabled ? state.items : undefined, isLoading: false, refetch: vi.fn() };
  },
}));
vi.mock("@/components/work-items/WorkItemDecisionCard", () => ({
  WorkItemDecisionCard: ({ item }: { item: { ask: { options: { label: string; effect: string }[] } } }) => (
    <div data-testid="unified-decision-card">
      {item.ask.options.map((o) => (
        <span key={o.label}>{`${o.label}: ${o.effect}`}</span>
      ))}
    </div>
  ),
}));

const { CaseRemediationDecision } = await import("./CaseRemediationDecision");

function ev(over: Partial<CtemCaseEvent>): CtemCaseEvent {
  return { at: "2026-11-21T10:00:00Z", actor: "system", kind: "note", issue: null, from: null, to: null, reason: null, evidence: null, ...over };
}

const NOTE = ev({
  kind: "note",
  reason: "Remediation option A: decided by policy (autonomy level)\nA. Upgrade nginx to 1.27.4: residual risk low; time to apply 2 days\nB. Add a WAF rule: residual risk medium",
});

beforeEach(() => {
  state.items = [];
  state.calls = [];
});

describe("CaseRemediationDecision", () => {
  it("shows decided by policy with the option, why, and the figure-free options", () => {
    const history = [NOTE, ev({ kind: "gate", from: "ctem.remediation.decision", to: "passed_automatically", reason: "autonomy level", evidence: "A" })];
    renderWithProviders(<CaseRemediationDecision caseId="c1" history={history} />);
    expect(screen.getByText("Decided by policy")).toBeTruthy();
    expect(screen.getByText("Option A")).toBeTruthy();
    expect(screen.getByText("autonomy level")).toBeTruthy();
    expect(screen.getByText(/B\. Add a WAF rule/)).toBeTruthy();
    expect(screen.queryByTestId("unified-decision-card")).toBeNull();
    // Nothing waits: the decision item is not read.
    expect(state.calls.every((c) => (c as { enabled: boolean }).enabled === false)).toBe(true);
  });

  it("renders a waiting decision on the unified decision card, with its figures", () => {
    state.items = [
      {
        id: "w1",
        status: "blocked",
        needs_human: true,
        is_company_managed: false,
        correlation_key: "ctem:remediation:c1",
        ask: { options: [{ key: "A", label: "Upgrade nginx", effect: "cost 4,000; expected loss 120,000 before, 8,000 after" }] },
      },
    ];
    const history = [NOTE, ev({ kind: "gate", from: "ctem.remediation.decision", to: "waited", reason: "pinned to wait" })];
    renderWithProviders(<CaseRemediationDecision caseId="c1" history={history} />);
    expect(screen.getByText("Waited for a person")).toBeTruthy();
    expect(screen.getByTestId("unified-decision-card")).toBeTruthy();
    expect(screen.getByText(/expected loss 120,000 before/)).toBeTruthy();
    expect(state.calls).toContainEqual({ filters: { correlation_key: "ctem:remediation:c1", open_only: true, limit: 1 }, enabled: true });
  });

  it("shows a viewer who cannot see the decision item that it waits, with no figure and no answer", () => {
    const history = [NOTE, ev({ kind: "gate", from: "ctem.remediation.decision", to: "waited", reason: "pinned to wait" })];
    renderWithProviders(<CaseRemediationDecision caseId="c1" history={history} />);
    expect(screen.getByTestId("ctem-case-remediation-elsewhere")).toBeTruthy();
    expect(screen.queryByTestId("unified-decision-card")).toBeNull();
    expect(screen.queryByText(/expected loss/)).toBeNull();
    expect(screen.queryByText(/cost/)).toBeNull();
  });

  it("names who answered and the option", () => {
    const history = [
      ev({ kind: "gate", from: "ctem.remediation.decision", to: "waited", at: "2026-11-21T10:00:00Z" }),
      ev({ kind: "gate", from: "ctem.remediation.decision", to: "answered", reason: "u-1", evidence: "B", at: "2026-11-21T11:00:00Z" }),
    ];
    renderWithProviders(<CaseRemediationDecision caseId="c1" history={history} personName={(id) => (id === "u-1" ? "Jane Doe" : null)} />);
    expect(screen.getByText("Answered by a person")).toBeTruthy();
    expect(screen.getByText("Option B")).toBeTruthy();
    expect(screen.getByText("by Jane Doe")).toBeTruthy();
  });

  it("says so when the case is not decided yet", () => {
    renderWithProviders(<CaseRemediationDecision caseId="c1" history={[]} />);
    expect(screen.getByText(/Not decided yet/)).toBeTruthy();
  });
});

describe("case gates", () => {
  it("reads the newest gate of a key and ignores other gates", () => {
    const history = [
      ev({ kind: "gate", from: "ctem.remediation.decision", to: "answered", at: "2026-11-21T12:00:00Z" }),
      ev({ kind: "gate", from: "ctem.remediation.decision", to: "waited", at: "2026-11-21T10:00:00Z" }),
      ev({ kind: "gate", from: "ctem.mobilize", to: "waited", at: "2026-11-21T13:00:00Z" }),
    ];
    expect(latestGate(history, "ctem.remediation.decision")?.to).toBe("answered");
    expect(latestGate([], "ctem.remediation.decision")).toBeNull();
  });

  it("reads the option lines of the newest remediation note only", () => {
    expect(remediationOptionLines([ev({ kind: "note", reason: "Something else\nA. x" }), NOTE])).toHaveLength(2);
    expect(remediationOptionLines([])).toEqual([]);
  });
});
