// The remediation decision of a case (CTEM-17-UI): "Decided by policy" with
// the option and why, a waiting decision answered on the unified work-item
// decision card (never a CTEM card), a viewer who cannot see the decision
// item gets no figure and no answer control, and a case not decided yet leaves
// the card out of the page (#2786).

import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseEvent } from "@/hooks/api/useCtemCasesApi";
import { gateReason, latestGate, parseRemediationOption, remediationOptionLines } from "./case-gates";

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
  WorkItemDecisionCard: ({ item, inCase }: { item: { ask: { options: { label: string; effect: string }[] } }; inCase?: boolean }) => (
    <div data-testid="unified-decision-card" data-in-case={String(Boolean(inCase))}>
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
    const history = [NOTE, ev({ kind: "gate", from: "ctem.remediation.decision", to: "passed_automatically", reason: "decided by policy: pre-approved runbook RB-FW-12 (pin run)", evidence: "A" })];
    renderWithProviders(<CaseRemediationDecision caseId="c1" history={history} />);
    expect(screen.getByText("Decided by policy")).toBeTruthy();
    expect(screen.getByText("Option A")).toBeTruthy();
    // The reason in translated words, never the backend text.
    expect(screen.getByTestId("ctem-case-remediation-why").textContent).toBe("Pre-approved runbook RB-FW-12");
    expect(screen.queryByText(/pin run/)).toBeNull();
    expect(screen.getByText("B. Add a WAF rule")).toBeTruthy();
    expect(screen.getByText("Residual risk: low")).toBeTruthy();
    expect(screen.getByText("Time to apply: 2 days")).toBeTruthy();
    expect(screen.queryByText(/residual risk low/)).toBeNull();
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
        // Even an item that names its case: inside the case, the card never offers "Open the case" (#2370).
        ctem_case: { id: "c1", title: "Case", state: "remediating", path: "/ctem/cases/c1" },
        ask: { options: [{ key: "A", label: "Upgrade nginx", effect: "cost 4,000; expected loss 120,000 before, 8,000 after" }] },
      },
    ];
    const history = [NOTE, ev({ kind: "gate", from: "ctem.remediation.decision", to: "waited", reason: "pinned to wait" })];
    renderWithProviders(<CaseRemediationDecision caseId="c1" history={history} />);
    expect(screen.getByText("Waiting for a person")).toBeTruthy();
    expect(screen.getByTestId("unified-decision-card").getAttribute("data-in-case")).toBe("true");
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

  it("never prints the id of a person it cannot name", () => {
    const id = "948a0ace-8644-4b09-ac27-6eb7ab9f1d93";
    const history = [ev({ kind: "gate", from: "ctem.remediation.decision", to: "answered", reason: id, evidence: "B" })];
    renderWithProviders(<CaseRemediationDecision caseId="c1" history={history} personName={() => null} />);
    expect(screen.getByText("by a person")).toBeTruthy();
    expect(screen.queryByText(new RegExp(id))).toBeNull();
  });

  it("says why a decision waits in words, not the backend text", () => {
    const history = [ev({ kind: "gate", from: "ctem.remediation.decision", to: "waited", reason: "the expected loss is above the approval threshold" })];
    renderWithProviders(<CaseRemediationDecision caseId="c1" history={history} />);
    expect(screen.getByTestId("ctem-case-remediation-why").textContent).toBe("Above the approval threshold");
  });

  it("is left out of the page while the case is not decided yet (#2786)", () => {
    renderWithProviders(<CaseRemediationDecision caseId="c1" history={[]} />);
    expect(screen.queryByTestId("ctem-case-remediation")).toBeNull();
  });

  it("shows as soon as a decision is recorded or its options are written (#2786)", () => {
    const { unmount } = renderWithProviders(
      <CaseRemediationDecision
        caseId="c1"
        history={[]}
        gates={[
          {
            key: "ctem.remediation.decision",
            phase: null,
            status: "answered",
            asked_at: "2026-11-21T09:00:00Z",
            asked_of: null,
            asked_by_agent: null,
            deadline: null,
            answered_at: "2026-11-21T10:00:00Z",
            answered_by: null,
            surface: null,
            option: null,
            comment: null,
            reason: null,
            passed_at: null,
            wait_seconds: 3600,
            decision_id: null,
            work_item_id: null,
            decision_filed: true,
            recorded_on_case: true,
            next: null,
          },
        ]}
      />,
    );
    expect(screen.getByTestId("ctem-case-remediation")).toBeTruthy();
    expect(screen.getByTestId("ctem-case-remediation-record")).toBeTruthy();
    unmount();
    renderWithProviders(<CaseRemediationDecision caseId="c1" history={[NOTE]} />);
    expect(screen.getByText("B. Add a WAF rule")).toBeTruthy();
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

  it("reads an option line as its key, label and facts", () => {
    expect(parseRemediationOption("A. Upgrade Tomcat to 10.1.25: residual risk low; time to apply 5 days")).toEqual({
      key: "A",
      label: "Upgrade Tomcat to 10.1.25",
      facts: [
        { kind: "residual_risk", value: "low" },
        { kind: "time_to_apply", value: "5 days" },
      ],
    });
    expect(parseRemediationOption("B. Restrict: admin port: pre-approved runbook RB-1")).toEqual({
      key: "B",
      label: "Restrict: admin port",
      facts: [{ kind: "runbook", value: "RB-1" }],
    });
    expect(parseRemediationOption("C. Isolate the host: no figures given")).toEqual({ key: "C", label: "Isolate the host", facts: [] });
    expect(parseRemediationOption("free text")).toEqual({ key: "", label: "free text", facts: [] });
  });

  it("classes every reason the backend writes", () => {
    expect(gateReason("decided by policy (level low)", "passed_automatically")).toEqual({ kind: "fleet_runs" });
    expect(gateReason("decided by policy: expected loss within the approval threshold (pin run)", "passed_automatically")).toEqual({ kind: "within_threshold" });
    expect(gateReason("an approval threshold is set and the expected loss is unknown", "waited")).toEqual({ kind: "loss_unknown" });
    expect(gateReason("the gate waits (pin wait)", "waited")).toEqual({ kind: "fleet_waits" });
    expect(gateReason("u-1", "answered")).toBeNull();
  });
});
