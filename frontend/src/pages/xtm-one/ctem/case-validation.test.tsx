// The validation of a case (CTEM-12-UI): safe mode and run order of each
// validation reference, gate A passed automatically by safe mode, a waiting
// non-read-only test on the unified work-item decision card (never a CTEM
// card), and a case not validated yet says so.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseEvent } from "@/hooks/api/useCtemCasesApi";
import { autoPassReason, chosenOptionLabel, latestValidations, safeModeOf } from "./case-validation";

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
  WorkItemDecisionCard: () => <div data-testid="unified-decision-card" />,
}));

const { CaseValidation } = await import("./CaseValidation");

function ev(over: Partial<CtemCaseEvent>): CtemCaseEvent {
  return { at: "2026-11-21T10:00:00Z", actor: "system", kind: "gate", issue: null, from: null, to: null, reason: null, evidence: null, ...over };
}

const SAFE = {
  provider: "openaev",
  kind: "validation",
  ref: "sc-1",
  result: "not_exposed" as const,
  at: "2026-11-21T10:00:00Z",
  safe_mode: true,
  run_order: ["DMZ web", "Core services"],
};

beforeEach(() => {
  state.items = [];
  state.calls = [];
});

describe("case-validation helpers", () => {
  it("lists the newest references first and reads safe mode", () => {
    const older = { ...SAFE, ref: "old", safe_mode: undefined };
    expect(latestValidations([older, SAFE]).map((v) => v.ref)).toEqual(["sc-1", "old"]);
    expect(safeModeOf(SAFE)).toBe("safe");
    expect(safeModeOf({ safe_mode: false })).toBe("unsafe");
    expect(safeModeOf({})).toBeNull();
  });

  it("names safe mode as the reason of an automatic pass, else the fleet", () => {
    expect(autoPassReason("passed_automatically", SAFE)).toBe("safe_mode");
    expect(autoPassReason("passed_automatically", { safe_mode: false })).toBe("fleet_runs");
    expect(autoPassReason("waited", SAFE)).toBeNull();
  });

  it("reads the chosen label from payload.answer, the shape the answer endpoint leaves (ask cleared)", () => {
    const answered = { ask: null, payload: { answer: { option: "A", label: "Run the test that changes the target" } } };
    expect(chosenOptionLabel(answered, "A")).toBe("Run the test that changes the target");
    expect(chosenOptionLabel(answered, "a")).toBe("Run the test that changes the target");
    expect(chosenOptionLabel(answered, "B")).toBeNull();
    expect(chosenOptionLabel({ ask: null, payload: { answer: { option: "A", label: " " } } }, "A")).toBeNull();
  });

  it("falls back to the ask's options, never inventing a label", () => {
    const item = { payload: {}, ask: { options: [{ key: "A", label: "Run it" }, { key: "B", label: "Skip it" }] } };
    expect(chosenOptionLabel(item, "A")).toBe("Run it");
    expect(chosenOptionLabel(item, "C")).toBeNull();
    expect(chosenOptionLabel(undefined, "A")).toBeNull();
    expect(chosenOptionLabel(item, null)).toBeNull();
  });
});

describe("CaseValidation", () => {
  it("shows safe mode, the run order and gate A passed automatically by safe mode", () => {
    const history = [ev({ from: "ctem.validation.non_readonly", to: "passed_automatically", reason: "pin run" })];
    renderWithProviders(<CaseValidation caseId="c1" validations={[SAFE]} history={history} />);
    expect(screen.getByTestId("ctem-case-validation-gate").textContent).toContain("Passed automatically: safe mode");
    expect(screen.getByTestId("ctem-case-validation-mode").textContent).toBe("Safe mode: read-only checks only");
    expect(screen.getByTestId("ctem-case-validation-order").textContent).toBe("1. DMZ web2. Core services");
    expect(screen.queryByText(/pin run/)).toBeNull();
    expect(screen.queryByTestId("unified-decision-card")).toBeNull();
    expect(state.calls.every((c) => (c as { enabled: boolean }).enabled === false)).toBe(true);
  });

  it("renders a waiting non-read-only test on the unified decision card", () => {
    state.items = [{ id: "w1", status: "blocked", needs_human: true, ask: { question: "Run it?", options: [] }, is_company_managed: false }];
    const history = [ev({ from: "ctem.validation.non_readonly", to: "waited" })];
    renderWithProviders(<CaseValidation caseId="c1" validations={[{ ...SAFE, safe_mode: false, result: "pending" }]} history={history} />);
    expect(screen.getByText("Waiting for a person")).toBeTruthy();
    expect(state.calls.some((c) => (c as { filters: { correlation_key: string } }).filters.correlation_key === "ctem:validation:c1")).toBe(true);
    expect(screen.getByTestId("ctem-case-validation-mode").textContent).toBe("Not read-only: a test that changes the target ran");
  });

  it("names the chosen option's label once a person answered, from the closed decision item", () => {
    state.items = [
      {
        id: "w1",
        status: "done",
        needs_human: false,
        ask: null,
        payload: { answer: { option: "A", label: "Run it", question: "Run it?", answered_at: "2026-11-21T10:05:00Z" } },
      },
    ];
    const history = [ev({ from: "ctem.validation.non_readonly", to: "answered", reason: "u1", evidence: "A" })];
    renderWithProviders(
      <CaseValidation caseId="c1" validations={[SAFE]} history={history} personName={(id) => (id === "u1" ? "Ada" : null)} />,
    );
    expect(screen.getByTestId("ctem-case-validation-option").textContent).toBe("Option A: Run it");
    const call = state.calls.find((c) => (c as { enabled: boolean }).enabled) as { filters: { open_only: boolean } };
    expect(call.filters.open_only).toBe(false);
    expect(screen.queryByTestId("unified-decision-card")).toBeNull();
  });

  it("falls back to the option key when the decision item is not visible", () => {
    const history = [ev({ from: "ctem.validation.non_readonly", to: "answered", evidence: "A" })];
    renderWithProviders(<CaseValidation caseId="c1" validations={[SAFE]} history={history} />);
    expect(screen.getByTestId("ctem-case-validation-option").textContent).toBe("Option A");
  });

  it("says a case not validated yet, never blank", () => {
    renderWithProviders(<CaseValidation caseId="c1" validations={[]} history={[]} />);
    expect(screen.getByText(/once the case is validated|once the case is prioritized|writes in OpenCRQ/)).toBeTruthy();
  });
});
