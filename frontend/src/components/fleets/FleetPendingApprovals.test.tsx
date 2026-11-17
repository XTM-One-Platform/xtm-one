// The fleet's pending approvals (#10202, slice D of #10000): the runs of the
// fleet that wait for a person, read with a GET narrowed to the fleet and to
// awaiting_human (no run started, no quota), a row unfolding the decision path,
// an empty state when nothing waits.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { AssignmentRunDetail } from "@/types";
import { renderWithProviders } from "@/testing/providers";

import { FleetPendingApprovals, pendingActionLabels, pendingKind, pendingToolNames, stillWaiting } from "./FleetPendingApprovals";

const api = vi.hoisted(() => ({
  runs: [] as unknown[],
  calls: [] as unknown[][],
  mutate: vi.fn(),
}));
vi.mock("@/hooks/api/useAssignmentApi", () => ({
  useAllAssignmentRuns: (...args: unknown[]) => {
    api.calls.push(args);
    return { data: api.runs, isLoading: false, isError: false, refetch: vi.fn() };
  },
  useRespondToHumanInput: () => ({ mutate: api.mutate, isError: false }),
  useSubmitApprovalDecisions: () => ({ mutate: api.mutate, isError: false }),
  useDismissRun: () => ({ mutate: api.mutate, isError: false }),
}));

const run = (id: string, extra: Partial<AssignmentRunDetail>): AssignmentRunDetail =>
  ({
    id,
    assignment_id: "a1",
    assignment_name: "Daily triage",
    agent_id: "ag1",
    agent_name: "Analyst",
    status: "awaiting_human",
    is_company_managed: false,
    human_input_question: null,
    human_input_response: null,
    human_input_history: null,
    pending_approvals: null,
    approval_answered: false,
    created_at: "2026-11-08T20:00:00Z",
    ...extra,
  }) as AssignmentRunDetail;

const proposal = (name: string) => ({
  tool_call_id: `c-${name}`,
  tool_name: name,
  tool_description: "",
  arguments: {},
  input_schema: {},
});

beforeEach(() => {
  api.runs = [];
  api.calls = [];
  api.mutate.mockClear();
});
afterEach(cleanup);

describe("pending approvals helpers", () => {
  it("names what a run waits on and keeps only what still waits for a person", () => {
    const actions = run("r1", { pending_approvals: [proposal("send_email"), proposal("send_email"), proposal("create_ticket")] as never });
    expect(pendingKind(actions)).toBe("actions");
    expect(pendingToolNames(actions)).toEqual(["send_email", "create_ticket"]);
    expect(pendingKind(run("r2", { human_input_question: "Which tenant?" }))).toBe("question");
    const decided = run("r3", { pending_approvals: [proposal("x")] as never, approval_answered: true });
    const answered = run("r4", { human_input_response: "yes" });
    const finished = run("r5", { status: "completed" });
    expect(stillWaiting([actions, decided, answered, finished]).map((r) => r.id)).toEqual(["r1"]);
  });

  it("names the actions as a person reads them, never the internal tool id (UX r1 B3)", () => {
    const actions = run("r1", { pending_approvals: [proposal("send_email"), proposal("jira_create_ticket")] as never });
    const labels = pendingActionLabels(actions, (id) => id);
    expect(labels).toEqual(["Sending email", "Jira create ticket"]);
    expect(labels.join(" ")).not.toMatch(/_/);
  });
});

describe("FleetPendingApprovals", () => {
  it("reads the fleet's awaiting_human runs only, through the runs feed", () => {
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    const args = api.calls[0];
    expect(args[2]).toBe("awaiting_human");
    expect(args[13]).toBe("f1");
  });

  it("shows an empty state when nothing waits", () => {
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    expect(screen.getByText("Nothing waits for approval")).toBeTruthy();
    expect(screen.queryByTestId("fleet-pending-row")).toBeNull();
  });

  it("lists each waiting run and unfolds its decision path on click", () => {
    api.runs = [
      run("r1", { pending_approvals: [proposal("send_email")] as never }),
      run("r2", { agent_name: "Writer", human_input_question: "Which tenant?" }),
    ];
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    const rows = screen.getAllByTestId("fleet-pending-row");
    expect(rows).toHaveLength(2);
    expect(screen.getByText("1 action: Sending email")).toBeTruthy();
    expect(screen.queryByText(/send_email/)).toBeNull();
    expect(screen.queryByTestId("fleet-pending-decision")).toBeNull();
    fireEvent.click(screen.getByText("1 action: Sending email"));
    expect(screen.getByTestId("fleet-pending-decision")).toBeTruthy();
  });

  it("heads the section with a pending summary and a trailing Review / Hide per row (UX r1 B5, B6)", () => {
    api.runs = [
      run("r1", { pending_approvals: [proposal("send_email")] as never }),
      run("r2", { agent_name: "Writer", human_input_question: "Which tenant?" }),
    ];
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    expect(screen.getByTestId("fleet-pending-summary").textContent).toContain("2 runs wait for a person");
    // A relative time reads after "started", never after "since" (UX r2 B2).
    expect(screen.getByTestId("fleet-pending-summary").textContent).toContain("oldest started ");
    expect(screen.getByTestId("fleet-pending-summary").textContent).not.toContain("oldest since");
    const toggles = screen.getAllByTestId("fleet-pending-toggle");
    expect(toggles).toHaveLength(2);
    expect(toggles[0].textContent).toBe("Review");
    expect(toggles[0].getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggles[0]);
    expect(screen.getAllByTestId("fleet-pending-toggle")[0].textContent).toBe("Hide");
    expect(screen.getAllByTestId("fleet-pending-toggle")[0].getAttribute("aria-expanded")).toBe("true");
    expect(screen.getAllByTestId("fleet-pending-decision")).toHaveLength(1);
  });

  it("keeps every row one line high: agent and waits-on truncate on one line (UX r1 B1, B2)", () => {
    api.runs = [run("r1", { agent_name: "A very long agent name that would wrap", human_input_question: "A long question ".repeat(20) })];
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    for (const id of ["fleet-pending-agent", "fleet-pending-waits-on"]) {
      expect(screen.getByTestId(id).getAttribute("data-truncate")).toBe("one-line");
    }
    expect(screen.getByRole("table").getAttribute("data-layout")).toBe("fixed");
  });
});
