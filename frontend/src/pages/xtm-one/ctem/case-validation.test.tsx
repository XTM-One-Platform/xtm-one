// The validation of a case (CTEM-12-UI): run order of each validation
// reference, gate A passed automatically at the fleet's level, a waiting
// validation on the unified work-item decision card (never a CTEM card), a
// stored safe_mode never rendered (#1810), and a case not validated yet says so.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseEvent } from "@/hooks/api/useCtemCasesApi";
import { autoPassReason, chosenOptionLabel, latestValidations } from "./case-validation";

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
  WorkItemDecisionCard: ({ inCase }: { inCase?: boolean }) => <div data-testid="unified-decision-card" data-in-case={String(Boolean(inCase))} />,
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
  run_order: ["DMZ web", "Core services"],
};

beforeEach(() => {
  state.items = [];
  state.calls = [];
});

describe("case-validation helpers", () => {
  it("lists the newest references first", () => {
    const older = { ...SAFE, ref: "old" };
    expect(latestValidations([older, SAFE]).map((v) => v.ref)).toEqual(["sc-1", "old"]);
  });

  it("names the fleet as the reason of an automatic pass, never a class of the inject", () => {
    expect(autoPassReason("passed_automatically")).toBe("fleet_runs");
    expect(autoPassReason("waited")).toBeNull();
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
  it("shows the run order and gate A passed automatically at the fleet's level, never a stored safe mode", () => {
    const history = [ev({ from: "ctem.validation.non_readonly", to: "passed_automatically", reason: "pin run" })];
    const stored = { ...SAFE, safe_mode: true } as typeof SAFE;
    renderWithProviders(<CaseValidation caseId="c1" validations={[stored]} history={history} />);
    expect(screen.getByTestId("ctem-case-validation-gate").textContent).toContain("The fleet runs this gate without a person");
    expect(screen.queryByTestId("ctem-case-validation-mode")).toBeNull();
    expect(screen.queryByText(/safe mode/i)).toBeNull();
    // Result and run order are chips (#1679), the provider with its logo.
    expect(screen.getByTestId("ctem-case-validation-result").getAttribute("data-tone")).toBe("green");
    expect(screen.getByTestId("ctem-case-validation-provider").textContent).toBe("OpenAEV");
    expect(screen.getByTestId("ctem-case-validation-order").textContent).toBe("1. DMZ web2. Core services");
    expect(screen.getByTestId("ctem-case-validation-order").querySelectorAll("[data-tone]")).toHaveLength(2);
    expect(screen.queryByText(/pin run/)).toBeNull();
    expect(screen.queryByTestId("unified-decision-card")).toBeNull();
    expect(state.calls.every((c) => (c as { enabled: boolean }).enabled === false)).toBe(true);
  });

  it("renders a waiting validation on the unified decision card", () => {
    // Even an item that names its case: inside the case, the card never offers "Open the case" (#2370).
    const ctem_case = { id: "c1", title: "Case", state: "validating", path: "/ctem/cases/c1" };
    state.items = [{ id: "w1", status: "blocked", needs_human: true, ask: { question: "Run it?", options: [] }, is_company_managed: false, ctem_case }];
    const history = [ev({ from: "ctem.validation.non_readonly", to: "waited" })];
    renderWithProviders(<CaseValidation caseId="c1" validations={[{ ...SAFE, result: "pending" }]} history={history} />);
    expect(screen.getByText("Waiting for a person")).toBeTruthy();
    expect(screen.getByTestId("unified-decision-card").getAttribute("data-in-case")).toBe("true");
    expect(state.calls.some((c) => (c as { filters: { correlation_key: string } }).filters.correlation_key === "ctem:validation:c1")).toBe(true);
    expect(screen.queryByTestId("ctem-case-validation-mode")).toBeNull();
    expect(screen.getByTestId("ctem-case-validation-result").textContent).toBe("Pending");
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
